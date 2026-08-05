"""Category totals for whatever the timeline is showing.

Each row is a category, its share drawn as a bar behind the text in the
category's own colour, so the panel reads as a breakdown at a glance and as
exact figures on inspection.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent
from PySide6.QtWidgets import QScrollArea, QSizePolicy, QWidget

from minmax.api import CategorySlice
from minmax.api.format import format_duration
from minmax.ui import theme

__all__ = ["SummaryPanel"]

ROW_HEIGHT = 26
SWATCH_WIDTH = 4
PADDING = 10
DURATION_COLUMN = 78


class _SummaryRows(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._slices: list[CategorySlice] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_slices(self, slices: list[CategorySlice]) -> None:
        # Largest first: the answer to "where did the day go" should be the top
        # line, not something to scan for.
        self._slices = sorted(slices, key=lambda item: item.seconds, reverse=True)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(240, ROW_HEIGHT * max(1, len(self._slices)))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        painter.fillRect(event.rect(), theme.surface(appearance))

        if not self._slices:
            painter.setPen(theme.subdued_text(appearance))
            painter.drawText(
                self.rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "Nothing tracked",
            )
            return

        dark = theme.is_dark(appearance)
        metrics = QFontMetrics(self.font())
        for index, item in enumerate(self._slices):
            top = index * ROW_HEIGHT
            row = QRectF(0, top, self.width(), ROW_HEIGHT)

            # Share bar, kept faint so the text stays the primary content.
            hex_colour = item.swatch.for_surface(dark=dark)
            bar = QColor(hex_colour)
            bar.setAlpha(56)
            painter.fillRect(QRectF(0, top + 2, row.width() * item.share, ROW_HEIGHT - 4), bar)

            painter.fillRect(QRectF(0, top + 4, SWATCH_WIDTH, ROW_HEIGHT - 8), QColor(hex_colour))

            painter.setPen(
                appearance.color(appearance.currentColorGroup(), appearance.ColorRole.Text)
            )
            text_width = row.width() - SWATCH_WIDTH - PADDING * 2 - DURATION_COLUMN
            painter.drawText(
                QRectF(SWATCH_WIDTH + PADDING, top, text_width, ROW_HEIGHT),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(
                    item.category_path, Qt.TextElideMode.ElideMiddle, int(text_width)
                ),
            )

            painter.drawText(
                QRectF(row.width() - DURATION_COLUMN - PADDING, top, DURATION_COLUMN, ROW_HEIGHT),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                format_duration(item.seconds),
            )


class SummaryPanel(QScrollArea):
    """Scrollable list of category totals with a header showing the grand total."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = _SummaryRows(self)
        self.setWidget(self._rows)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(240)

    def set_slices(self, slices: list[CategorySlice]) -> None:
        self._rows.set_slices(slices)


def heading_font(base: QFont) -> QFont:
    font = QFont(base)
    font.setBold(True)
    return font
