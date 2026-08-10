"""Legend for the trends chart, carrying each group's share of the visible days.

A legend is always present for two or more series -- identity must never be
carried by colour alone. It doubles as the relief for the light-mode contrast
warning on three palette steps: those hues are always accompanied by a visible
text label here.

Percentages describe the days currently on screen and update as the reader
scrolls or zooms, which is what makes the chart answerable rather than
decorative.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from minmax.api import StreamKind, TrendSeries
from minmax.api.format import format_measure
from minmax.ui import theme

__all__ = ["TrendsLegend"]

ROW_HEIGHT = 30
SWATCH = 11
PADDING = 12

# Wide enough for a three-digit hour total ("250h 21m"), which a multi-month
# range reaches easily -- also enough room for a count in the low thousands.
DURATION_COLUMN = 82
SHARE_COLUMN = 56


class TrendsLegend(QWidget):
    """One row per series: swatch, label, share of the visible window, total."""

    seriesActivated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series: list[TrendSeries] = []
        self._shares: list[float] = []
        self._totals: list[int] = []
        self._stream_kind = StreamKind.INTERVAL
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_series(
        self,
        series: list[TrendSeries],
        shares: list[float],
        totals: list[int],
        *,
        stream_kind: StreamKind = StreamKind.INTERVAL,
    ) -> None:
        self._series = series
        self._shares = shares
        self._totals = totals
        self._stream_kind = stream_kind
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(320, ROW_HEIGHT * max(1, len(self._series)) + 40)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        painter.fillRect(event.rect(), theme.surface(appearance))

        if not self._series:
            painter.setPen(theme.subdued_text(appearance))
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "No data yet")
            return

        dark = theme.is_dark(appearance)
        text_colour = appearance.color(appearance.currentColorGroup(), appearance.ColorRole.Text)
        metrics = QFontMetrics(self.font())

        heading = QFont(self.font())
        heading.setBold(True)
        painter.setFont(heading)
        painter.setPen(theme.subdued_text(appearance))
        painter.drawText(
            QRectF(PADDING, 6, self.width() - PADDING * 2, 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "SHARE OF VISIBLE RANGE",
        )
        painter.setFont(self.font())

        for index, series in enumerate(self._series):
            top = 32 + index * ROW_HEIGHT
            share = self._shares[index] if index < len(self._shares) else 0.0
            total = self._totals[index] if index < len(self._totals) else 0

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(series.swatch.for_surface(dark=dark)))
            painter.drawRoundedRect(
                QRectF(PADDING, top + (ROW_HEIGHT - SWATCH) / 2, SWATCH, SWATCH), 2.0, 2.0
            )

            # Text wears text tokens, never the series colour -- the swatch
            # beside it is what carries identity.
            painter.setPen(text_colour)
            label_left = PADDING + SWATCH + 8
            label_width = self.width() - label_left - SHARE_COLUMN - DURATION_COLUMN - PADDING
            painter.drawText(
                QRectF(label_left, top, label_width, ROW_HEIGHT),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(series.label, Qt.TextElideMode.ElideMiddle, int(label_width)),
            )

            share_font = QFont(self.font())
            share_font.setBold(True)
            painter.setFont(share_font)
            painter.drawText(
                QRectF(
                    self.width() - DURATION_COLUMN - SHARE_COLUMN - PADDING,
                    top,
                    SHARE_COLUMN,
                    ROW_HEIGHT,
                ),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                f"{share:.1%}",
            )

            painter.setFont(self.font())
            painter.setPen(theme.subdued_text(appearance))
            painter.drawText(
                QRectF(self.width() - DURATION_COLUMN - PADDING, top, DURATION_COLUMN, ROW_HEIGHT),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                format_measure(total, self._stream_kind),
            )

    def _series_at(self, y: float) -> TrendSeries | None:
        index = int((y - 32) // ROW_HEIGHT)
        if 0 <= index < len(self._series):
            return self._series[index]
        return None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        series = self._series_at(event.position().y())
        if series is not None and series.can_drill and not series.is_other:
            self.seriesActivated.emit(series.label)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        series = self._series_at(event.position().y())
        drillable = series is not None and series.can_drill and not series.is_other
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if drillable else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)
