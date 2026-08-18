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
    "chip_background",
    "day_rule",
    "day_rule_text",
    "excluded_time",
    "grid_line",
    "gutter_text",
    "is_dark",
    "selection_ring",
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


def day_rule(palette: QPalette) -> QColor:
    """The line at local midnight on a continuously scrolling timeline.

    Deliberately much stronger than :func:`grid_line`: with no page boundary to
    separate one day from the next, this is the only thing that says where the
    reader is, and it has to stay legible drawn across a saturated block.
    """
    colour = QColor(palette.color(QPalette.ColorRole.Text))
    colour.setAlpha(180)
    return colour


def day_rule_text(palette: QPalette) -> QColor:
    return palette.color(QPalette.ColorRole.Text)


def chip_background(palette: QPalette) -> QColor:
    """Backing for a label that must stay readable over arbitrary content."""
    colour = QColor(palette.color(QPalette.ColorRole.Base))
    colour.setAlpha(235)
    return colour


def selection_ring(palette: QPalette) -> QColor:
    """Outline on the selected block. Uses the system accent, as a selection should."""
    return palette.color(QPalette.ColorRole.Highlight)


def excluded_time(palette: QPalette) -> QColor:
    """Wash over a stretch a saved view was not active in.

    Deliberately lighter than :func:`grid_line`: the stretch genuinely
    occurred and is genuinely empty (see ``DayView.active_spans``'s
    docstring), so this is a legibility affordance for telling "outside the
    view" apart from "a quiet day", not a claim that the time was cut out --
    it must not read as louder than the blocks drawn on top of it.
    """
    colour = QColor(palette.color(QPalette.ColorRole.Text))
    colour.setAlpha(16 if is_dark(palette) else 12)
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
