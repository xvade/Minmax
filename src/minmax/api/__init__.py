"""The facade the UI talks to.

The UI imports from this package and nothing else. See
:mod:`minmax.api.client` for why, and ``docs/architecture.md`` §8 for how that
keeps a future web client from requiring backend changes.
"""

from minmax.api.client import (
    AUTO_INTERVAL,
    DAILY_INTERVAL,
    MAX_INTERVAL_COUNT,
    CategorySlice,
    CorrelationRow,
    CorrelationsView,
    CsvPreview,
    DayMark,
    DaySummary,
    DayView,
    Granularity,
    HourMark,
    ImportOutcome,
    InProcessClient,
    Interval,
    MinmaxClient,
    RangeTotals,
    StreamKind,
    StreamSummary,
    TimelineRecord,
    TimelineWindow,
    TrendColumn,
    TrendSeries,
    TrendsView,
    system_timezone,
)
from minmax.api.format import format_duration
from minmax.core.models import FieldRole, FieldSpec
from minmax.core.palette import Swatch

__all__ = [
    "AUTO_INTERVAL",
    "DAILY_INTERVAL",
    "MAX_INTERVAL_COUNT",
    "CategorySlice",
    "CorrelationRow",
    "CorrelationsView",
    "CsvPreview",
    "DayMark",
    "DaySummary",
    "DayView",
    "FieldRole",
    "FieldSpec",
    "Granularity",
    "HourMark",
    "ImportOutcome",
    "InProcessClient",
    "Interval",
    "MinmaxClient",
    "RangeTotals",
    "StreamKind",
    "StreamSummary",
    "Swatch",
    "TimelineRecord",
    "TimelineWindow",
    "TrendColumn",
    "TrendSeries",
    "TrendsView",
    "format_duration",
    "system_timezone",
]
