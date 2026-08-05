"""The facade the UI talks to.

The UI imports from this package and nothing else. See
:mod:`minmax.api.client` for why, and ``docs/architecture.md`` §8 for how that
keeps a future web client from requiring backend changes.
"""

from minmax.api.client import (
    CategorySlice,
    DayView,
    HourMark,
    InProcessClient,
    MinmaxClient,
    RangeTotals,
    StreamSummary,
    TimelineRecord,
    TrendSeries,
    TrendsView,
    system_timezone,
)
from minmax.api.format import format_duration
from minmax.core.palette import Swatch

__all__ = [
    "CategorySlice",
    "DayView",
    "HourMark",
    "InProcessClient",
    "MinmaxClient",
    "RangeTotals",
    "StreamSummary",
    "Swatch",
    "TimelineRecord",
    "TrendSeries",
    "TrendsView",
    "format_duration",
    "system_timezone",
]
