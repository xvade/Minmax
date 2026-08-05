"""Arithmetic for the side-scrolling stacked area chart.

The trends screen answers a different question from the day timeline: not "what
happened on Tuesday" but "what has the shape of my time been". It plots one
column per local day, stacked by category group, scrolling horizontally through
however much history exists.

As with :mod:`minmax.core.timeline`, everything here is pure and Qt-free so it
can be tested directly -- stacking and window arithmetic are exactly the kind of
logic that is invisible once buried in a paint method, and a mistake in them
produces a chart that is confidently wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_PIXELS_PER_DAY",
    "MAX_PIXELS_PER_DAY",
    "MIN_PIXELS_PER_DAY",
    "TrendsGeometry",
    "clamp_day_zoom",
    "nice_seconds_ceiling",
    "stack_bands",
    "tick_interval_days",
    "visible_day_range",
    "window_totals",
]

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86_400

#: Zoom limits, in pixels per day.
#:
#: The minimum fits roughly 500 days -- comfortably more than a year -- across a
#: 1000px plot, which is the point of a trends view. The maximum makes a day a
#: wide enough column to read as an individual bar, past which the day timeline
#: is the better screen.
MIN_PIXELS_PER_DAY = 2.0
MAX_PIXELS_PER_DAY = 80.0
DEFAULT_PIXELS_PER_DAY = 22.0

#: Candidate spacings for date ticks, in days. Chosen so labels land on
#: meaningful intervals (a week, a fortnight, a quarter) rather than arbitrary
#: counts that happen to fit.
_TICK_STEPS = (1, 2, 3, 7, 14, 28, 56, 91, 182, 364)

#: Candidate y-axis ceilings, in seconds. A day cannot exceed 24 hours, so the
#: ramp stops there.
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


def clamp_day_zoom(pixels_per_day: float) -> float:
    return max(MIN_PIXELS_PER_DAY, min(MAX_PIXELS_PER_DAY, pixels_per_day))


@dataclass(frozen=True, slots=True)
class TrendsGeometry:
    """Maps day indices to horizontal pixels and values to vertical ones."""

    day_count: int
    pixels_per_day: float
    plot_height: float
    max_value: float
    """Value at the top of the y axis. Seconds, or 1.0 when normalised."""

    def __post_init__(self) -> None:
        if self.day_count < 0:
            raise ValueError("day_count cannot be negative")
        if self.pixels_per_day <= 0:
            raise ValueError("pixels_per_day must be positive")
        if self.max_value <= 0:
            raise ValueError("max_value must be positive")

    @property
    def content_width(self) -> float:
        return self.day_count * self.pixels_per_day

    def x_for_day(self, day_index: float) -> float:
        """Left edge of a day's column."""
        return day_index * self.pixels_per_day

    def centre_for_day(self, day_index: int) -> float:
        return (day_index + 0.5) * self.pixels_per_day

    def day_at_x(self, x: float) -> int:
        """Which day a horizontal position falls in, clamped to the range."""
        if self.day_count == 0:
            return 0
        raw = int(x // self.pixels_per_day)
        return max(0, min(self.day_count - 1, raw))

    def y_for_value(self, value: float) -> float:
        """Vertical position of a value, measured from the top of the plot."""
        return self.plot_height * (1.0 - value / self.max_value)


def visible_day_range(
    geometry: TrendsGeometry, scroll_offset: float, viewport_width: float
) -> tuple[int, int]:
    """Inclusive ``(first, last)`` day indices visible in the viewport.

    A partially visible column counts as visible, because it is drawn and the
    reader can see it -- the date labels and the percentages must describe what
    is actually on screen, not a tidier subset of it.
    """
    if geometry.day_count == 0:
        return (0, 0)

    first = geometry.day_at_x(max(0.0, scroll_offset))
    last = geometry.day_at_x(max(0.0, scroll_offset + viewport_width - 1))
    return (first, max(first, last))


def stack_bands(
    series_values: Sequence[Sequence[int]], *, normalise: bool = False
) -> list[list[tuple[float, float]]]:
    """Turn per-series daily values into stacked ``(lower, upper)`` bands.

    Returns one list per series, each holding one ``(lower, upper)`` pair per
    day, cumulative from the bottom of the stack in the order given.

    With ``normalise``, each day is divided by its own total so the stack fills
    the plot and the chart reads as composition rather than volume. A day with
    no data normalises to all-zero rather than dividing by zero: an empty day is
    genuinely empty, and stretching it to full height would invent a shape.
    """
    if not series_values:
        return []

    day_count = len(series_values[0])
    for values in series_values:
        if len(values) != day_count:
            raise ValueError("every series must cover the same number of days")

    bands: list[list[tuple[float, float]]] = [[] for _ in series_values]
    for day in range(day_count):
        total = sum(values[day] for values in series_values) if normalise else 0
        scale = (1.0 / total) if normalise and total > 0 else (0.0 if normalise else 1.0)

        cursor = 0.0
        for index, values in enumerate(series_values):
            height = values[day] * scale
            bands[index].append((cursor, cursor + height))
            cursor += height

    return bands


def window_totals(
    series_values: Sequence[Sequence[int]], first_day: int, last_day: int
) -> list[int]:
    """Total seconds per series over an inclusive day window.

    This is what the legend's percentages are computed from, so they describe
    the days actually on screen and update as the reader scrolls or zooms.
    """
    if not series_values:
        return []
    day_count = len(series_values[0])
    if day_count == 0:
        return [0] * len(series_values)

    low = max(0, min(first_day, day_count - 1))
    high = max(low, min(last_day, day_count - 1))
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


def tick_interval_days(pixels_per_day: float, *, min_label_pixels: float = 64.0) -> int:
    """Days between date ticks, so labels have room and never collide.

    Snaps to a meaningful interval rather than the smallest that fits: at a
    given zoom a reader is better served by weekly ticks than by "every 5 days".
    """
    if pixels_per_day <= 0:
        raise ValueError("pixels_per_day must be positive")

    needed = min_label_pixels / pixels_per_day
    for step in _TICK_STEPS:
        if step >= needed:
            return step
    return _TICK_STEPS[-1]
