"""The timeline: a continuous column of blocks, scrolling across midnight.

Modelled on Timecard's history screen. Blocks are sized to their real duration
so they tile without overlapping, are coloured by their top-level group, carry
their name once tall enough and their length once there is room for both.

The view holds a *window* of consecutive days rather than a single one, and asks
for more as the reader approaches either end. That is what lets a run crossing
midnight be drawn as the single run it is, with the day boundary ruled through
it, instead of being clipped at 00:00 and marked as continuing somewhere the
reader cannot scroll to. Continuation marks remain, but now only fire at the
outer edges of everything loaded.

All of the arithmetic lives in :mod:`minmax.core.timeline`, which has no Qt
dependency and is unit-tested directly. This module is deliberately only paint
and input handling.
"""

from __future__ import annotations

import bisect
from datetime import date

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

from minmax.api import TimelineRecord, TimelineWindow
from minmax.api.format import format_duration
from minmax.core import palette as category_palette
from minmax.core.timeline import (
    DEFAULT_PIXELS_PER_HOUR,
    LaidOutBlock,
    TimelineGeometry,
    anchored_scroll_offset,
    clamp_zoom,
    lay_out_blocks,
)
from minmax.ui import theme

__all__ = ["Timeline", "TimelinePane"]

GUTTER_WIDTH = 52
BLOCK_INSET = 6
CORNER_RADIUS = 3.0
TEXT_PADDING = 6
DAY_LABEL_HEIGHT = 18.0
MARKER_DIAMETER = 9.0

#: How close to an end the viewport must come before more days are requested,
#: as a multiple of the viewport height. One full screen of runway means the
#: next days are already loaded by the time they would be scrolled into view.
EDGE_TRIGGER = 1.0


class Timeline(QWidget):
    """Paints a window of days. Height grows with the zoom level."""

    recordHovered = Signal(object)  # TimelineRecord | None
    recordSelected = Signal(object)  # TimelineRecord | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window: TimelineWindow | None = None
        self._pixels_per_hour = DEFAULT_PIXELS_PER_HOUR
        self._blocks: list[LaidOutBlock] = []
        #: ``self._blocks[i].top``, kept alongside for :meth:`_visible_block_range`.
        self._block_tops: list[float] = []
        #: The tallest block currently laid out, plus a pixel for the minimum
        #: render height a near-zero block is clamped to (see ``_block_rect``).
        self._max_block_height: float = 0.0
        self._selected_id: int | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # -- state -----------------------------------------------------------

    @property
    def pixels_per_hour(self) -> float:
        return self._pixels_per_hour

    @property
    def window_view(self) -> TimelineWindow | None:
        return self._window

    @property
    def selected_id(self) -> int | None:
        return self._selected_id

    def set_window(self, window: TimelineWindow) -> None:
        self._window = window
        # A selection that is no longer loaded would paint nowhere and confuse
        # the inspector, so it goes when the record leaves the window.
        if self._selected_id is not None and not any(
            record.id == self._selected_id for record in window.records
        ):
            self._selected_id = None
            self.recordSelected.emit(None)
        self._relayout()

    def set_zoom(self, pixels_per_hour: float) -> None:
        clamped = clamp_zoom(pixels_per_hour)
        if clamped == self._pixels_per_hour:
            return
        self._pixels_per_hour = clamped
        self._relayout()

    def select(self, record: TimelineRecord | None) -> None:
        new_id = record.id if record is not None else None
        if new_id == self._selected_id:
            return
        self._selected_id = new_id
        self.recordSelected.emit(record)
        self.update()

    def selected_record(self) -> TimelineRecord | None:
        if self._window is None or self._selected_id is None:
            return None
        for record in self._window.records:
            if record.id == self._selected_id:
                return record
        return None

    def geometry_model(self) -> TimelineGeometry | None:
        if self._window is None:
            return None
        return TimelineGeometry(
            start_epoch=self._window.start_epoch,
            end_epoch=self._window.end_epoch,
            pixels_per_hour=self._pixels_per_hour,
        )

    def _relayout(self) -> None:
        geometry = self.geometry_model()
        if geometry is None or self._window is None:
            self._blocks = []
        else:
            self._blocks = lay_out_blocks(
                [(record.start_epoch, record.end_epoch) for record in self._window.records],
                geometry,
            )
        # Records arrive ordered by start time (see Repository.intervals_in_range
        # / events_in_range), and a block's top is a monotonic function of its
        # record's start, so self._blocks is already sorted by top -- which is
        # what lets _visible_block_range bisect instead of scanning.
        self._block_tops = [block.top for block in self._blocks]
        self._max_block_height = max((block.height for block in self._blocks), default=0.0) + 1.0
        self.updateGeometry()
        self.update()

    def _visible_block_range(self, top: float, bottom: float) -> tuple[int, int]:
        """Index range of ``self._blocks`` that can possibly touch ``[top, bottom]``.

        A busy point-in-time stream (a heart-rate or location feed sampled every
        few minutes) can load tens of thousands of markers into a window; without
        this, painting and hit-testing both walked every one of them on every
        paint or pointer move, most of which sit far outside the small sliver
        actually visible or under the cursor. Blocks are sorted by top (see
        ``_relayout``), so the candidates are one contiguous slice, found in
        O(log n) rather than O(n).
        """
        lo = bisect.bisect_left(self._block_tops, top - self._max_block_height)
        hi = bisect.bisect_right(self._block_tops, bottom)
        return lo, hi

    # -- sizing ----------------------------------------------------------

    def sizeHint(self) -> QSize:
        geometry = self.geometry_model()
        height = int(geometry.height) if geometry is not None else 0
        return QSize(GUTTER_WIDTH + 240, height)

    # -- painting --------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        painter.fillRect(event.rect(), theme.surface(appearance))

        geometry = self.geometry_model()
        if geometry is None or self._window is None:
            return

        self._paint_hour_grid(painter, geometry)
        lo, hi = self._visible_block_range(event.rect().top(), event.rect().bottom())
        for block in self._blocks[lo:hi]:
            self._paint_block(painter, block, self._window.records[block.index])
        # Day rules go over the blocks: the boundary must stay visible through a
        # run that crosses it, which is the whole point of not clipping there.
        self._paint_day_rules(painter, geometry)
        self._paint_selection(painter, lo, hi)

    def _paint_hour_grid(self, painter: QPainter, geometry: TimelineGeometry) -> None:
        if self._window is None:
            return
        appearance = self.palette()
        # Thin the gridlines when zoomed out so they do not turn into a smear.
        every = 1 if self._pixels_per_hour >= 40 else 2 if self._pixels_per_hour >= 30 else 3

        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
        painter.setFont(font)

        for mark in self._window.hour_marks[::every]:
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

    def _paint_day_rules(self, painter: QPainter, geometry: TimelineGeometry) -> None:
        """Rule and name each local midnight.

        With continuous scrolling the day boundary is the only thing telling the
        reader where they are, so it is drawn over everything else and carries
        its own chip of surface so a block cannot swallow the date.
        """
        if self._window is None:
            return
        appearance = self.palette()
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 0.5))
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        for mark in self._window.day_marks:
            y = geometry.y_for_epoch(mark.epoch)
            if y < 0 or y > geometry.height:
                continue

            # Starts at the gutter, not at zero: a rule drawn across the whole
            # width strikes through the "00:00" label sitting on the same line.
            painter.setPen(theme.day_rule(appearance))
            painter.drawLine(GUTTER_WIDTH, int(y), self.width(), int(y))

            width = metrics.horizontalAdvance(mark.label) + 14
            chip = QRectF(GUTTER_WIDTH + BLOCK_INSET, y + 2, width, DAY_LABEL_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(theme.chip_background(appearance)))
            painter.drawRoundedRect(chip, 3.0, 3.0)

            painter.setPen(theme.day_rule_text(appearance))
            painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), mark.label)

    def _paint_block(self, painter: QPainter, block: LaidOutBlock, record: TimelineRecord) -> None:
        if block.is_marker:
            self._paint_marker(painter, block, record)
            return

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

    def _paint_marker(self, painter: QPainter, block: LaidOutBlock, record: TimelineRecord) -> None:
        """A point-in-time record: a small dot at its instant, not a block.

        There is no duration to size a rectangle from, so drawing one would
        either be misleadingly wide (the fixed marker height stretched full
        width) or need an arbitrary width with no meaning. The hit-test rect
        (:meth:`_block_rect`, same as a block's) still spans the full row, so
        the dot stays comfortably clickable despite reading as a moment.
        """
        appearance = self.palette()
        hex_colour = record.swatch.for_surface(dark=theme.is_dark(appearance))
        center_y = block.top + block.height / 2
        center_x = GUTTER_WIDTH + BLOCK_INSET + MARKER_DIAMETER / 2
        dot = QRectF(
            center_x - MARKER_DIAMETER / 2,
            center_y - MARKER_DIAMETER / 2,
            MARKER_DIAMETER,
            MARKER_DIAMETER,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(hex_colour)))
        painter.drawEllipse(dot)

        if not block.show_label:
            return

        label = record.recorded_path
        if record.fields:
            label += "  ·  " + "  ".join(f"{name}: {value}" for name, value in record.fields)

        text_left = dot.right() + TEXT_PADDING
        text_rect = QRectF(
            text_left, block.top, self.width() - text_left - TEXT_PADDING, block.height
        )
        painter.setPen(theme.gutter_text(appearance))
        metrics = QFontMetrics(painter.font())
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(text_rect.width())),
        )

    def _paint_continuation_marks(
        self, painter: QPainter, block: LaidOutBlock, rect: QRectF
    ) -> None:
        """Mark a run that extends past everything loaded.

        Inside the window a run crossing midnight needs no mark at all -- it is
        drawn whole, with the day rule through it. These ticks now mean "there
        is more of this run than has been loaded", which is only true at the two
        outer edges.
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

    def _paint_selection(self, painter: QPainter, lo: int, hi: int) -> None:
        if self._window is None or self._selected_id is None:
            return
        for block in self._blocks[lo:hi]:
            if self._window.records[block.index].id != self._selected_id:
                continue
            pen = painter.pen()
            pen.setColor(theme.selection_ring(self.palette()))
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                self._block_rect(block).adjusted(1, 1, -1, -1), CORNER_RADIUS, CORNER_RADIUS
            )
            return

    def _block_rect(self, block: LaidOutBlock) -> QRectF:
        return QRectF(
            GUTTER_WIDTH + BLOCK_INSET,
            block.top,
            max(0.0, self.width() - GUTTER_WIDTH - BLOCK_INSET * 2),
            max(1.0, block.height),
        )

    # -- interaction -----------------------------------------------------

    def record_at(self, position: QPoint) -> TimelineRecord | None:
        if self._window is None:
            return None
        lo, hi = self._visible_block_range(position.y(), position.y())
        for block in self._blocks[lo:hi]:
            if self._block_rect(block).contains(position.x(), position.y()):
                return self._window.records[block.index]
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.select(self.record_at(event.position().toPoint()))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        record = self.record_at(event.position().toPoint())
        self.recordHovered.emit(record)
        if record is not None:
            QToolTip.showText(event.globalPosition().toPoint(), _tooltip_text(record), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)


class TimelinePane(QScrollArea):
    """Scroll container that owns zooming and asks for more days at the edges."""

    #: The window needs extending; the screen owns loading, this only asks.
    needsEarlier = Signal()
    needsLater = Signal()
    #: The local day now at the top of the viewport.
    topDayChanged = Signal(object)  # date
    recordSelected = Signal(object)  # TimelineRecord | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.timeline = Timeline(self)
        self.setWidget(self.timeline)
        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._top_day: date | None = None
        self.timeline.recordSelected.connect(self.recordSelected)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # -- content ---------------------------------------------------------

    def set_window(self, window: TimelineWindow, *, anchor_epoch: int | None = None) -> None:
        """Show a window, keeping ``anchor_epoch`` at the same place on screen.

        Extending the window prepends content above the viewport, which would
        otherwise jerk the view backwards by however many days were added. The
        anchor is what makes growing the window invisible to the reader.
        """
        self.timeline.set_window(window)
        self.timeline.adjustSize()
        if anchor_epoch is not None:
            self.scroll_to_epoch(anchor_epoch, align_top=True)
        self._announce_top_day()

    def epoch_at_top(self) -> int | None:
        geometry = self.timeline.geometry_model()
        if geometry is None:
            return None
        return geometry.epoch_for_y(self.verticalScrollBar().value())

    def assume_top_day(self, day: date) -> None:
        """Record that the owner has just placed ``day`` at the top.

        Before the pane has a viewport there is nothing to scroll in, so the
        computed top day is whatever the window starts with rather than the day
        that was asked for. Telling the pane keeps its change-detection honest;
        without it the first real scroll back to that day would look like no
        change at all and never be announced.
        """
        self._top_day = day

    def top_day(self) -> date | None:
        """The local day occupying the top of the viewport."""
        window = self.timeline.window_view
        epoch = self.epoch_at_top()
        if window is None or epoch is None:
            return None
        current = window.first_day
        for mark in window.day_marks:
            if mark.epoch <= epoch:
                current = mark.day
            else:
                break
        return current

    # -- zoom ------------------------------------------------------------

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
        """Zoom so one whole day fills the viewport, and go to the day on top.

        A desktop window is tall enough to show a full day at once, which is the
        view that answers "where did today go?" without scrolling. Zooming in is
        for inspecting short runs, so this is what the window opens at.
        """
        window = self.timeline.window_view
        if window is None or not window.days:
            return
        # The remembered day, not a freshly computed one: on the first show the
        # scrollbar has not been driven anywhere yet, so computing would fit the
        # window's first day rather than the one the screen just navigated to.
        target = self._top_day or self.top_day() or window.first_day
        summary = window.summary_for(target)
        if summary is None:
            return

        hours = (summary.day_end_epoch - summary.day_start_epoch) / 3600
        self.timeline.set_zoom(self.viewport().height() / hours)
        self.timeline.adjustSize()
        self.scroll_to_epoch(summary.day_start_epoch, align_top=True)

    def scroll_to_epoch(self, epoch_seconds: int, *, align_top: bool = False) -> None:
        """Put an instant at the top of the viewport, or centred on it."""
        geometry = self.timeline.geometry_model()
        if geometry is None:
            return
        target = geometry.y_for_epoch(epoch_seconds)
        if not align_top:
            target -= self.viewport().height() / 2
        self.verticalScrollBar().setValue(round(target))

    # -- edges -----------------------------------------------------------

    def _on_scrolled(self, value: int) -> None:
        self._announce_top_day()
        runway = self.viewport().height() * EDGE_TRIGGER
        bar = self.verticalScrollBar()
        if value <= runway:
            self.needsEarlier.emit()
        if value >= bar.maximum() - runway:
            self.needsLater.emit()

    def _announce_top_day(self) -> None:
        day = self.top_day()
        if day is not None and day != self._top_day:
            self._top_day = day
            self.topDayChanged.emit(day)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.zoom_by(1.15**steps, anchor_y=event.position().y())
            event.accept()
            return
        super().wheelEvent(event)


def _tooltip_text(record: TimelineRecord) -> str:
    if record.is_instant:
        lines = [record.recorded_path, record.start_label]
        lines.extend(f"{name}: {value}" for name, value in record.fields)
        return "\n".join(lines)
    return (
        f"{record.recorded_path}\n"
        f"{record.start_label}-{record.end_label}  "
        f"{format_duration(record.duration_seconds)}"
    )
