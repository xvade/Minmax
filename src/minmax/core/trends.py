"""Arithmetic for the side-scrolling stacked area chart.

The trends screen answers a different question from the day timeline: not "what
happened on Tuesday" but "what has the shape of my time been". It plots one
column per period -- a day, a week, or a month, see :mod:`minmax.core.columns`
-- stacked by category group, scrolling horizontally through however much
history exists.

Everything here is indexed by *column*, never by day. That distinction is the
whole reason weekly and monthly views are possible: the pixel arithmetic does
not know or care how much time a column covers, so aggregating the data changes
what is plotted without changing any of this.

As with :mod:`minmax.core.timeline`, it is all pure and Qt-free so it can be
tested directly -- stacking and window arithmetic are exactly the kind of logic
that is invisible once buried in a paint method, and a mistake in them produces
a chart that is confidently wrong.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_PIXELS_PER_COLUMN",
    "MAX_PIXELS_PER_COLUMN",
    "MIN_PIXELS_PER_COLUMN",
    "TrendsGeometry",
    "clamp_column_zoom",
    "fit_column_zoom",
    "nice_seconds_ceiling",
    "nice_value_ceiling",
    "stack_bands",
    "tick_interval_columns",
    "visible_column_range",
    "window_totals",
]

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86_400

#: Zoom limits, in pixels per column.
#:
#: The minimum fits roughly 500 columns -- comfortably more than a year of days
#: -- across a 1000px plot, which is the point of a trends view. The maximum
#: bounds *manual* zoom only, so that holding the key down cannot leave a single
#: column stretched across the screen with nothing to compare it to.
#:
#: "Fit all" is deliberately not subject to it: see :func:`fit_column_zoom`.
MIN_PIXELS_PER_COLUMN = 2.0
MAX_PIXELS_PER_COLUMN = 200.0
DEFAULT_PIXELS_PER_COLUMN = 22.0

#: Candidate spacings for date ticks, in columns. Chosen so that with daily
#: columns the labels land on meaningful intervals (a week, a fortnight, a
#: quarter) rather than arbitrary counts that happen to fit.
_TICK_STEPS = (1, 2, 3, 7, 14, 28, 56, 91, 182, 364)

#: Candidate y-axis ceilings, in seconds per day. A day cannot exceed 24 hours,
#: so the ramp stops there; weekly and monthly columns are plotted as a daily
#: average for exactly this reason, which keeps the axis comparable across
#: granularities instead of rescaling by a factor of seven every time.
_VALUE_STEPS = (
    1 * SECONDS_PER_HOUR,
    2 * SECONDS_PER_HOUR,
    4 * SECONDS_PER_HOUR,
    6 * SECONDS_PER_HOUR,
    8 * SECONDS_PER_HOUR,
    12 * SECONDS_PER_HOUR,
    16 * SECONDS_PER_HOUR,
    20 * SECONDS_PER_HOUR,
    24 * SECONDS_PER_HOUR,
)


def clamp_column_zoom(pixels_per_column: float) -> float:
    return max(MIN_PIXELS_PER_COLUMN, min(MAX_PIXELS_PER_COLUMN, pixels_per_column))


def fit_column_zoom(plot_width: float, column_count: int) -> float:
    """The zoom at which ``column_count`` columns exactly fill ``plot_width``.

    Floored but *not* capped, unlike :func:`clamp_column_zoom`. A quarterly
    interval over a year is four columns and a yearly one can be a single
    column; any fixed ceiling low enough to keep manual zoom sensible would
    strand those against the left edge with the rest of the plot blank, which
    reads as missing data rather than as a wide column.
    """
    if column_count < 1:
        raise ValueError(f"column_count must be at least 1, got {column_count}")
    return max(MIN_PIXELS_PER_COLUMN, plot_width / column_count)


@dataclass(frozen=True, slots=True)
class TrendsGeometry:
    """Maps column indices to horizontal pixels and values to vertical ones."""

    column_count: int
    pixels_per_column: float
    plot_height: float
    max_value: float
    """Value at the top of the y axis. Seconds, or 1.0 when normalised."""

    def __post_init__(self) -> None:
        if self.column_count < 0:
            raise ValueError("column_count cannot be negative")
        if self.pixels_per_column <= 0:
            raise ValueError("pixels_per_column must be positive")
        if self.max_value <= 0:
            raise ValueError("max_value must be positive")

    @property
    def content_width(self) -> float:
        return self.column_count * self.pixels_per_column

    def x_for_column(self, column_index: float) -> float:
        """Left edge of a column."""
        return column_index * self.pixels_per_column

    def centre_for_column(self, column_index: int) -> float:
        return (column_index + 0.5) * self.pixels_per_column

    def column_at_x(self, x: float) -> int:
        """Which column a horizontal position falls in, clamped to the range."""
        if self.column_count == 0:
            return 0
        raw = int(x // self.pixels_per_column)
        return max(0, min(self.column_count - 1, raw))

    def y_for_value(self, value: float) -> float:
        """Vertical position of a value, measured from the top of the plot."""
        return self.plot_height * (1.0 - value / self.max_value)


def visible_column_range(
    geometry: TrendsGeometry, scroll_offset: float, viewport_width: float
) -> tuple[int, int]:
    """Inclusive ``(first, last)`` column indices visible in the viewport.

    A partially visible column counts as visible, because it is drawn and the
    reader can see it -- the date labels and the percentages must describe what
    is actually on screen, not a tidier subset of it.
    """
    if geometry.column_count == 0:
        return (0, 0)

    first = geometry.column_at_x(max(0.0, scroll_offset))
    last = geometry.column_at_x(max(0.0, scroll_offset + viewport_width - 1))
    return (first, max(first, last))


def stack_bands(
    series_values: Sequence[Sequence[float]], *, normalise: bool = False
) -> list[list[tuple[float, float]]]:
    """Turn per-series column values into stacked ``(lower, upper)`` bands.

    Returns one list per series, each holding one ``(lower, upper)`` pair per
    column, cumulative from the bottom of the stack in the order given.

    With ``normalise``, each column is divided by its own total so the stack
    fills the plot and the chart reads as composition rather than volume. A
    column with no data normalises to all-zero rather than dividing by zero: an
    empty column is genuinely empty, and stretching it to full height would
    invent a shape.
    """
    if not series_values:
        return []

    column_count = len(series_values[0])
    for values in series_values:
        if len(values) != column_count:
            raise ValueError("every series must cover the same number of columns")

    bands: list[list[tuple[float, float]]] = [[] for _ in series_values]
    for column in range(column_count):
        total = sum(values[column] for values in series_values) if normalise else 0.0
        scale = (1.0 / total) if normalise and total > 0 else (0.0 if normalise else 1.0)

        cursor = 0.0
        for index, values in enumerate(series_values):
            height = values[column] * scale
            bands[index].append((cursor, cursor + height))
            cursor += height

    return bands


def window_totals(
    series_values: Sequence[Sequence[float]], first_column: int, last_column: int
) -> list[float]:
    """Total per series over an inclusive column window -- seconds, event
    counts, or a field's summed value, whichever the series holds.

    This is what the legend's percentages (and a mean metric's window
    average) are computed from, so they describe the columns actually on
    screen and update as the reader scrolls or zooms.
    """
    if not series_values:
        return []
    column_count = len(series_values[0])
    if column_count == 0:
        return [0.0] * len(series_values)

    low = max(0, min(first_column, column_count - 1))
    high = max(low, min(last_column, column_count - 1))
    return [sum(values[low : high + 1]) for values in series_values]


def nice_seconds_ceiling(peak_seconds: float) -> int:
    """A readable y-axis ceiling at or above ``peak_seconds``.

    Snapped to hour counts a person reads without decoding -- 6h, 12h, 24h --
    rather than to whatever the peak happened to be.
    """
    for step in _VALUE_STEPS:
        if peak_seconds <= step:
            return step
    return _VALUE_STEPS[-1]


def nice_value_ceiling(peak_value: float) -> float:
    """A readable y-axis ceiling at or above ``peak_value``, for a trends
    chart with no natural bound -- an event count, or a declared numeric
    field's total or average.

    Not :func:`nice_seconds_ceiling`: that one tops out at 24h because a day
    cannot hold more hours than that, but a count (or a field value, which
    can be anything) has no such bound, so it needs its own steps rather
    than clamping at a fixed maximum the way a duration ceiling correctly
    does.
    """
    if peak_value <= 1:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(peak_value))
    for multiplier in (1, 2, 5, 10):
        candidate = magnitude * multiplier
        if peak_value <= candidate:
            return float(candidate)
    return float(magnitude * 10)


def tick_interval_columns(pixels_per_column: float, *, min_label_pixels: float = 64.0) -> int:
    """Columns between date ticks, so labels have room and never collide.

    Snaps to a meaningful interval rather than the smallest that fits: with
    daily columns a reader is better served by weekly ticks than by "every 5
    days".
    """
    if pixels_per_column <= 0:
        raise ValueError("pixels_per_column must be positive")

    needed = min_label_pixels / pixels_per_column
    for step in _TICK_STEPS:
        if step >= needed:
            return step
    return _TICK_STEPS[-1]
