"""Launching the desktop app.

Kept separate from :mod:`minmax.ui.main_window` so that constructing a window
and running an event loop are independent -- tests build windows without ever
starting one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from minmax.api import MinmaxClient
from minmax.ui.main_window import MainWindow

__all__ = ["run"]


def run(
    client: MinmaxClient,
    *,
    stream_key: str = "timecard",
    config_path: Path | None = None,
    view_key: str | None = None,
) -> int:
    """Show the window and run the event loop until it closes.

    An existing ``QApplication`` is reused if one is already running, so this
    can be called from a host that owns the event loop itself.

    ``config_path`` is where remapped shortcuts live. It is passed in rather
    than computed here because working out the data directory is the storage
    layer's job, and the UI is not permitted to ask it. ``view_key``, when
    given, must already be a real view -- the caller (``cli.py``'s
    ``cmd_ui``) checks that before this ever constructs a window, since a bad
    key here would raise while building it rather than failing cleanly.
    """
    app = QApplication.instance() or QApplication([])

    window = MainWindow(client, stream_key=stream_key, config_path=config_path, view_key=view_key)
    window.show()
    window.raise_()
    window.activateWindow()

    return int(app.exec())
