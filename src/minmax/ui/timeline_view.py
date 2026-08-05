"""The day timeline: one local day drawn as a column of blocks.

Modelled on Timecard's history screen. Blocks are sized to their real duration
so they tile without overlapping, are coloured by their top-level group, carry
their name once tall enough and their length once there is room for both, and a
run crossing midnight is marked at the edge rather than pretending to end there.

All of the arithmetic lives in :mod:`minmax.core.timeline`, which has no Qt
dependency and is unit-tested directly. This module is deliberately only paint
and input handling.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QScrollArea, QSizePolicy, QToolTip, QWidget

from minmax.api import DayView, TimelineRecord
from minmax.api.format import format_duration
from minmax.core import palette as category_palette
from minmax.core.timeline import (
    DEFAULT_PIXELS_PER_HOUR,
    DayGeometry,
    LaidOutBlock,
    anchored_scroll_offset,
    clamp_zoom,
    lay_out_day,
)
from minmax.ui import theme

__all__ = ["DayTimeline", "TimelinePane"]

GUTTER_WIDTH = 52
BLOCK_INSET = 6
CORNER_RADIUS = 3.0
TEXT_PADDING = 6


class DayTimeline(QWidget):
    """Paints a single day. Height grows with the zoom level."""

    recordHovered = Signal(object)  # TimelineRecord | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view: DayView | None = None
        self._pixels_per_hour = DEFAULT_PIXELS_PER_HOUR
        self._blocks: list[LaidOutBlock] = []
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # -- state -----------------------------------------------------------

    @property
    def pixels_per_hour(self) -> float:
        return self._pixels_per_hour

    def set_day(self, view: DayView) -> None:
        self._view = view
        self._relayout()

    def set_zoom(self, pixels_per_hour: float) -> None:
        clamped = clamp_zoom(pixels_per_hour)
        if clamped == self._pixels_per_hour:
            return
        self._pixels_per_hour = clamped
        self._relayout()

    def day_geometry(self) -> DayGeometry | None:
        if self._view is None:
            return None
        return DayGeometry(
            day_start_epoch=self._view.day_start_epoch,
            day_end_epoch=self._view.day_end_epoch,
            pixels_per_hour=self._pixels_per_hour,
        )

    def _relayout(self) -> None:
        geometry = self.day_geometry()
        if geometry is None or self._view is None:
            self._blocks = []
        else:
            self._blocks = lay_out_day(
                [(record.start_epoch, record.end_epoch) for record in self._view.records],
                geometry,
            )
        self.updateGeometry()
        self.update()

    # -- sizing ----------------------------------------------------------

    def sizeHint(self) -> QSize:
        geometry = self.day_geometry()
        height = int(geometry.height) if geometry is not None else 0
        return QSize(GUTTER_WIDTH + 240, height)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        painter.fillRect(event.rect(), theme.surface(appearance))

        geometry = self.day_geometry()
        if geometry is None or self._view is None:
            return

        self._paint_hour_grid(painter, geometry)
        for block in self._blocks:
            self._paint_block(painter, block, self._view.records[block.index])

    def _paint_hour_grid(self, painter: QPainter, geometry: DayGeometry) -> None:
        if self._view is None:
            return
        appearance = self.palette()
        # Thin the gridlines when zoomed out so they do not turn into a smear.
        every = 1 if self._pixels_per_hour >= 40 else 2 if self._pixels_per_hour >= 30 else 3

        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
        painter.setFont(font)

        for mark in self._view.hour_marks[::every]:
            y = geometry.y_for_epoch(mark.epoch)
            painter.setPen(theme.grid_line(appearance))
            painter.drawLine(GUTTER_WIDTH, int(y), self.width(), int(y))

            painter.setPen(theme.gutter_text(appearance))
            # Clamped so the midnight label is not half-cut by the top edge.
            painter.drawText(
                QRectF(0, max(0.0, y - 8), GUTTER_WIDTH - 8, 16),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                mark.label,
            )

    def _paint_block(self, painter: QPainter, block: LaidOutBlock, record: TimelineRecord) -> None:
        rect = self._block_rect(block)
        hex_colour = record.swatch.for_surface(dark=theme.is_dark(self.palette()))
        colour = QColor(hex_colour)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

        # Hairline separators, so abutting same-coloured runs stay distinct.
        painter.setPen(theme.block_border(self.palette()))
        painter.drawLine(int(rect.left()), int(rect.top()), int(rect.right()), int(rect.top()))

        self._paint_continuation_marks(painter, block, rect)

        if not block.show_label:
            return

        painter.setPen(QColor(category_palette.text_colour_on(hex_colour)))
        text_rect = rect.adjusted(TEXT_PADDING, 1, -TEXT_PADDING, -1)

        if block.show_duration:
            duration = format_duration(record.duration_seconds)
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
                duration,
            )
            text_rect = text_rect.adjusted(
                0, 0, -QFontMetrics(painter.font()).horizontalAdvance(duration) - TEXT_PADDING, 0
            )

        metrics = QFontMetrics(painter.font())
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            metrics.elidedText(
                record.recorded_path, Qt.TextElideMode.ElideMiddle, int(text_rect.width())
            ),
        )

    def _paint_continuation_marks(
        self, painter: QPainter, block: LaidOutBlock, rect: QRectF
    ) -> None:
        """Mark a run that extends past this day rather than clipping silently.

        Timecard keeps a midnight-crossing run as one block with the day
        boundary drawn through it; here the day is the whole view, so the
        overhang is shown as a tick at the edge it continues past.
        """
        if not (block.continues_before or block.continues_after):
            return

        pen = painter.pen()
        marker = QColor(theme.surface(self.palette()))
        marker.setAlpha(200)
        painter.setPen(marker)
        for should_mark, y in (
            (block.continues_before, rect.top() + 2),
            (block.continues_after, rect.bottom() - 2),
        ):
            if should_mark:
                for offset in range(0, int(rect.width()), 10):
                    start = int(rect.left() + offset)
                    painter.drawLine(start, int(y), start + 5, int(y))
        painter.setPen(pen)

    def _block_rect(self, block: LaidOutBlock) -> QRectF:
        return QRectF(
            GUTTER_WIDTH + BLOCK_INSET,
            block.top,
            max(0.0, self.width() - GUTTER_WIDTH - BLOCK_INSET * 2),
            max(1.0, block.height),
        )

    # -- interaction -----------------------------------------------------

    def record_at(self, position: QPoint) -> TimelineRecord | None:
        if self._view is None:
            return None
        for block in self._blocks:
            if self._block_rect(block).contains(position.x(), position.y()):
                return self._view.records[block.index]
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        record = self.record_at(event.position().toPoint())
        self.recordHovered.emit(record)
        if record is not None:
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{record.recorded_path}\n"
                f"{record.start_label}-{record.end_label}  "
                f"{format_duration(record.duration_seconds)}",
                self,
            )
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)


class TimelinePane(QScrollArea):
    """Scroll container that owns zooming, so zoom can stay anchored."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.timeline = DayTimeline(self)
        self.setWidget(self.timeline)
        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def set_day(self, view: DayView) -> None:
        self.timeline.set_day(view)

    def zoom_by(self, factor: float, anchor_y: float | None = None) -> None:
        """Multiply the zoom, keeping the instant under ``anchor_y`` in place.

        ``anchor_y`` is in viewport coordinates; without it, the centre of the
        viewport is held instead, which is what keyboard zoom should do.
        """
        bar = self.verticalScrollBar()
        old_zoom = self.timeline.pixels_per_hour
        new_zoom = clamp_zoom(old_zoom * factor)
        if new_zoom == old_zoom:
            return

        anchor = anchor_y if anchor_y is not None else self.viewport().height() / 2
        offset = anchored_scroll_offset(bar.value(), anchor, old_zoom, new_zoom)

        self.timeline.set_zoom(new_zoom)
        # The scrollbar's range only updates once the widget has resized, so
        # apply the new offset after layout has caught up.
        self.timeline.adjustSize()
        bar.setValue(round(offset))

    def reset_zoom(self) -> None:
        self.timeline.set_zoom(DEFAULT_PIXELS_PER_HOUR)

    def fit_day(self) -> None:
        """Zoom so the whole day fits the viewport.

        A desktop window is tall enough to show a full day at once, which is the
        view that answers "where did today go?" without scrolling. Zooming in is
        for inspecting short runs, so this is what the window opens at.
        """
        geometry = self.timeline.day_geometry()
        if geometry is None or geometry.seconds <= 0:
            return
        hours = geometry.seconds / 3600
        self.timeline.set_zoom(self.viewport().height() / hours)
        self.timeline.adjustSize()
        self.verticalScrollBar().setValue(0)

    def scroll_to_epoch(self, epoch_seconds: int) -> None:
        """Centre the viewport on an instant, e.g. "now" when opening today."""
        geometry = self.timeline.day_geometry()
        if geometry is None:
            return
        target = geometry.y_for_epoch(epoch_seconds) - self.viewport().height() / 2
        self.verticalScrollBar().setValue(round(target))

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.zoom_by(1.15**steps, anchor_y=event.position().y())
            event.accept()
            return
        super().wheelEvent(event)
