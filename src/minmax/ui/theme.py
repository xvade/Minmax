"""Colours for the custom-painted views.

Everything here is derived from the widget's own ``QPalette`` rather than
hard-coded, so the app follows the system light/dark appearance automatically --
the same approach Timecard takes by building its UI from semantic system
colours. The only fixed colours in the app are the category colours themselves,
which come from :mod:`minmax.core.palette` and are chosen to read against either
background.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

__all__ = [
    "block_border",
    "grid_line",
    "gutter_text",
    "is_dark",
    "subdued_text",
    "surface",
]


def is_dark(palette: QPalette) -> bool:
    """Whether the current appearance is a dark one."""
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def surface(palette: QPalette) -> QColor:
    """Background behind the timeline."""
    return palette.color(QPalette.ColorRole.Base)


def grid_line(palette: QPalette) -> QColor:
    """Hour gridlines: present, but never competing with the blocks."""
    colour = QColor(palette.color(QPalette.ColorRole.Text))
    colour.setAlpha(40 if is_dark(palette) else 30)
    return colour


def gutter_text(palette: QPalette) -> QColor:
    colour = QColor(palette.color(QPalette.ColorRole.Text))
    colour.setAlpha(150)
    return colour


def subdued_text(palette: QPalette) -> QColor:
    colour = QColor(palette.color(QPalette.ColorRole.Text))
    colour.setAlpha(110)
    return colour


def block_border(palette: QPalette) -> QColor:
    """Hairline between abutting blocks, so a run boundary stays visible.

    Real days are almost entirely contiguous -- 325 of 337 adjacent runs in the
    first export share an exact boundary -- so without a separator a stretch of
    same-coloured runs would read as one long block.
    """
    colour = QColor(palette.color(QPalette.ColorRole.Base))
    colour.setAlpha(90)
    return colour
