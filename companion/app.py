"""Application bootstrap: wires the async BlueZ + audio layers to the Qt UI via qasync.

The BlueZ manager and the audio engine are framework-free and side-effect-explicit; this
file is the ONLY place that knows about both Qt and asyncio. Blocking CLI calls
(pw-dump / pactl / wpctl) run in a thread executor so the UI never stalls; D-Bus calls run
as coroutines on the qasync loop.

Everything the UI can emit is handled here — there are no dead signals:
  scan            -> power adapters, filtered discovery, AUTO-STOP after a window
  connect/pair    -> pair (via the registered Agent1) if new, then connect, then enrich
  sound           -> codec dialog -> WirePlumber policy (merged) -> reconnect to apply
  share           -> resolve REAL pw nodes -> live module-combine-sink -> teardown
  volume          -> wpctl on the resolved node id
  tray actions    -> same handlers as the window
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from companion.audio import codecs, pwcli
from companion.audio import wireplumber as wp
from companion.bluez.device import BluezDevice
from companion.bluez.manager import BluezManager
from companion.core import capabilities
from companion.core.errors import friendly_bluez_error
from companion.core.share import resolve_share_action
from companion.core.models import Device, DeviceKind, ShareTarget
from companion.ui import theme
from companion.ui.main_window import MainWindow
from companion.ui.sound_dialog import SoundDialog
from companion.ui.more_dialog import MoreDialog
from companion.ui.tray import CompanionTray

log = logging.getLogger(__name__)

_DISCOVERY_SECONDS = 30


class CompanionApp:
    def __init__(self) -> None:
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        theme.apply(self.qapp)
        self.window = MainWindow()
        self.tray = CompanionTray() if CompanionTray.available() else None
        self.manager = BluezManager(on_confirm=self._confirm_pair,
                                    on_display=self._display_passkey)
        # Persisted per-device WirePlumber rules (kept merged — Mo2).
        self._rules: dict[str, wp.DeviceRule] = {}
        self._combine_module: Optional[int] = None
        self._audio_unavailable = False   # set once if the PipeWire CLIs are missing
        self._busy_paths: set[str] = set()  # connect/disconnect in flight (debounce)
        self._scanning = False
        # Local (non-Bluetooth) outputs — headphone jack / HDMI / USB — keyed by pw:path.
        self._local_devices: dict[str, Device] = {}
        self._local_refreshing = False           # reentrancy guard for the live poll
        self._local_timer: Optional[QTimer] = None
        # Share state: the DESIRED group (persists for auto-rejoin) vs the live members.
        self._share_intent: dict[str, ShareTarget] = {}
        self._share_members: set[str] = set()
        self._share_resyncing = False
        self._share_resync_again = False
        self._wire()

    # ----------------------------------------------------------------- wiring
    def _wire(self) -> None:
        self.manager.on_device_added = self._on_added
        self.manager.on_device_changed = self._on_changed
        self.manager.on_device_removed = self._on_removed
        self.manager.on_adapter_changed = (
            lambda: self._spawn(self._refresh_adapter_banner()))

        d = self.window.detail
        self.window.scan_requested.connect(lambda: self._spawn(self._scan_then_stop()))
        self.window.adapter_action_requested.connect(self._on_adapter_action)
        d.connect_requested.connect(lambda p: self._spawn(self._connect(p)))
        d.disconnect_requested.connect(lambda p: self._spawn(self._disconnect(p)))
        d.sound_requested.connect(self._open_sound_dialog)
        d.mic_requested.connect(lambda p: self._spawn(self._toggle_mic(p)))
        d.volume_changed.connect(
            lambda p, frac: self._spawn(self._set_volume(p, frac)))
        d.more_requested.connect(self._open_more)
        d.transport_requested.connect(
            lambda p, v: self._spawn(self._transport(p, v)))
        d.mute_toggled.connect(lambda p: self._spawn(self._toggle_mute(p)))
        self.window.share.play_requested.connect(
            lambda targets: self._spawn(self._start_share(targets)))
        self.window.share.stop_requested.connect(
            lambda: self._spawn(self._stop_share()))
        self.window.share.member_volume_changed.connect(
            lambda p, frac: self._spawn(self._set_volume(p, frac)))

        # Clean up our combine sink(s) on quit so they never linger in KDE/pavucontrol.
        self.qapp.aboutToQuit.connect(self._on_about_to_quit)

        if self.tray:
            # A tray exists, so the headerless close button hides to background
            # instead of quitting; the app keeps running for playback / share /
            # auto-reconnect. Real quit is the tray's Quit action.
            self.qapp.setQuitOnLastWindowClosed(False)
            self.window.set_minimize_to_tray(True)
            self.tray.open_requested.connect(self._raise_window)
            self.tray.quit_requested.connect(self.qapp.quit)
            self.tray.sound_requested.connect(self._tray_sound)
            self.tray.share_requested.connect(self._focus_share)
            self.tray.swap_requested.connect(self._swap_device)
            self.tray.output_selected.connect(
                lambda name: self._spawn(self._select_output(name)))

    # ------------------------------------------------------------- BlueZ glue
    def _bluez(self, path: str) -> BluezDevice:
        dev = self.manager.device(path)
        if dev is None:
            raise KeyError(path)
        return dev

    def _on_added(self, dev: BluezDevice) -> None:
        self.window.upsert_device(dev.model)
        self._refresh_tray()
        if dev.model.connected:
            self._spawn(self._enrich(dev))

    def _on_changed(self, dev: BluezDevice) -> None:
        self.window.upsert_device(dev.model)
        self._refresh_tray()
        # When a device transitions to connected, fill in codec/volume/node.
        if dev.model.connected and dev.model.node_name is None:
            self._spawn(self._enrich(dev))
        # Keep an active Share group synced: a member whose connection state has
        # diverged from the live group must be removed (dropped) or re-added.
        if dev.path in self._share_intent:
            if dev.model.connected != (dev.path in self._share_members):
                self._spawn(self._rebuild_share())

    def _on_removed(self, path: str) -> None:
        self.window.remove_device(path)
        self._refresh_tray()
        # A member object vanished entirely — reconcile the group (intent is kept so
        # it auto-rejoins if the device comes back).
        if path in self._share_members:
            self._spawn(self._rebuild_share())

    # --------------------------------------------------------------- actions
    async def _scan_then_stop(self) -> None:
        if self._scanning:
            return                                # already scanning — ignore re-clicks
        # Gate on the adapter machine: never start discovery unless Bluetooth is Ready.
        from companion.core import capabilities
        state = await self.manager.adapter_state()
        if state != capabilities.AdapterState.READY:
            await self._refresh_adapter_banner()
            self._warn("Can't scan",
                       capabilities.adapter_message(state) or "Bluetooth isn't ready.")
            return
        self._scanning = True
        try:
            await self.manager.start_discovery()
            await asyncio.sleep(_DISCOVERY_SECONDS)
        except Exception as exc:
            msg, _ = friendly_bluez_error(exc)
            self._warn("Can't scan", msg or "Bluetooth scan failed. Is the adapter on?")
            log.warning("discovery failed: %s", exc)
        finally:
            try:
                await self.manager.stop_discovery()
            except Exception:
                log.debug("stop_discovery failed (already stopped?)")
            self._scanning = False

    async def _connect(self, path: str) -> None:
        if path in self._busy_paths:
            return                                # debounce double-clicks
        self._busy_paths.add(path)
        try:
            dev = self._bluez(path)
            if dev.model.connected:               # already on — just (re)enrich
                await self._enrich(dev)
                return
            if not dev.model.paired:
                try:
                    await asyncio.wait_for(dev.pair(), timeout=30)
                except asyncio.TimeoutError:
                    await dev.cancel_pairing()
                    self._warn("Pairing timed out",
                               "The device didn't respond. Put it in pairing mode, "
                               "move it closer, and try again.")
                    log.warning("pair timed out for %s", path)
                    return
                except Exception as exc:
                    msg, _ = friendly_bluez_error(exc)
                    if msg:
                        self._warn("Pairing failed", msg)
                    log.warning("pair failed for %s: %s", path, exc)
                    return
            await self._connect_with_retry(dev)
            await self._enrich(dev)
        except KeyError:
            log.info("device %s vanished before connect", path)
        finally:
            self._busy_paths.discard(path)

    async def _connect_with_retry(self, dev: BluezDevice, attempts: int = 3) -> None:
        """Connect, retrying transient BlueZ states (busy / in-progress / abort)."""
        for i in range(attempts):
            if dev.model.connected:
                return
            try:
                await dev.connect()
                return
            except Exception as exc:
                msg, transient = friendly_bluez_error(exc)
                if not msg:                       # benign (already connected)
                    return
                if transient and i < attempts - 1:
                    log.info("connect %s transient (%s); retry %d/%d",
                             dev.path, exc, i + 1, attempts - 1)
                    await asyncio.sleep(1.5)
                    continue
                self._warn("Could not connect", msg)
                log.warning("connect failed for %s: %s", dev.path, exc)
                return

    async def _disconnect(self, path: str) -> None:
        if path in self._busy_paths:
            return
        self._busy_paths.add(path)
        try:
            await self._bluez(path).disconnect()
        except KeyError:
            pass                                  # already gone
        except Exception as exc:
            msg, _ = friendly_bluez_error(exc)
            if msg:                               # empty == benign (not connected)
                log.warning("disconnect failed for %s: %s", path, exc)
        finally:
            self._busy_paths.discard(path)

    async def _enrich(self, dev: BluezDevice) -> None:
        """Resolve the live PipeWire node, codec and volume for a connected device."""
        if self._audio_unavailable:
            return  # already established the PipeWire CLIs are missing — don't spam
        address = dev.model.address
        try:
            nodes = await self._exec(pwcli.dump_nodes)
        except pwcli.AudioToolMissing as exc:
            self._audio_unavailable = True   # warn ONCE, then stop retrying
            log.warning(
                "PipeWire CLI '%s' not found on PATH — codec, volume and Share are "
                "disabled. Install the PipeWire utilities (Fedora: 'pipewire-utils' + "
                "'pulseaudio-utils' + 'wireplumber'; Arch: 'pipewire' + 'pipewire-pulse' "
                "+ 'wireplumber'; Debian/Ubuntu: 'pipewire-bin' + 'pipewire-pulse' + "
                "'wireplumber'), then restart Companion.", exc)
            return
        except Exception:
            log.exception("pw-dump failed")
            return
        node = pwcli.resolve_sink(address, nodes)
        if node is not None:
            dev.model.node_name = node.name
            dev.model.node_id = node.id
            dev.set_codec(pwcli.resolve_codec(address, nodes))
            vol = await self._exec(pwcli.get_volume, node.id)
            dev.set_volume(vol)
        if dev.model.has_mic:
            await self._enrich_mic(dev, nodes)
        self.window.upsert_device(dev.model)
        self._refresh_tray()

    def _local_device(self, node) -> Device:
        """Wrap a live PipeWire output node (jack / HDMI / USB) as a Device.

        path is `pw:<node_name>` so it never collides with a BlueZ object path; it is
        always paired+connected and flagged is_local so capabilities expose only volume
        and Share/combine — no connect/pair/codec/mic verbs.
        """
        return Device(
            path=f"pw:{node.name}",
            address="",
            name=node.description or node.name,
            kind=DeviceKind.SPEAKER,
            paired=True,
            connected=True,
            is_local=True,
            node_name=node.name,
            node_id=node.id,
        )

    async def _refresh_local_outputs(self) -> None:
        """Enumerate the system's own audio outputs and present them as always-connected
        devices that can join a Share/combine group. Reads the live PipeWire graph; never
        fabricates a sink. Drops ones that disappeared (e.g. unplugged headphones).

        Runs on a short timer (see start()), so a freshly plugged jack / USB DAC / HDMI
        sink appears in the list and mixer live — PipeWire has no cheap "sink added"
        signal we can lean on here. To keep idle polls flicker-free, a device is only
        pushed into the UI when it's new or actually changed."""
        if self._audio_unavailable or self._local_refreshing:
            return
        self._local_refreshing = True
        try:
            try:
                nodes = await self._exec(pwcli.dump_nodes)
            except pwcli.AudioToolMissing:
                self._audio_unavailable = True
                return
            except Exception:
                log.exception("pw-dump failed while listing local outputs")
                return
            seen: set[str] = set()
            for node in pwcli.list_local_outputs(nodes):
                dev = self._local_device(node)
                seen.add(dev.path)
                try:
                    dev.volume = await self._exec(pwcli.get_volume, node.id)
                except Exception:
                    dev.volume = None
                prev = self._local_devices.get(dev.path)
                self._local_devices[dev.path] = dev
                if (prev is None or prev.volume != dev.volume
                        or prev.name != dev.name or prev.node_id != dev.node_id):
                    self.window.upsert_device(dev)
            for gone in [p for p in self._local_devices if p not in seen]:
                self._local_devices.pop(gone, None)
                self.window.remove_device(gone)
            await self._sync_tray_outputs(nodes)
        finally:
            self._local_refreshing = False

    def _model_for(self, path: str) -> Optional[Device]:
        """The Device model for either a local output (pw:) or a BlueZ device."""
        if path.startswith("pw:"):
            return self._local_devices.get(path)
        try:
            return self._bluez(path).model
        except KeyError:
            return None

    async def _node_id_for(self, path: str) -> Optional[int]:
        """Resolve the PipeWire node id for a device, enriching a BT device if needed."""
        if path.startswith("pw:"):
            dev = self._local_devices.get(path)
            return dev.node_id if dev else None
        dev = self._bluez(path)
        if dev.model.node_id is None:
            await self._enrich(dev)
        return dev.model.node_id

    def _reflect_volume(self, path: str, fraction: float) -> None:
        """Keep the per-device slider and the Share member slider in lock-step."""
        model = self._model_for(path)
        if model is not None:
            model.volume = fraction
        self.window.reflect_volume(path, fraction)

    def _on_about_to_quit(self) -> None:
        """Gracefully remove our combine sink(s) on quit so they never linger in
        KDE / pavucontrol / `pactl list` after the app exits."""
        try:
            if self._combine_module is not None:
                pwcli.stop_combine(self._combine_module)
                self._combine_module = None
            pwcli.stop_all_combines()
        except Exception:
            log.exception("combine cleanup on quit failed")

    async def _set_volume(self, path: str, fraction: float) -> None:
        node_id = await self._node_id_for(path)
        if node_id is not None:
            await self._exec(pwcli.set_volume, node_id, fraction)
            self._reflect_volume(path, fraction)

    # -------------------------------------------------------------- transport
    async def _transport(self, path: str, verb: str) -> None:
        """AVRCP passthrough (play/pause/next/previous). No-op if no player."""
        if not await self.manager.control_media(path, verb):
            log.debug("transport %s unavailable for %s", verb, path)

    async def _toggle_mute(self, path: str) -> None:
        """Per-device output mute via the resolved PipeWire node (BT or local)."""
        node_id = await self._node_id_for(path)
        if node_id is None:
            self._warn("Mute", "No audio output node for this device yet.")
            return
        if await self._exec(pwcli.set_mute, node_id, "toggle"):
            muted = await self._exec(pwcli.is_muted, node_id)
            model = self._model_for(path)
            if model is not None:
                model.muted = muted
                self.window.upsert_device(model)

    # ------------------------------------------------------------------- mic
    async def _enrich_mic(self, dev: BluezDevice, nodes=None) -> None:
        """Resolve the device's PipeWire card, its current mode, and (in mic mode) the
        live mic source node + level. Reads pactl/pw-dump — never guesses."""
        if self._audio_unavailable:
            return
        address = dev.model.address
        try:
            card = await self._exec(pwcli.card_for_address, address)
        except pwcli.AudioToolMissing:
            self._audio_unavailable = True
            return
        except Exception:
            log.exception("card lookup failed")
            return
        m = dev.model
        if card is None:
            m.card_name = None
            m.mic_active = False
            m.mic_node_id = None
            return
        m.card_name = card.name
        m.active_profile = card.active_profile
        m.headset_profile = codecs.best_headset_profile(card.profiles)
        if codecs.is_a2dp_profile(card.active_profile):
            m.music_profile = card.active_profile     # remember to return to it later
        m.mic_active = codecs.profile_mode(card.active_profile) == "mic"
        if m.mic_active:
            try:
                src = await self._exec(pwcli.resolve_source, address, nodes)
            except Exception:
                src = None
            if src is not None:
                m.mic_node_id = src.id
                m.mic_volume = await self._exec(pwcli.get_volume, src.id)
                m.mic_muted = await self._exec(pwcli.is_muted, src.id)
        else:
            m.mic_node_id = None

    async def _toggle_mic(self, path: str) -> None:
        """One-tap MIC: flip the device between Music (A2DP, mic off) and Call (HFP/HSP,
        mic on). No modal — the MIC button itself is the control.

        Returning to Music restores the REMEMBERED A2DP codec profile (see
        capabilities.mic_toggle_profile), so toggling the mic never silently downgrades
        your audio. The verb stays disabled for devices with no headset profile."""
        if self._audio_unavailable:
            self._warn("Microphone", "PipeWire tools aren't available.")
            return
        try:
            dev = self._bluez(path)
        except KeyError:
            return
        m = dev.model
        if not m.has_mic:
            self._warn("No microphone",
                       "This device doesn't advertise a headset/hands-free mic profile.")
            return
        if m.card_name is None:
            await self._enrich_mic(dev)
        if m.card_name is None:
            self._warn("Microphone",
                       "No PipeWire card for this device yet — connect it and retry.")
            return
        target = capabilities.mic_toggle_profile(m)
        if target is None:
            self._warn("Microphone",
                       "This device exposes no headset (HFP/HSP) profile, so its mic "
                       "can't be used on this system.")
            return
        going_to_call = not m.mic_active
        ok = await self._exec(pwcli.set_card_profile, m.card_name, target)
        if not ok:
            self._warn("Microphone", "Could not switch the audio profile.")
            return
        # Switching profiles re-creates the sink/source; re-resolve so codec, volume,
        # node id and mic state don't go stale (_enrich also refreshes the mic).
        await self._enrich(dev)
        log.info("mic %s for %s",
                 "ON (call mode)" if going_to_call else "OFF (music)", path)
        self.window.upsert_device(m)
        self._refresh_tray()

    # ------------------------------------------------------------------ more
    def _open_more(self, path: str) -> None:
        dev = self.manager.device(path)
        if dev is None:
            return
        rule = self._rules.get(dev.model.address_underscored)
        auto = bool(rule.auto_connect) if rule and rule.auto_connect is not None else False
        dlg = MoreDialog(dev.model, auto_connect=auto, parent=self.window)
        dlg.reconnect_requested.connect(lambda p: self._spawn(self._reconnect(p)))
        dlg.trust_toggled.connect(
            lambda p, t: self._spawn(self._set_trusted(p, t)))
        dlg.forget_requested.connect(lambda p: self._spawn(self._forget(p)))
        dlg.autoconnect_toggled.connect(
            lambda p, on: self._spawn(self._apply_autoconnect(p, on)))
        dlg.rename_requested.connect(lambda p, name: self._spawn(self._rename(p, name)))
        dlg.exec()

    async def _rename(self, path: str, alias: str) -> None:
        """Persist a friendly name via the BlueZ Alias (Device1.Alias). BlueZ emits
        PropertiesChanged so the watch updates the UI; we also upsert immediately for
        snappy feedback and refresh the tray tooltip."""
        dev = self.manager.device(path)
        if dev is None:
            return
        try:
            await dev.set_alias(alias)
        except Exception as exc:
            log.exception("rename failed for %s", path)
            detail = str(exc).strip() or exc.__class__.__name__
            self._warn("Rename", f"Could not rename this device.\n\n{detail}")
            return
        dev.model.name = alias
        self.window.upsert_device(dev.model)
        self._refresh_tray()
        log.info("renamed %s -> %r", path, alias)

    async def _set_trusted(self, path: str, trusted: bool) -> None:
        try:
            await self._bluez(path).set_trusted(trusted)
            self.window.upsert_device(self._bluez(path).model)
        except Exception:
            log.debug("set_trusted(%s) failed for %s", trusted, path)

    async def _forget(self, path: str) -> None:
        """Forget (unpair) a device via Adapter1.RemoveDevice, then drop its rule."""
        model = self.window.device_for(path)
        try:
            ok = await self.manager.remove_device(path)
        except Exception as exc:
            msg, _ = friendly_bluez_error(exc)
            self._warn("Could not forget",
                       msg or "BlueZ refused to remove the device.")
            return
        if ok:
            if model is not None:
                self._rules.pop(model.address_underscored, None)
            self.window.remove_device(path)
            self._refresh_tray()

    async def _apply_autoconnect(self, path: str, enabled: bool) -> None:
        """Persist a per-device WirePlumber bluez5.auto-connect override."""
        model = self.window.device_for(path)
        if model is None:
            return
        key = model.address_underscored
        existing = self._rules.get(key)
        try:
            self._rules[key] = wp.DeviceRule(
                device_name_glob=f"bluez_card.{key}",
                nick=(existing.nick if existing else model.name),
                ldac_quality=(existing.ldac_quality if existing else None),
                aac_bitratemode=(existing.aac_bitratemode if existing else None),
                auto_connect=enabled,
            )
            await self._exec(wp.apply_rules, list(self._rules.values()))
        except Exception:
            log.exception("failed to write auto-connect rule")
            self._warn("Could not save setting",
                       "Check permissions on your WirePlumber config directory.")
            return
        # Per-device rules only take effect after WirePlumber reloads.
        if not await self._exec(wp.reload):
            self._warn("Restart WirePlumber to apply",
                       "Run:  systemctl --user restart wireplumber")

    # --------------------------------------------------------------- adapter
    def _on_adapter_action(self) -> None:
        self._spawn(self._adapter_action_flow())

    async def _adapter_action_flow(self) -> None:
        """Banner action: unblock rfkill (soft) and/or power the adapter on."""
        from companion.core import capabilities
        state = await self.manager.adapter_state()
        if state == capabilities.AdapterState.SOFT_BLOCKED:
            await self._exec(self._rfkill_unblock)
        await self.manager.power_on_adapters()
        await self._refresh_adapter_banner()

    @staticmethod
    def _rfkill_unblock() -> None:
        import shutil
        import subprocess
        if shutil.which("rfkill") is None:
            return
        try:
            subprocess.run(["rfkill", "unblock", "bluetooth"],
                           timeout=8, check=False)
        except Exception:
            log.debug("rfkill unblock failed", exc_info=True)

    async def _refresh_adapter_banner(self) -> None:
        """Classify the adapter and surface exactly one honest banner state."""
        from companion.core import capabilities
        try:
            state = await self.manager.adapter_state()
        except Exception:
            log.debug("adapter_state failed", exc_info=True)
            return
        label = ""
        if state == capabilities.AdapterState.POWERED_OFF:
            label = "TURN ON"
        elif state == capabilities.AdapterState.SOFT_BLOCKED:
            label = "UNBLOCK"
        self.window.set_adapter_state(
            capabilities.adapter_message(state),
            capabilities.adapter_actionable(state),
            label)

    # ------------------------------------------------------------------ sound
    def _open_sound_dialog(self, path: str) -> None:
        self._spawn(self._sound_dialog_flow(path))

    async def _sound_dialog_flow(self, path: str) -> None:
        model = self.window.device_for(path)
        if model is None:
            return
        # Offer ONLY the codecs this device+stack can actually negotiate (PipeWire's
        # intersection of device caps and installed encoders), so the user is never
        # shown an impossible option — radical reduction over a settings sprawl.
        available = None
        if not self._audio_unavailable:
            try:
                dev_profiles, _sys = await self._exec(
                    pwcli.device_codec_profiles, model.address)
                available = codecs.available_codecs(dev_profiles) or None
            except pwcli.AudioToolMissing:
                self._audio_unavailable = True
            except Exception:
                log.debug("codec profile probe failed", exc_info=True)
        current = model.codec.name.lower().replace("-", "_") if model.codec else None
        dialog = SoundDialog(model.name, current_codec_key=current,
                             available_codecs=available, parent=self.window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        await self._apply_sound_flow(path, model, dialog.choice())

    def _apply_sound(self, model: Device, choice) -> bool:
        try:
            # Preference ramp: chosen codec first, then the rest as compatible fallback.
            allow = [choice.codec_key] + [
                k for k in codecs.all_keys() if k != choice.codec_key]
            wp.apply_global(allow)
            self._rules[model.address_underscored] = wp.DeviceRule(
                device_name_glob=f"bluez_card.{model.address_underscored}",
                nick=model.name,
                ldac_quality=choice.ldac_quality,
                aac_bitratemode=choice.aac_bitratemode,
            )
            wp.apply_rules(list(self._rules.values()))   # rewrite ALL rules merged
            return True
        except Exception:
            log.exception("failed to write WirePlumber policy")
            self._warn("Could not write audio policy",
                       "Check permissions on your WirePlumber config directory.")
            return False

    async def _apply_sound_flow(self, path: str, model: Device, choice) -> None:
        """Make a codec/quality change actually take effect, then sync the UI to the
        codec that REALLY negotiated.

        Per-device & independent: each A2DP device negotiates its own codec on its own
        node, so two devices in one Share group can run different codecs — changing one
        never touches the other (rules are kept merged, one per device).
        """
        if not self._apply_sound(model, choice):
            return
        # Prefer the LOW-LEVEL live path: selecting the device's per-codec PipeWire card
        # profile (a2dp-sink-<codec>) re-negotiates A2DP in place — no WirePlumber
        # restart, no Bluetooth reconnect, no audio drop on other outputs — and is
        # per-device, so each device keeps its own codec. Fall back to reload+reconnect
        # only when this PipeWire build exposes no per-codec profile for the choice.
        try:
            applied = await self._exec(
                pwcli.switch_codec_profile, model.address, choice.codec_key)
        except pwcli.AudioToolMissing:
            self._audio_unavailable = True
            self._warn("PipeWire tools missing",
                       "Install pipewire-pulse / wireplumber to change the codec.")
            return
        except Exception:
            log.exception("live codec profile switch failed for %s", path)
            applied = None
        if applied is None:
            # Older PipeWire (no per-codec profile): the drop-in policy is inert until
            # WirePlumber reloads, so restart it, THEN reconnect so the device
            # re-negotiates A2DP against the freshly loaded config.
            if not await self._exec(wp.reload):
                self._warn(
                    "Restart WirePlumber to apply",
                    "Couldn't auto-reload the audio service. Run:\n"
                    "    systemctl --user restart wireplumber\n"
                    "then reconnect the device.")
            try:
                await self._bluez(path).reconnect()
            except KeyError:
                return                   # device vanished mid-change — nothing to sync
            except Exception:
                log.exception("reconnect after codec change failed for %s", path)
        # Re-read the LIVE negotiated codec (+ volume/node) and synchronize the UI to
        # the codec that REALLY negotiated, not merely the one requested.
        try:
            dev = self._bluez(path)
        except KeyError:
            return
        await self._enrich(dev)
        negotiated = dev.model.codec.name if dev.model.codec else None
        if negotiated and not codecs.requested_codec_satisfied(
                choice.codec_key, negotiated):
            info = codecs.by_key(choice.codec_key)
            want = info.label if info else choice.codec_key.upper()
            try:
                dev_profiles, sys_profiles = await self._exec(
                    pwcli.device_codec_profiles, model.address)
            except Exception:
                dev_profiles, sys_profiles = [], None
            detail = codecs.diagnose_fallback(
                choice.codec_key, negotiated, dev_profiles, sys_profiles)
            self._warn(
                f"{model.name}: using {negotiated}, not {want}",
                detail or (f"The device or link couldn't negotiate {want}; the "
                           "next-best shared codec is in use. Other devices keep "
                           "their own codec."))

    async def _reconnect(self, path: str) -> None:
        try:
            await self._bluez(path).reconnect()
            await self._enrich(self._bluez(path))
        except Exception:
            log.exception("reconnect failed for %s", path)

    # ------------------------------------------------------------------ share
    async def _start_share(self, targets: list[ShareTarget]) -> None:
        """User pressed PLAY: record the DESIRED group, then build it live."""
        if not targets:
            await self._stop_share()
            return
        # Intent persists across a member disconnecting so it auto-rejoins on return.
        self._share_intent = {t.device_path: t for t in targets}
        await self._rebuild_share(user_initiated=True)

    async def _stop_share(self) -> None:
        await self._teardown_share(clear_intent=True)
        self._sync_share_ui()

    def _resolve_share_members(self, nodes) -> list[ShareTarget]:
        """From the desired intent, keep only members usable right now: a Bluetooth member
        must be connected AND expose a live sink; a local output (jack/HDMI) must still be
        present in the PipeWire graph. Never guess a node name."""
        resolved: list[ShareTarget] = []
        first = True
        for path, intent_t in self._share_intent.items():
            model = self.window.device_for(path)
            if model is None:
                continue
            if getattr(model, "is_local", False):
                node_name = model.node_name
                if not node_name or not any(n.name == node_name for n in nodes):
                    continue
                resolved.append(ShareTarget(
                    device_path=path, name=model.name, pipewire_node=node_name,
                    latency_offset_ms=intent_t.latency_offset_ms, is_primary=first))
                first = False
                continue
            if not model.connected:
                continue
            node = pwcli.resolve_sink(model.address, nodes)
            if node is None:
                log.info("share: %s connected but no live sink yet; excluding",
                         intent_t.name)
                continue
            resolved.append(ShareTarget(
                device_path=path, name=model.name, pipewire_node=node.name,
                latency_offset_ms=intent_t.latency_offset_ms, is_primary=first))
            first = False
        return resolved

    async def _rebuild_share(self, user_initiated: bool = False) -> None:
        """Reconcile the live combine sink with the desired group + backend state.

        Rebuild ONLY when the resolvable membership actually changes, so a member
        dropping collapses the group to the rest and a member returning auto-rejoins,
        with no needless audio interruptions.
        """
        if not self._share_intent:
            await self._teardown_share()
            self._sync_share_ui()
            return
        if self._share_resyncing:               # coalesce overlapping backend events
            self._share_resync_again = True
            return
        self._share_resyncing = True
        try:
            again = True
            while again:
                again = False
                try:
                    nodes = await self._exec(pwcli.dump_nodes)
                except pwcli.AudioToolMissing:
                    if user_initiated:
                        self._warn("PipeWire tools missing",
                                   "Install pipewire-pulse / wireplumber to use Share.")
                    return
                except Exception:
                    log.exception("pw-dump failed during share resync")
                    return
                resolved = self._resolve_share_members(nodes)
                desired = {t.device_path for t in resolved}
                _, changed = resolve_share_action(desired, self._share_members)
                if changed or (desired and self._combine_module is None):
                    await self._teardown_share(clear_intent=False)
                    if resolved:
                        idx = await self._exec(pwcli.start_combine, resolved)
                        if idx is None:
                            if user_initiated:
                                self._warn("Share failed",
                                           "Could not create the combined output.")
                        else:
                            self._combine_module = idx
                            self._share_members = desired
                            log.info("share: now playing on %d device(s): %s",
                                     len(desired), ", ".join(t.name for t in resolved))
                    elif user_initiated:
                        self._warn("Nothing to share",
                                   "Connect at least one audio device first.")
                self._sync_share_ui()
                if self._share_resync_again:    # a backend event arrived mid-rebuild
                    self._share_resync_again = False
                    again = True
        finally:
            self._share_resyncing = False

    async def _teardown_share(self, clear_intent: bool = True) -> None:
        if self._combine_module is not None:
            await self._exec(pwcli.stop_combine, self._combine_module)
            self._combine_module = None
        self._share_members = set()
        if clear_intent:
            self._share_intent = {}

    def _sync_share_ui(self) -> None:
        self.window.share.set_active_members(self._share_members)
        self._refresh_tray()

    # ------------------------------------------------------------- pairing UI
    def _confirm_pair(self, device_path: str, passkey: int) -> bool:
        if passkey == -1:
            text = "Allow this device to pair?"
        else:
            text = f"Confirm passkey {passkey:06d}?"
        reply = QMessageBox.question(
            self.window, "Pairing", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def _display_passkey(self, device_path: str, passkey: int) -> None:
        QMessageBox.information(
            self.window, "Pairing",
            f"Enter this passkey on the device:\n\n{passkey:06d}")

    # -------------------------------------------------------------- tray glue
    def _tray_sound(self) -> None:
        path = self._primary_path()
        if path:
            self._open_sound_dialog(path)

    def _focus_share(self) -> None:
        # Pick up a freshly-plugged headphone jack / HDMI output before showing Share.
        self._spawn(self._refresh_local_outputs())
        self._raise_window()

    def _swap_device(self) -> None:
        paths = [d.path for d in self.manager.devices if d.model.connected]
        if not paths:
            return
        current = self.window.selected_path()
        nxt = paths[(paths.index(current) + 1) % len(paths)] if current in paths else paths[0]
        model = self.window.device_for(nxt)
        if model:
            self.window.detail.set_device(model)
        self._raise_window()

    def _raise_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    # --------------------------------------------------------------- helpers
    def _primary_path(self) -> Optional[str]:
        connected = [d.model for d in self.manager.devices if d.model.connected]
        return connected[0].path if connected else None

    async def _sync_tray_outputs(self, nodes=None) -> None:
        """Populate the tray's Output submenu with every selectable sink (Bluetooth +
        local), flagging the current default, so the user can switch output from the
        tray without opening the window."""
        if self.tray is None or self._audio_unavailable:
            return
        try:
            if nodes is None:
                nodes = await self._exec(pwcli.dump_nodes)
            default = await self._exec(pwcli.get_default_sink)
        except pwcli.AudioToolMissing:
            self._audio_unavailable = True
            return
        except Exception:
            log.debug("could not refresh tray outputs")
            return
        outputs = []
        for node in pwcli.list_output_sinks(nodes):
            label = node.description or node.name
            outputs.append((node.name, label, node.name == default))
        self.tray.set_outputs(outputs)

    async def _select_output(self, sink_name: str) -> None:
        """Make the chosen sink the system default output (pactl set-default-sink)."""
        ok = await self._exec(pwcli.set_default_sink, sink_name)
        if not ok:
            self._warn("Output", "Could not switch the default output.")
        await self._sync_tray_outputs()

    def _refresh_tray(self) -> None:
        if not self.tray:
            return
        connected = [d.model for d in self.manager.devices if d.model.connected]
        primary = connected[0] if connected else None
        self.tray.refresh(primary, live=bool(connected))
        self.tray.show()

    def _warn(self, title: str, body: str) -> None:
        QMessageBox.warning(self.window, title, body)

    async def _exec(self, func, *args):
        """Run a blocking CLI helper off the Qt/asyncio thread."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func, *args)

    @staticmethod
    def _spawn(coro) -> None:
        asyncio.ensure_future(coro)

    # ----------------------------------------------------------------- start
    async def start(self) -> None:
        try:
            await self.manager.connect()
        except Exception:
            log.exception("could not reach BlueZ on the system bus")
            self._warn("Bluetooth unavailable",
                       "Could not reach BlueZ on the system D-Bus. Is bluetoothd running?")
        # Sweep any combine sink left behind by a previous crash/quit before we begin.
        try:
            await self._exec(pwcli.stop_all_combines)
        except Exception:
            log.debug("combine cleanup at startup skipped")
        await self._refresh_adapter_banner()
        await self._refresh_local_outputs()
        # Poll local outputs so a freshly plugged jack / USB DAC / HDMI shows up in the
        # list and mixer live (PipeWire gives us no cheap "sink added" signal here).
        self._local_timer = QTimer()
        self._local_timer.setInterval(3000)
        self._local_timer.timeout.connect(
            lambda: self._spawn(self._refresh_local_outputs()))
        self._local_timer.start()
        self.window.show()
