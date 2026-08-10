"""The facade the UI talks to.

The UI imports from this package and nothing else. See
:mod:`minmax.api.client` for why, and ``docs/architecture.md`` §8 for how that
keeps a future web client from requiring backend changes.
"""

from minmax.api.client import (
    AUTO_INTERVAL,
    DAILY_INTERVAL,
    MAX_INTERVAL_COUNT,
    Aggregate,
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
    TrendMetric,
    TrendSeries,
    TrendsView,
    other_breakdown,
    system_timezone,
)
from minmax.api.format import format_duration, format_value
from minmax.core.models import FieldRole, FieldSpec
from minmax.core.palette import Swatch
from minmax.ingest import validate_declaration

__all__ = [
    "AUTO_INTERVAL",
    "DAILY_INTERVAL",
    "MAX_INTERVAL_COUNT",
    "Aggregate",
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
    "TrendMetric",
    "TrendSeries",
    "TrendsView",
    "format_duration",
    "format_value",
    "other_breakdown",
    "system_timezone",
    "validate_declaration",
]
