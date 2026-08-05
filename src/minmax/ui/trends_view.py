"""The trends screen: a side-scrolling stacked area over days.

Where the day timeline answers "what happened on Tuesday", this answers "what
has the shape of my time been". One column per local day, stacked by category
group, scrolling horizontally through as much history as exists.

Two structural choices worth knowing:

**The widget owns its scroll offset and paints only the visible window**, rather
than being a huge widget inside a ``QScrollArea``. Ten years at maximum zoom
would be a 290,000px-wide widget; painting a window keeps cost proportional to
what is on screen and lets the axis labels stay pinned to the plot edges.

**The chart is drawn to the rules in the dataviz reference**: at most eight
categorical hues with the tail folded into a neutral "Other", solid hairline
gridlines rather than dashed, a 2px surface gap between stacked bands rather
than borders around them, a legend always present, and no value printed on every
point -- the axis and the hover tooltip carry the detail.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPolygonF,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from minmax.api import TrendsView
from minmax.api.format import format_duration
from minmax.core.trends import (
    DEFAULT_PIXELS_PER_DAY,
    TrendsGeometry,
    clamp_day_zoom,
    nice_seconds_ceiling,
    stack_bands,
    tick_interval_days,
    visible_day_range,
    window_totals,
)
from minmax.ui import theme

__all__ = ["TrendsChart"]

AXIS_GUTTER = 58
AXIS_STRIP = 34
PLOT_PADDING_TOP = 12
BAND_GAP = 2.0


class TrendsChart(QWidget):
    """Paints the stacked area and owns scrolling, zooming, and hit-testing."""

    #: Emitted with the inclusive ``(first, last)`` dates now on screen.
    visibleRangeChanged = Signal(object, object)
    #: Emitted when the reader asks to focus a series (double-click a band).
    seriesActivated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view: TrendsView | None = None
        self._pixels_per_day = DEFAULT_PIXELS_PER_DAY
        self._scroll_offset = 0.0
        self._normalised = False
        self._hover_day: int | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(240)

    # -- state -----------------------------------------------------------

    @property
    def pixels_per_day(self) -> float:
        return self._pixels_per_day

    @property
    def normalised(self) -> bool:
        return self._normalised

    @property
    def scroll_offset(self) -> float:
        return self._scroll_offset

    def set_view(self, view: TrendsView) -> None:
        self._view = view
        self._scroll_offset = 0.0
        self.scroll_to_end()
        self.update()

    def set_normalised(self, normalised: bool) -> None:
        if normalised != self._normalised:
            self._normalised = normalised
            self.update()

    def toggle_normalised(self) -> None:
        self.set_normalised(not self._normalised)

    # -- geometry --------------------------------------------------------

    @property
    def plot_width(self) -> float:
        return max(1.0, self.width() - AXIS_GUTTER)

    @property
    def plot_height(self) -> float:
        return max(1.0, self.height() - AXIS_STRIP - PLOT_PADDING_TOP)

    def geometry_model(self) -> TrendsGeometry | None:
        if self._view is None or not self._view.days:
            return None
        ceiling = (
            1.0 if self._normalised else float(nice_seconds_ceiling(self._view.peak_day_seconds))
        )
        return TrendsGeometry(
            day_count=len(self._view.days),
            pixels_per_day=self._pixels_per_day,
            plot_height=self.plot_height,
            max_value=ceiling,
        )

    def visible_dates(self) -> tuple[date, date] | None:
        geometry = self.geometry_model()
        if geometry is None or self._view is None:
            return None
        first, last = visible_day_range(geometry, self._scroll_offset, self.plot_width)
        return self._view.days[first], self._view.days[last]

    def visible_indices(self) -> tuple[int, int]:
        geometry = self.geometry_model()
        if geometry is None:
            return (0, 0)
        return visible_day_range(geometry, self._scroll_offset, self.plot_width)

    # -- scrolling and zoom ----------------------------------------------

    def _max_offset(self) -> float:
        geometry = self.geometry_model()
        if geometry is None:
            return 0.0
        return max(0.0, geometry.content_width - self.plot_width)

    def set_scroll_offset(self, offset: float) -> None:
        clamped = max(0.0, min(self._max_offset(), offset))
        if clamped != self._scroll_offset:
            self._scroll_offset = clamped
            self._announce_range()
            self.update()

    def scroll_by_days(self, days: float) -> None:
        self.set_scroll_offset(self._scroll_offset + days * self._pixels_per_day)

    def scroll_to_start(self) -> None:
        self.set_scroll_offset(0.0)

    def scroll_to_end(self) -> None:
        """Open at the most recent data, which is what a trends view is for."""
        self.set_scroll_offset(self._max_offset())
        self._announce_range()

    def zoom_by(self, factor: float, anchor_x: float | None = None) -> None:
        """Change days-per-screen, keeping the day under ``anchor_x`` in place."""
        old = self._pixels_per_day
        new = clamp_day_zoom(old * factor)
        if new == old:
            return

        anchor = anchor_x if anchor_x is not None else self.plot_width / 2
        self._pixels_per_day = new
        self._scroll_offset = max(
            0.0, min(self._max_offset(), (self._scroll_offset + anchor) * (new / old) - anchor)
        )
        self._announce_range()
        self.update()

    def fit_all(self) -> None:
        """Zoom so the entire recorded span is on screen at once."""
        if self._view is None or not self._view.days:
            return
        self._pixels_per_day = clamp_day_zoom(self.plot_width / len(self._view.days))
        self._scroll_offset = 0.0
        self._announce_range()
        self.update()

    def _announce_range(self) -> None:
        dates = self.visible_dates()
        if dates is not None:
            self.visibleRangeChanged.emit(dates[0], dates[1])

    # -- painting --------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.set_scroll_offset(self._scroll_offset)
        self._announce_range()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        painter.fillRect(event.rect(), theme.surface(appearance))

        geometry = self.geometry_model()
        if geometry is None or self._view is None:
            painter.setPen(theme.subdued_text(appearance))
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "No data yet")
            return

        dark = theme.is_dark(appearance)
        first, last = visible_day_range(geometry, self._scroll_offset, self.plot_width)
        # One extra column each side so bands run to the plot edges rather than
        # stopping half a column short.
        low = max(0, first - 1)
        high = min(geometry.day_count - 1, last + 1)

        self._paint_value_grid(painter, geometry)
        self._paint_bands(painter, geometry, low, high, dark=dark)
        self._paint_date_axis(painter, geometry, first, last)
        self._paint_crosshair(painter, geometry)

    def _plot_point(self, geometry: TrendsGeometry, day: int, value: float) -> QPointF:
        return QPointF(
            AXIS_GUTTER + geometry.centre_for_day(day) - self._scroll_offset,
            PLOT_PADDING_TOP + geometry.y_for_value(value),
        )

    def _edge(self, geometry: TrendsGeometry, day_edge: float, value: float) -> QPointF:
        """A point at a day-column boundary rather than its centre."""
        return QPointF(
            AXIS_GUTTER + geometry.x_for_day(day_edge) - self._scroll_offset,
            PLOT_PADDING_TOP + geometry.y_for_value(value),
        )

    def _edge_points(
        self,
        geometry: TrendsGeometry,
        band: list[tuple[float, float]],
        low: int,
        high: int,
        *,
        upper: bool,
    ) -> list[QPointF]:
        """The polyline along one side of a band, spanning full end columns.

        Values are plotted at day centres, but the *first and last* days of the
        whole series also get a point at the outer edge of their own column. A
        day's value applies across that entire day, so stopping at its centre
        would leave a blank half-column that reads as the data ramping up from
        nothing -- which is a different claim from the one the data makes.
        """
        side = 1 if upper else 0
        points: list[QPointF] = []

        if low == 0:
            points.append(self._edge(geometry, 0.0, band[0][side]))
        points.extend(
            self._plot_point(geometry, day, band[day][side]) for day in range(low, high + 1)
        )
        if high == geometry.day_count - 1:
            points.append(self._edge(geometry, float(geometry.day_count), band[high][side]))

        return points

    def _paint_value_grid(self, painter: QPainter, geometry: TrendsGeometry) -> None:
        appearance = self.palette()
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
        painter.setFont(font)

        divisions = 4
        for step in range(divisions + 1):
            value = geometry.max_value * step / divisions
            y = PLOT_PADDING_TOP + geometry.y_for_value(value)

            # Solid hairlines, one shade off the surface. Never dashed: dashing
            # reads as a threshold or projection when it is only a grid.
            painter.setPen(theme.grid_line(appearance))
            painter.drawLine(AXIS_GUTTER, int(y), self.width(), int(y))

            painter.setPen(theme.gutter_text(appearance))
            if self._normalised:
                label = f"{value * 100:.0f}%"
            else:
                # "0s" reads oddly in a column of "6h", "12h", "18h".
                label = "0" if value == 0 else format_duration(int(value))
            painter.drawText(
                QRectF(0, y - 9, AXIS_GUTTER - 8, 18),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                label,
            )

    def _paint_bands(
        self, painter: QPainter, geometry: TrendsGeometry, low: int, high: int, *, dark: bool
    ) -> None:
        if self._view is None:
            return
        bands = stack_bands(
            [series.values for series in self._view.series], normalise=self._normalised
        )
        painter.setClipRect(
            QRectF(AXIS_GUTTER, 0, self.plot_width, self.plot_height + PLOT_PADDING_TOP)
        )

        for series, band in zip(self._view.series, bands, strict=True):
            top_edge = self._edge_points(geometry, band, low, high, upper=True)
            polygon = QPolygonF(top_edge)
            for point in reversed(self._edge_points(geometry, band, low, high, upper=False)):
                polygon.append(point)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(series.swatch.for_surface(dark=dark)))
            painter.drawPolygon(polygon)

            # A 2px surface gap between stacked bands, rather than a border
            # drawn around each one: the separation is the surface showing
            # through, so bands stay readable without gaining outlines.
            painter.setBrush(Qt.BrushStyle.NoBrush)
            gap_pen = painter.pen()
            gap_pen.setColor(theme.surface(self.palette()))
            gap_pen.setWidthF(BAND_GAP)
            painter.setPen(gap_pen)
            painter.drawPolyline(QPolygonF(top_edge))

        painter.setClipping(False)

    def _paint_date_axis(
        self, painter: QPainter, geometry: TrendsGeometry, first: int, last: int
    ) -> None:
        if self._view is None:
            return
        appearance = self.palette()
        baseline = PLOT_PADDING_TOP + self.plot_height

        painter.setPen(theme.grid_line(appearance))
        painter.drawLine(AXIS_GUTTER, int(baseline), self.width(), int(baseline))

        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
        painter.setFont(font)
        metrics = QFontMetrics(font)

        interval = tick_interval_days(self._pixels_per_day)
        painter.setPen(theme.subdued_text(appearance))
        for day in range(first, last + 1):
            if day % interval or day in (first, last):
                continue
            self._draw_date_label(painter, geometry, day, metrics, emphasised=False)

        # The first and last visible days are always labelled, whatever the tick
        # interval works out to -- the reader must be able to see the span they
        # are looking at without counting columns.
        emphasis = QFont(font)
        emphasis.setBold(True)
        painter.setFont(emphasis)
        painter.setPen(appearance.color(appearance.currentColorGroup(), appearance.ColorRole.Text))
        for day in {first, last}:
            self._draw_date_label(painter, geometry, day, QFontMetrics(emphasis), emphasised=True)

    def _draw_date_label(
        self,
        painter: QPainter,
        geometry: TrendsGeometry,
        day: int,
        metrics: QFontMetrics,
        *,
        emphasised: bool,
    ) -> None:
        if self._view is None:
            return
        moment = self._view.days[day]
        # Built piecewise because the no-pad day directive is not portable.
        text = f"{moment.day} {moment:%b}"
        width = metrics.horizontalAdvance(text) + 10

        centre = AXIS_GUTTER + geometry.centre_for_day(day) - self._scroll_offset
        # Keep an end label fully inside the plot instead of half off the edge.
        left = min(max(centre - width / 2, AXIS_GUTTER), self.width() - width)
        baseline = PLOT_PADDING_TOP + self.plot_height

        if emphasised:
            painter.drawLine(int(centre), int(baseline), int(centre), int(baseline + 4))
        painter.drawText(
            QRectF(left, baseline + 6, width, AXIS_STRIP - 8),
            int(Qt.AlignmentFlag.AlignCenter),
            text,
        )

    def _paint_crosshair(self, painter: QPainter, geometry: TrendsGeometry) -> None:
        if self._hover_day is None:
            return
        x = AXIS_GUTTER + geometry.centre_for_day(self._hover_day) - self._scroll_offset
        if not (AXIS_GUTTER <= x <= self.width()):
            return
        pen = painter.pen()
        pen.setColor(theme.gutter_text(self.palette()))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(int(x), PLOT_PADDING_TOP, int(x), int(PLOT_PADDING_TOP + self.plot_height))

    # -- interaction -----------------------------------------------------

    def day_at(self, x: float) -> int | None:
        geometry = self.geometry_model()
        if geometry is None or x < AXIS_GUTTER:
            return None
        return geometry.day_at_x(x - AXIS_GUTTER + self._scroll_offset)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        day = self.day_at(event.position().x())
        if day != self._hover_day:
            self._hover_day = day
            self.update()
        if day is not None and self._view is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltip_for(day), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: object) -> None:
        self._hover_day = None
        self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def _tooltip_for(self, day: int) -> str:
        assert self._view is not None
        moment = self._view.days[day]
        total = sum(series.values[day] for series in self._view.series)
        lines = [f"{moment:%A} {moment.day} {moment:%b %Y}", f"{format_duration(total)} tracked"]
        for series in sorted(self._view.series, key=lambda s: -s.values[day]):
            if series.values[day]:
                share = series.values[day] / total if total else 0.0
                lines.append(
                    f"  {series.label}  {format_duration(series.values[day])}  {share:.0%}"
                )
        return "\n".join(lines)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double-clicking a band focuses that group, splitting it into children."""
        if self._view is None:
            return
        day = self.day_at(event.position().x())
        if day is None:
            return
        bands = stack_bands(
            [series.values for series in self._view.series], normalise=self._normalised
        )
        geometry = self.geometry_model()
        if geometry is None:
            return
        value = geometry.max_value * (
            1.0 - (event.position().y() - PLOT_PADDING_TOP) / self.plot_height
        )
        for series, band in zip(self._view.series, bands, strict=True):
            lower, upper = band[day]
            if lower <= value <= upper and series.can_drill and not series.is_other:
                self.seriesActivated.emit(series.label)
                return

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.zoom_by(1.2**steps, anchor_x=event.position().x() - AXIS_GUTTER)
        else:
            delta = event.angleDelta().x() or event.angleDelta().y()
            self.set_scroll_offset(self._scroll_offset - delta)
        event.accept()

    def window_shares(self) -> list[float]:
        """Each series' share of the days currently on screen."""
        if self._view is None:
            return []
        first, last = self.visible_indices()
        totals = window_totals([s.values for s in self._view.series], first, last)
        grand = sum(totals)
        return [(value / grand) if grand else 0.0 for value in totals]

    def window_totals_seconds(self) -> list[int]:
        if self._view is None:
            return []
        first, last = self.visible_indices()
        return window_totals([s.values for s in self._view.series], first, last)
