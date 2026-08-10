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

from minmax.api import StreamKind, TrendMetric, TrendSeries
from minmax.ui import theme
from minmax.ui.trends_view import format_trend_amount

__all__ = ["TrendsLegend"]

ROW_HEIGHT = 30
SWATCH = 11
PADDING = 12

# Wide enough for a three-digit hour total ("250h 21m"), which a multi-month
# range reaches easily -- also enough room for a count in the low thousands.
DURATION_COLUMN = 82
SHARE_COLUMN = 56

_DEFAULT_METRIC = TrendMetric(label="")


class TrendsLegend(QWidget):
    """One row per series: swatch, label, share of the visible window, value.

    "Share" only means something for a stacking metric -- a mean has no whole
    for a share to be a fraction of, so :attr:`TrendMetric.stacks` switches
    the heading and drops the percentage column entirely rather than showing
    a number that would not add up to 100%.
    """

    seriesActivated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._series: list[TrendSeries] = []
        self._shares: list[float] = []
        self._values: list[float] = []
        self._stream_kind = StreamKind.INTERVAL
        self._metric = _DEFAULT_METRIC
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_series(
        self,
        series: list[TrendSeries],
        shares: list[float],
        values: list[float],
        *,
        stream_kind: StreamKind = StreamKind.INTERVAL,
        metric: TrendMetric = _DEFAULT_METRIC,
    ) -> None:
        self._series = series
        self._shares = shares
        self._values = values
        self._stream_kind = stream_kind
        self._metric = metric
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
        stacks = self._metric.stacks
        share_column = SHARE_COLUMN if stacks else 0

        heading = QFont(self.font())
        heading.setBold(True)
        painter.setFont(heading)
        painter.setPen(theme.subdued_text(appearance))
        painter.drawText(
            QRectF(PADDING, 6, self.width() - PADDING * 2, 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "SHARE OF VISIBLE RANGE" if stacks else "AVERAGE OVER VISIBLE RANGE",
        )
        painter.setFont(self.font())

        for index, series in enumerate(self._series):
            top = 32 + index * ROW_HEIGHT
            value = self._values[index] if index < len(self._values) else 0.0

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(series.swatch.for_surface(dark=dark)))
            painter.drawRoundedRect(
                QRectF(PADDING, top + (ROW_HEIGHT - SWATCH) / 2, SWATCH, SWATCH), 2.0, 2.0
            )

            # Text wears text tokens, never the series colour -- the swatch
            # beside it is what carries identity.
            painter.setPen(text_colour)
            label_left = PADDING + SWATCH + 8
            label_width = self.width() - label_left - share_column - DURATION_COLUMN - PADDING
            painter.drawText(
                QRectF(label_left, top, label_width, ROW_HEIGHT),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(series.label, Qt.TextElideMode.ElideMiddle, int(label_width)),
            )

            if stacks:
                share = self._shares[index] if index < len(self._shares) else 0.0
                share_font = QFont(self.font())
                share_font.setBold(True)
                painter.setFont(share_font)
                painter.drawText(
                    QRectF(
                        self.width() - DURATION_COLUMN - share_column - PADDING,
                        top,
                        share_column,
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
                format_trend_amount(value, self._metric, self._stream_kind),
            )

    def _series_at(self, y: float) -> TrendSeries | None:
        index = int((y - 32) // ROW_HEIGHT)
        if 0 <= index < len(self._series):
            return self._series[index]
        return None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        series = self._series_at(event.position().y())
        if series is not None and (series.can_drill or series.is_other):
            self.seriesActivated.emit(series.label)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        series = self._series_at(event.position().y())
        clickable = series is not None and (series.can_drill or series.is_other)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)
