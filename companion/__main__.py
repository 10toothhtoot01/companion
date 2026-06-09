"""Entry point. Runs the Qt app on top of an asyncio loop via qasync."""
from __future__ import annotations

import asyncio
import logging
import sys


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        import qasync
    except ImportError:
        sys.stderr.write(
            "qasync is required. Install dependencies: pip install -r requirements.txt\n"
        )
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from companion.app import CompanionApp

    # Fractional display scaling (e.g. GNOME at 125%/150%) otherwise rounds the
    # scale factor and clips the app's fixed-size widgets and tracked text.
    # PassThrough keeps geometry crisp. Must be set before QApplication exists.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    qapp = QApplication.instance() or QApplication(sys.argv)
    loop = qasync.QEventLoop(qapp)
    asyncio.set_event_loop(loop)

    app = CompanionApp()
    with loop:
        loop.create_task(app.start())
        return loop.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
