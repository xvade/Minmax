"""The trends screen: a side-scrolling stacked area over calendar columns.

Where the day timeline answers "what happened on Tuesday", this answers "what
has the shape of my time been". One column per period -- a day, a week, or a
month -- stacked by category group, scrolling horizontally through as much
history as exists.

Three structural choices worth knowing:

**The widget owns its scroll offset and paints only the visible window**, rather
than being a huge widget inside a ``QScrollArea``. Ten years at maximum zoom
would be a 290,000px-wide widget; painting a window keeps cost proportional to
what is on screen and lets the axis labels stay pinned to the plot edges.

**Columns are plotted as a daily average, never as a total.** A weekly column
holds roughly seven times the seconds of a daily one, so plotting totals would
rescale the y axis by seven the moment the granularity changed and make the same
data look like a different life. Dividing by the days a column covers keeps the
axis in hours-per-day at every granularity.

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

from minmax.api import StreamKind, TrendMetric, TrendsView
from minmax.api.format import format_duration, format_measure, format_value
from minmax.core.trends import (
    DEFAULT_PIXELS_PER_COLUMN,
    TrendsGeometry,
    clamp_column_zoom,
    fit_column_zoom,
    nice_seconds_ceiling,
    nice_value_ceiling,
    stack_bands,
    tick_interval_columns,
    visible_column_range,
    window_totals,
)
from minmax.ui import theme

__all__ = ["TrendsChart", "format_trend_amount"]

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
        self._pixels_per_column = DEFAULT_PIXELS_PER_COLUMN
        self._scroll_offset = 0.0
        self._normalised = False
        self._hover_column: int | None = None
        #: Stacked bands for the current view/normalisation, recomputed only
        #: when either changes rather than on every paint -- a paint can be
        #: triggered by a hover-driven crosshair move or a scroll, which would
        #: otherwise re-stack every column of a multi-year view dozens of
        #: times a second for no change in the result. Empty whenever the
        #: view's metric does not stack -- see ``_lines`` instead.
        self._bands: list[list[tuple[float, float]]] = []
        #: Per-column reading averages for a mean metric, recomputed only
        #: when the view changes (never by normalisation, which a mean
        #: metric refuses). Empty whenever the view's metric stacks.
        self._lines: list[list[float | None]] = []
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(240)

    # -- state -----------------------------------------------------------

    @property
    def pixels_per_column(self) -> float:
        return self._pixels_per_column

    @property
    def normalised(self) -> bool:
        return self._normalised

    @property
    def scroll_offset(self) -> float:
        return self._scroll_offset

    def set_view(self, view: TrendsView) -> None:
        self._view = view
        if not view.metric.stacks:
            # A percentage of a set of averages means nothing -- there is no
            # whole they add up to -- so a mean view never opens normalised.
            self._normalised = False
        self._bands = (
            stack_bands(self._plot_values(), normalise=self._normalised)
            if view.metric.stacks
            else []
        )
        self._lines = [] if view.metric.stacks else self._line_values()
        self._scroll_offset = 0.0
        self.scroll_to_end()
        self.update()

    def set_normalised(self, normalised: bool) -> None:
        if self._view is not None and not self._view.metric.stacks:
            # Refused rather than silently ignored: a mean's bands are never
            # built, so there is nothing here to normalise.
            return
        if normalised != self._normalised:
            self._normalised = normalised
            self._bands = stack_bands(self._plot_values(), normalise=self._normalised)
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
        if self._view is None or not self._view.columns:
            return None
        if self._normalised:
            ceiling = 1.0
        elif self._view.stream_kind is StreamKind.INTERVAL:
            # An interval stream's only metric is a duration, capped at 24h
            # for a real reason (a day cannot hold more hours than that).
            ceiling = float(nice_seconds_ceiling(self._view.peak_plot_value))
        else:
            # An event count, a field's total, or a field's average all have
            # no natural bound, so they share the generic 1-2-5 ceiling.
            ceiling = float(nice_value_ceiling(self._view.peak_plot_value))
        return TrendsGeometry(
            column_count=len(self._view.columns),
            pixels_per_column=self._pixels_per_column,
            plot_height=self.plot_height,
            max_value=ceiling,
        )

    def _plot_values(self) -> list[list[float]]:
        """Per-column daily averages, which is what the bands are drawn from.

        Only meaningful for a stacking metric -- see :meth:`_line_values` for
        a mean one, which must not be divided by the column's day count a
        second time.
        """
        if self._view is None:
            return []
        spans = [max(1, column.day_count) for column in self._view.columns]
        return [
            [value / span for value, span in zip(series.values, spans, strict=True)]
            for series in self._view.series
        ]

    def _line_values(self) -> list[list[float | None]]:
        """Per-column reading averages for a mean metric.

        ``None`` where a category recorded no reading that column: plotting
        zero there would claim the reading *was* zero, the same false claim
        :mod:`minmax.core.trends` already refuses to make about an empty
        column's stacked height.
        """
        if self._view is None:
            return []
        return [
            [
                value / weight if weight else None
                for value, weight in zip(series.values, series.weights, strict=True)
            ]
            for series in self._view.series
        ]

    def visible_dates(self) -> tuple[date, date] | None:
        """First and last *day* on screen -- the span, not the column starts.

        A weekly column that is partly on screen still shows the whole week, so
        the honest answer for the header is the outer edge of the end columns.
        """
        geometry = self.geometry_model()
        if geometry is None or self._view is None:
            return None
        first, last = visible_column_range(geometry, self._scroll_offset, self.plot_width)
        return self._view.columns[first].start, self._view.columns[last].end

    def visible_indices(self) -> tuple[int, int]:
        geometry = self.geometry_model()
        if geometry is None:
            return (0, 0)
        return visible_column_range(geometry, self._scroll_offset, self.plot_width)

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

    def scroll_by_columns(self, columns: float) -> None:
        self.set_scroll_offset(self._scroll_offset + columns * self._pixels_per_column)

    def scroll_to_start(self) -> None:
        self.set_scroll_offset(0.0)

    def scroll_to_end(self) -> None:
        """Open at the most recent data, which is what a trends view is for."""
        self.set_scroll_offset(self._max_offset())
        self._announce_range()

    def zoom_by(self, factor: float, anchor_x: float | None = None) -> None:
        """Change columns-per-screen, keeping the column under ``anchor_x`` in place."""
        old = self._pixels_per_column
        new = clamp_column_zoom(old * factor)
        if new == old:
            return

        anchor = anchor_x if anchor_x is not None else self.plot_width / 2
        self._pixels_per_column = new
        self._scroll_offset = max(
            0.0, min(self._max_offset(), (self._scroll_offset + anchor) * (new / old) - anchor)
        )
        self._announce_range()
        self.update()

    def fit_all(self) -> None:
        """Zoom so the entire range is on screen at once."""
        if self._view is None or not self._view.columns:
            return
        self._pixels_per_column = fit_column_zoom(self.plot_width, len(self._view.columns))
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
        first, last = visible_column_range(geometry, self._scroll_offset, self.plot_width)
        # One extra column each side so bands run to the plot edges rather than
        # stopping half a column short.
        low = max(0, first - 1)
        high = min(geometry.column_count - 1, last + 1)

        self._paint_value_grid(painter, geometry)
        if self._view.metric.stacks:
            self._paint_bands(painter, geometry, low, high, dark=dark)
        else:
            self._paint_lines(painter, geometry, low, high, dark=dark)
        self._paint_date_axis(painter, geometry, first, last)
        self._paint_crosshair(painter, geometry)

    def _plot_point(self, geometry: TrendsGeometry, column: int, value: float) -> QPointF:
        return QPointF(
            AXIS_GUTTER + geometry.centre_for_column(column) - self._scroll_offset,
            PLOT_PADDING_TOP + geometry.y_for_value(value),
        )

    def _edge(self, geometry: TrendsGeometry, column_edge: float, value: float) -> QPointF:
        """A point at a column boundary rather than its centre."""
        return QPointF(
            AXIS_GUTTER + geometry.x_for_column(column_edge) - self._scroll_offset,
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

        Values are plotted at column centres, but the *first and last* columns of
        the whole series also get a point at the outer edge of their own column.
        A column's value applies across all of it, so stopping at its centre
        would leave a blank half-column that reads as the data ramping up from
        nothing -- which is a different claim from the one the data makes.
        """
        side = 1 if upper else 0
        points: list[QPointF] = []

        if low == 0:
            points.append(self._edge(geometry, 0.0, band[0][side]))
        points.extend(
            self._plot_point(geometry, column, band[column][side])
            for column in range(low, high + 1)
        )
        if high == geometry.column_count - 1:
            points.append(self._edge(geometry, float(geometry.column_count), band[high][side]))

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
            assert self._view is not None  # geometry_model() already checked this
            label = _axis_label(
                value,
                normalised=self._normalised,
                metric=self._view.metric,
                stream_kind=self._view.stream_kind,
            )
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
        bands = self._bands
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

    def _paint_lines(
        self, painter: QPainter, geometry: TrendsGeometry, low: int, high: int, *, dark: bool
    ) -> None:
        """A mean metric's series as unstacked lines rather than bands: an
        average does not add, so nothing here is stacked or filled."""
        if self._view is None:
            return
        painter.setClipRect(
            QRectF(AXIS_GUTTER, 0, self.plot_width, self.plot_height + PLOT_PADDING_TOP)
        )

        for series, values in zip(self._view.series, self._lines, strict=True):
            pen = painter.pen()
            pen.setColor(QColor(series.swatch.for_surface(dark=dark)))
            pen.setWidthF(2.0)
            painter.setPen(pen)

            # A run of consecutive readings, broken at each gap -- a missing
            # column has no average to interpolate across.
            segment: list[QPointF] = []
            for column in range(low, high + 1):
                value = values[column]
                if value is None:
                    if len(segment) > 1:
                        painter.drawPolyline(QPolygonF(segment))
                    segment = []
                    continue
                segment.append(self._plot_point(geometry, column, value))
            if len(segment) > 1:
                painter.drawPolyline(QPolygonF(segment))

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

        interval = tick_interval_columns(self._pixels_per_column)
        painter.setPen(theme.subdued_text(appearance))
        for column in range(first, last + 1):
            if column % interval or column in (first, last):
                continue
            self._draw_date_label(painter, geometry, column, metrics, emphasised=False)

        # The first and last visible columns are always labelled, whatever the
        # tick interval works out to -- the reader must be able to see the span
        # they are looking at without counting columns.
        emphasis = QFont(font)
        emphasis.setBold(True)
        painter.setFont(emphasis)
        painter.setPen(appearance.color(appearance.currentColorGroup(), appearance.ColorRole.Text))
        for column in {first, last}:
            self._draw_date_label(
                painter, geometry, column, QFontMetrics(emphasis), emphasised=True
            )

    def _draw_date_label(
        self,
        painter: QPainter,
        geometry: TrendsGeometry,
        column: int,
        metrics: QFontMetrics,
        *,
        emphasised: bool,
    ) -> None:
        if self._view is None:
            return
        text = self._view.columns[column].axis_label
        width = metrics.horizontalAdvance(text) + 10

        centre = AXIS_GUTTER + geometry.centre_for_column(column) - self._scroll_offset
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
        if self._hover_column is None:
            return
        x = AXIS_GUTTER + geometry.centre_for_column(self._hover_column) - self._scroll_offset
        if not (AXIS_GUTTER <= x <= self.width()):
            return
        pen = painter.pen()
        pen.setColor(theme.gutter_text(self.palette()))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(int(x), PLOT_PADDING_TOP, int(x), int(PLOT_PADDING_TOP + self.plot_height))

    # -- interaction -----------------------------------------------------

    def column_at(self, x: float) -> int | None:
        geometry = self.geometry_model()
        if geometry is None or x < AXIS_GUTTER:
            return None
        return geometry.column_at_x(x - AXIS_GUTTER + self._scroll_offset)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        column = self.column_at(event.position().x())
        if column != self._hover_column:
            self._hover_column = column
            self.update()
        if column is not None and self._view is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltip_for(column), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: object) -> None:
        self._hover_column = None
        self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def _tooltip_for(self, column: int) -> str:
        assert self._view is not None
        view = self._view
        metric = view.metric
        span = view.columns[column]
        lines = [span.detail_label]

        if metric.stacks:
            kind = view.stream_kind
            total = sum(series.values[column] for series in view.series)
            if span.day_count > 1:
                # A multi-day column needs both numbers: the total is what
                # happened, the average is what the plotted height means.
                lines.append(
                    f"{format_trend_amount(total, metric, kind)} over {span.day_count} days"
                    f"  ·  {format_trend_amount(total / span.day_count, metric, kind)}/day"
                )
            else:
                # "N events tracked" reads oddly; "N events" (or a field's
                # bare value) already says enough.
                bare = metric.field_key is not None or kind is StreamKind.EVENT
                suffix = "" if bare else " tracked"
                lines.append(f"{format_trend_amount(total, metric, kind)}{suffix}")

            for series in sorted(view.series, key=lambda s: -s.values[column]):
                if series.values[column]:
                    share = series.values[column] / total if total else 0.0
                    amount = format_trend_amount(series.values[column], metric, kind)
                    lines.append(f"  {series.label}  {amount}  {share:.0%}")
        else:
            # A mean does not sum to a meaningful whole, so there is no share
            # to report -- only each series' own average this column, and the
            # value-weighted average across all of them.
            readings = [
                (series, series.values[column] / series.weights[column])
                for series in view.series
                if series.weights[column]
            ]
            if readings:
                overall_value = sum(series.values[column] for series, _ in readings)
                overall_weight = sum(series.weights[column] for series, _ in readings)
                overall = format_value(overall_value / overall_weight, unit=metric.unit)
                lines.append(f"average {overall}")
            else:
                lines.append("no readings")
            for series, average in sorted(readings, key=lambda item: -item[1]):
                lines.append(f"  {series.label}  {format_value(average, unit=metric.unit)}")

        return "\n".join(lines)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double-clicking a band or line activates that series: a regular
        one focuses that group, splitting it into children; "Other" shows
        the breakdown of whatever got folded into it. Either way the screen
        that owns this chart decides which, from the same ``seriesActivated``
        signal -- the chart only needs to know a series is *clickable*, not
        what clicking it does."""
        view = self._view
        if view is None:
            return
        column = self.column_at(event.position().x())
        if column is None:
            return
        geometry = self.geometry_model()
        if geometry is None:
            return
        y = event.position().y()

        if view.metric.stacks:
            value = geometry.max_value * (1.0 - (y - PLOT_PADDING_TOP) / self.plot_height)
            for series, band in zip(view.series, self._bands, strict=True):
                lower, upper = band[column]
                if lower <= value <= upper and (series.can_drill or series.is_other):
                    self.seriesActivated.emit(series.label)
                    return
            return

        # No band to hit-test against a line, so the nearest plotted point
        # within a few pixels wins instead.
        threshold = 6.0
        nearest: tuple[float, str] | None = None
        for series, values in zip(view.series, self._lines, strict=True):
            reading = values[column]
            if reading is None or not (series.can_drill or series.is_other):
                continue
            distance = abs(PLOT_PADDING_TOP + geometry.y_for_value(reading) - y)
            if distance <= threshold and (nearest is None or distance < nearest[0]):
                nearest = (distance, series.label)
        if nearest is not None:
            self.seriesActivated.emit(nearest[1])

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
        """Each series' share of the columns currently on screen.

        Empty for a mean metric: an average does not sum to a meaningful
        whole, so there is no share to compute.
        """
        if self._view is None or not self._view.metric.stacks:
            return []
        first, last = self.visible_indices()
        totals = window_totals([s.values for s in self._view.series], first, last)
        grand = sum(totals)
        return [(value / grand) if grand else 0.0 for value in totals]

    def window_values(self) -> list[float]:
        """Each series' value over the visible window: a sum for a stacking
        metric, or the value-weighted average of its readings for a mean --
        never the mean of the window's per-column averages, which would be
        the same mean-of-means mistake ``core/measures.py`` is written to
        prevent everywhere else.
        """
        if self._view is None:
            return []
        first, last = self.visible_indices()
        value_sums = window_totals([s.values for s in self._view.series], first, last)
        if self._view.metric.stacks:
            return value_sums
        weight_sums = window_totals([s.weights for s in self._view.series], first, last)
        return [
            (value / weight) if weight else 0.0
            for value, weight in zip(value_sums, weight_sums, strict=True)
        ]


def format_trend_amount(value: float, metric: TrendMetric, kind: StreamKind) -> str:
    """A stacking metric's value the way its shape reads: a count or a
    duration via :func:`format_measure`, or a declared field's total via
    :func:`format_value` and its unit."""
    if metric.field_key is None:
        return format_measure(round(value), kind)
    return format_value(value, unit=metric.unit)


def _axis_label(
    value: float, *, normalised: bool, metric: TrendMetric, stream_kind: StreamKind
) -> str:
    """One y-axis gridline's text.

    Pulled out of the paint method because it is easy to get subtly wrong in
    a way pixels don't reveal in review: ``round()`` on a count axis collapses
    a half-integer gridline (a small, realistic ceiling for an event stream
    puts them at 0, 0.5, 1, 1.5, 2) to the same displayed value as its
    neighbour via round-half-to-even, so a chart with a low daily count
    showed "2" on two different gridlines before this was a named function
    with its own test.
    """
    if normalised:
        return f"{value * 100:.0f}%"
    if value == 0:
        return "0"
    if metric.field_key is not None:
        return format_value(value, unit=metric.unit)
    if stream_kind is StreamKind.INTERVAL:
        # "0s" reads oddly in a column of "6h", "12h", "18h" -- handled above.
        return format_duration(int(value))
    return f"{value:g}"
