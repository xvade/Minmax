"""Launching the desktop app.

Kept separate from :mod:`minmax.ui.main_window` so that constructing a window
and running an event loop are independent -- tests build windows without ever
starting one.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from minmax.api import MinmaxClient
from minmax.ui.main_window import MainWindow

__all__ = ["run"]


def run(client: MinmaxClient, *, stream_key: str = "timecard") -> int:
    """Show the window and run the event loop until it closes.

    An existing ``QApplication`` is reused if one is already running, so this
    can be called from a host that owns the event loop itself.
    """
    app = QApplication.instance() or QApplication([])

    window = MainWindow(client, stream_key=stream_key)
    window.show()
    window.raise_()
    window.activateWindow()

    return int(app.exec())
