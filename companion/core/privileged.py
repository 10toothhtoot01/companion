"""polkit-gated privileged writes.

Most of Companion writes only to the user's own ~/.config (no privilege needed). For the
rare system-wide change (e.g. enabling bluetoothd experimental LE-Audio, or writing to
/etc), we go through polkit rather than running the whole app as root.

This module is a thin, testable boundary: it builds the pkexec command but does not run
it unless explicitly asked, so unit tests can assert the command without side effects.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

ACTION_ID = "org.companion.bluetooth.write-system-config"


@dataclass(frozen=True)
class PrivilegedWrite:
    target_path: str
    contents: str

    def pkexec_command(self, helper: str = "companion-config-helper") -> list[str]:
        """Build the pkexec invocation for the bundled helper.

        The helper (installed separately, owned by the polkit action) validates the
        target path against an allow-list before writing. We never pass arbitrary shell.
        """
        return ["pkexec", helper, "--write", self.target_path]

    def run(self, helper: str = "companion-config-helper") -> int:
        if shutil.which("pkexec") is None:
            raise RuntimeError("pkexec not available; cannot perform privileged write")
        proc = subprocess.run(
            self.pkexec_command(helper),
            input=self.contents.encode("utf-8"),
            capture_output=True,
        )
        return proc.returncode
