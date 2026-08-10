"""The facade the UI talks to.

``AGENTS.md`` requires that redoing the UI have no effect on the underlying
systems. That is enforced here by construction: the UI is written against the
:class:`MinmaxClient` *protocol* and never imports ``storage``, ``ingest``, or
``analysis``. Today the desktop app binds that protocol to
:class:`InProcessClient`, which calls the repository directly -- no server
process, no serialization cost. When a web UI arrives, an ``HttpClient`` and a
thin server implement the same protocol, and neither the existing UI nor any
backend module changes.

The protocol is therefore designed as though it were already a network API:

- operations are coarse-grained, so a view is one call rather than a chatty loop;
- every argument and return value is a plain, serializable value; and
- no live database rows, connections, or ORM objects cross the boundary.

Presentation-adjacent values that all clients must agree on -- colours, the
local day a record belongs to, formatted clock readings -- are computed *here*
rather than in the UI, so a second client renders the same data identically.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Protocol, runtime_checkable

from minmax.analysis.correlate import correlate as run_correlate
from minmax.analysis.models import Skipped
from minmax.analysis.sweep import DEFAULT_SWEEP
from minmax.core import paths
from minmax.core.columns import (
    AUTO_INTERVAL,
    DAILY_INTERVAL,
    MAX_INTERVAL_COUNT,
    Column,
    Granularity,
    Interval,
    columns_for,
)
from minmax.core.models import (
    CategoryTotal,
    EventCategoryTotal,
    FieldSpec,
    StoredEvent,
    StoredInterval,
    Stream,
    StreamKind,
)
from minmax.core.palette import OTHER, Swatch, swatch_for_slot
from minmax.core.timeline import hour_boundaries
from minmax.core.timeref import local_day_bounds
from minmax.ingest import guess_fields, importer_for, read_sample, run_import
from minmax.storage.repository import OverlapError, Repository

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
    "Granularity",
    "HourMark",
    "ImportOutcome",
    "InProcessClient",
    "Interval",
    "MinmaxClient",
    "RangeTotals",
    "StreamKind",
    "StreamSummary",
    "TimelineRecord",
    "TimelineWindow",
    "TrendColumn",
    "TrendSeries",
    "TrendsView",
    "system_timezone",
]


def system_timezone() -> tzinfo:
    """The machine's current timezone.

    Lives here rather than in ``core`` so that ``core`` stays free of
    environment access: a client decides *which* timezone a user's "Tuesday"
    means, and the domain layer only does arithmetic with the one it is given.
    """
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None  # astimezone() always attaches one
    return tz


# ----------------------------------------------------------------------
# Transfer objects
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What happened when a file was handed to :meth:`MinmaxClient.import_file`.

    ``error`` is set when nothing was attempted at all -- no importer for the
    requested stream, no such file, or overlapping data that aborted the import
    before anything was stored. The numeric fields are all zero in that case.
    Otherwise they describe what :func:`~minmax.ingest.base.run_import` did,
    the same figures the CLI's ``import`` command prints.
    """

    source_name: str
    stream_key: str = ""
    error: str | None = None
    skipped: bool = False
    """Identical content was already imported; nothing new was stored."""

    rows_read: int = 0
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    notes: Sequence[str] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class CsvPreview:
    """A CSV inspected before a stream is created from it.

    What the new-stream dialog shows -- the header, a few sample rows -- and
    pre-fills its role dropdowns from. ``error`` is set for a missing or
    unreadable file; every other field is empty in that case.
    """

    header: tuple[str, ...] = ()
    sample_rows: tuple[tuple[str, ...], ...] = ()
    suggested_fields: tuple[FieldSpec, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class StreamSummary:
    key: str
    name: str
    kind: str
    exclusive: bool
    record_count: int
    tracked_seconds: int
    first_day: date | None
    last_day: date | None
    watch_path: str | None = None
    """The path :meth:`MinmaxClient.set_auto_fetch` last remembered, if any."""
    watch_enabled: bool = False
    """Whether this stream is re-imported from ``watch_path`` once a minute."""


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """One observation, carrying everything needed to draw it."""

    id: int
    category_path: str
    """The category's current path -- what to group by."""

    recorded_path: str
    """The path in effect when the record was captured -- what to label it."""

    top_level: str
    swatch: Swatch
    start_epoch: int
    end_epoch: int
    duration_seconds: int
    start_label: str
    """Local clock reading, ``HH:MM``, using the record's own stored offset."""

    end_label: str

    start_detail: str = ""
    """Full local reading with seconds and offset -- what the inspector shows.

    ``HH:MM`` is right on a block, where space is scarce and the minute is what
    a reader is checking. It is wrong in an inspector: two runs that both read
    ``09:15`` may be forty seconds apart, and the whole point of inspecting one
    is to see what was actually recorded.
    """

    end_detail: str = ""

    import_run_id: int = 0
    """Which import produced this record. The provenance chain starts here."""

    source_name: str = ""
    """The file that import read, by name. Empty if the run is unknown."""

    is_instant: bool = False
    """True for a point-in-time record. ``start_epoch == end_epoch`` and
    ``duration_seconds == 0`` in that case -- there is no span to draw, only a
    moment, so a renderer draws a marker instead of a block."""

    fields: tuple[tuple[str, str], ...] = ()
    """The record's declared field values as ``(label, formatted value)``
    pairs, in declared order. Empty for an interval record, and for a
    point-in-time record with no declared fields beyond its instant."""


@dataclass(frozen=True, slots=True)
class CategorySlice:
    category_path: str
    top_level: str
    swatch: Swatch
    seconds: int
    record_count: int
    share: float
    """Fraction of the queried total, 0.0-1.0. Zero when the total is zero."""


@dataclass(frozen=True, slots=True)
class HourMark:
    """An hour boundary and the clock reading a person would see there.

    Labels are attached here rather than in the view because turning an instant
    into a clock reading needs a timezone. On a fall-back day that means ``01:00``
    legitimately appears twice, which is what the user's clock did; a view
    computing labels from elapsed time would instead count up to ``24:00``.
    """

    epoch: int
    label: str


@dataclass(frozen=True, slots=True)
class DayMark:
    """The instant a local day begins, and how to name it.

    A continuous timeline has no other way to say where one day ends and the
    next starts. On a DST day these are not 24 hours apart, which is why the
    instant is carried rather than recomputed from an index.
    """

    epoch: int
    day: date
    label: str


@dataclass(frozen=True, slots=True)
class DaySummary:
    """One local day's totals, without its records."""

    day: date
    day_start_epoch: int
    day_end_epoch: int
    totals: list[CategorySlice] = field(default_factory=list)
    tracked_seconds: int = 0


@dataclass(frozen=True, slots=True)
class DayView:
    """Everything the timeline needs for one local day, in one call."""

    day: date
    day_start_epoch: int
    day_end_epoch: int
    records: list[TimelineRecord] = field(default_factory=list)
    totals: list[CategorySlice] = field(default_factory=list)
    hour_marks: list[HourMark] = field(default_factory=list)
    tracked_seconds: int = 0
    prefix: str | None = None
    stream_kind: StreamKind = StreamKind.INTERVAL
    """Which of :attr:`CategorySlice.seconds` / ``.record_count`` -- and
    :attr:`tracked_seconds` -- means something for this view. A point-in-time
    stream has no duration, so ``tracked_seconds`` is 0 for one and the count
    is what a reader wants instead."""

    @property
    def is_empty(self) -> bool:
        return not self.records


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    """A run of consecutive local days, drawn as one continuous column.

    The day timeline scrolls across midnight rather than stepping day by day,
    the way Timecard's history does, so the view's unit is a *window* of days
    rather than a single one. A run crossing midnight is one record inside it,
    with no boundary handling at all -- the clipping and continuation marks in
    :func:`~minmax.core.timeline.lay_out_day` only apply at the window's own
    outer edges now, which is where they are honest.

    Per-day summaries travel with the records so that scrolling from one day to
    the next does not need a second round trip to repaint the totals panel.
    """

    first_day: date
    last_day: date
    start_epoch: int
    end_epoch: int
    records: list[TimelineRecord] = field(default_factory=list)
    hour_marks: list[HourMark] = field(default_factory=list)
    day_marks: list[DayMark] = field(default_factory=list)
    days: list[DaySummary] = field(default_factory=list)
    prefix: str | None = None
    stream_kind: StreamKind = StreamKind.INTERVAL

    @property
    def is_empty(self) -> bool:
        return not self.records

    def summary_for(self, day: date) -> DaySummary | None:
        for summary in self.days:
            if summary.day == day:
                return summary
        return None

    def covers(self, first: date, last: date) -> bool:
        return self.first_day <= first and last <= self.last_day


@dataclass(frozen=True, slots=True)
class TrendColumn:
    """One column of the stacked area: an inclusive span of local days."""

    start: date
    end: date
    day_count: int
    axis_label: str
    """Short form for the date axis, where space is scarce."""

    detail_label: str
    """Full form for the tooltip, naming the span rather than abbreviating it."""

    clipped: bool = False
    """The calendar period runs past the requested range at one end."""


@dataclass(frozen=True, slots=True)
class TrendSeries:
    """One band of the stacked area: a category group across every column."""

    label: str
    swatch: Swatch
    values: list[int]
    """Seconds in each column, aligned index-for-index with
    :attr:`TrendsView.columns` -- or event counts, when
    :attr:`TrendsView.stream_kind` is ``EVENT``. Counting is summable the same
    way seconds are, so the rest of the chart (stacking, the daily-average
    axis) needs no change to plot one instead of the other; only the units on
    screen differ, which is a formatting decision, not a shape one."""

    total_seconds: int
    is_other: bool = False
    """True for the folded tail. It carries a neutral colour, not a hue."""

    can_drill: bool = False
    """Whether this group has sub-categories worth focusing into."""


@dataclass(frozen=True, slots=True)
class TrendsView:
    """A stacked area over consecutive calendar columns.

    Three properties matter for correctness rather than looks:

    **Every column between the first and last is present**, including empty
    ones. The first real export has a two-month gap in it; compressing that away
    would turn "I stopped tracking" into "these days were adjacent", which is a
    different and false claim.

    **The series set is fixed for the whole range**, decided by totals over all
    of it rather than over whatever is on screen. Scrolling therefore never
    changes which groups exist or what colour they are -- only the percentages,
    which describe the visible window.

    **A column's plotted height is its daily average, not its total.** A weekly
    column holding seven times as many seconds as a daily one would rescale the
    y axis by seven, so changing the interval would appear to change the data.
    Dividing by the days the column covers keeps the axis in hours-per-day and
    comparable across every interval -- and incidentally stops a half-week at
    the edge of a range from reading as a collapse in activity.
    """

    columns: list[TrendColumn]
    series: list[TrendSeries] = field(default_factory=list)
    prefix: str | None = None
    interval: Interval = DAILY_INTERVAL
    """Always resolved -- its unit is never :attr:`~minmax.core.columns.Granularity.AUTO`."""

    requested_interval: Interval = AUTO_INTERVAL
    """What the caller asked for, so a view can show "auto (weekly)"."""

    since: date | None = None
    until: date | None = None
    total_seconds: int = 0
    peak_daily_seconds: int = 0
    """Largest per-day average across the columns -- what sets the y ceiling."""

    stream_kind: StreamKind = StreamKind.INTERVAL
    """Whether :attr:`TrendSeries.values` (and the two totals above) hold
    seconds or event counts."""

    @property
    def is_empty(self) -> bool:
        return not self.columns or not self.series

    def daily_average(self, series_index: int, column_index: int) -> float:
        """Seconds per day for one series in one column -- the plotted height."""
        column = self.columns[column_index]
        return self.series[series_index].values[column_index] / max(1, column.day_count)


@dataclass(frozen=True, slots=True)
class RangeTotals:
    since: date
    until: date
    prefix: str | None
    slices: list[CategorySlice] = field(default_factory=list)
    total_seconds: int = 0
    stream_kind: StreamKind = StreamKind.INTERVAL


@dataclass(frozen=True, slots=True)
class CorrelationRow:
    """One (pair, interval) result, flattened for display.

    By default this is a pair's *best* interval -- the one with the largest
    ``|r|`` -- one row per pair. ``all_intervals=True`` on
    :meth:`MinmaxClient.correlations` instead emits one row per interval a
    pair was actually tested at, in which case ``stability``/``consistent_sign``
    (properties of the whole pair, not of one interval) repeat on every row.
    """

    x_key: str
    x_label: str
    y_key: str
    y_label: str
    interval_label: str
    n: int
    r: float
    p_value: float
    q_value: float
    stability: float
    consistent_sign: bool
    first_period: date
    last_period: date
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorrelationsView:
    """A ranked correlation sweep, ready to render."""

    rows: list[CorrelationRow]
    skipped_notes: list[str]
    """Human-readable, already aggregated -- see ``_summarize_skips``. Not
    one line per skipped (pair, interval): with a few thousand combinations
    tested, most skips share the same interval and reason."""
    tests_performed: int
    series_count: int
    truncated_series: int = 0
    first_day: date | None = None
    last_day: date | None = None

    @property
    def is_empty(self) -> bool:
        return not self.rows


# ----------------------------------------------------------------------
# Protocol
# ----------------------------------------------------------------------


@runtime_checkable
class MinmaxClient(Protocol):
    """The whole surface the UI is permitted to use."""

    def streams(self) -> list[StreamSummary]: ...

    def bounds(self, stream_key: str) -> tuple[date, date] | None:
        """First and last local day with data, or None when the stream is empty."""

    def day(self, stream_key: str, day: date, *, prefix: str | None = None) -> DayView: ...

    def window(
        self,
        stream_key: str,
        first_day: date,
        last_day: date,
        *,
        prefix: str | None = None,
    ) -> TimelineWindow:
        """A run of consecutive local days, for the continuously scrolling timeline."""

    def range_totals(
        self,
        stream_key: str,
        since: date,
        until: date,
        *,
        prefix: str | None = None,
    ) -> RangeTotals: ...

    def trends(
        self,
        stream_key: str,
        *,
        since: date | None = None,
        until: date | None = None,
        prefix: str | None = None,
        interval: Interval | Granularity = Granularity.AUTO,
        max_series: int = 8,
    ) -> TrendsView: ...

    def category_paths(self, stream_key: str) -> list[str]:
        """Every category path in the stream, sorted -- for filter completion."""

    def import_file(self, path: Path, *, stream_key: str, force: bool = False) -> ImportOutcome:
        """Archive, parse, and store one source file, exactly as the CLI's
        ``import`` command does."""

    def create_stream(
        self,
        key: str,
        name: str,
        kind: StreamKind,
        *,
        exclusive: bool = False,
        fields: Sequence[FieldSpec] = (),
    ) -> StreamSummary:
        """Declare a new stream, failing if the key is already taken."""

    def inspect_csv(self, path: Path) -> CsvPreview:
        """Read a CSV's header and a sample of its rows, and guess a column
        mapping -- what the new-stream dialog shows before anything is
        created. Never raises; a missing or unreadable file comes back as
        ``CsvPreview(error=...)``."""

    def set_auto_fetch(self, stream_key: str, *, enabled: bool, path: Path | None = None) -> None:
        """Turn a stream's once-a-minute re-import on or off.

        ``path``, when given, becomes the new remembered source; omitted, the
        previously remembered one (if any) is kept. Raises ``LookupError`` for
        an unknown stream; does not itself attempt an import -- the caller
        decides when to try the new path, typically once immediately so a bad
        choice is reported right away rather than on the next tick.
        """

    def correlations(
        self,
        *,
        stream_keys: Sequence[str] | None = None,
        since: date | None = None,
        until: date | None = None,
        sweep: Sequence[Interval] | None = None,
        min_periods: int = 8,
        min_r: float = 0.5,
        max_q: float | None = None,
        top: int | None = 20,
        per_category: bool = False,
        include_fields: bool = True,
        field_sum: bool = False,
        max_series: int = 40,
        all_intervals: bool = False,
    ) -> CorrelationsView:
        """Sweep every candidate pair of series across every declared stream
        (or just ``stream_keys``) for a correlation, and return a ranked,
        presentation-ready report.

        A thin, flattening wrapper over ``analysis.correlate`` -- see there
        for what every parameter actually controls (the sample-size and
        multiple-comparisons reasoning lives there, not here). ``sweep=None``
        uses the engine's own default ladder of period lengths.
        """


# ----------------------------------------------------------------------
# In-process implementation
# ----------------------------------------------------------------------


class InProcessClient:
    """Binds the protocol straight onto the repository, with no transport."""

    def __init__(
        self,
        repository: Repository,
        *,
        timezone: tzinfo | None = None,
        home: Path | None = None,
    ) -> None:
        self._repository = repository
        self._tz = timezone if timezone is not None else system_timezone()
        self._home = home
        """Where raw imports are archived. ``None`` means :meth:`import_file`
        cannot be used -- resolving a data directory is the storage layer's
        job, so a client built without one (as in most tests) simply does not
        support importing."""

    @property
    def timezone(self) -> tzinfo:
        """The timezone in which "a day" is interpreted for this client."""
        return self._tz

    # -- streams ---------------------------------------------------------

    def streams(self) -> list[StreamSummary]:
        summaries: list[StreamSummary] = []
        for stream in self._repository.list_streams():
            assert stream.id is not None
            bounds = self._repository.observed_range(stream.id)
            record_count = 0
            tracked_seconds = 0
            if bounds is not None:
                if stream.kind is StreamKind.EVENT:
                    counts = self._repository.counts_by_category(
                        stream.id, bounds[0], bounds[1] + 1
                    )
                    record_count = sum(total.record_count for total in counts)
                else:
                    totals = self._repository.totals_by_category(stream.id, bounds[0], bounds[1])
                    record_count = sum(total.record_count for total in totals)
                    tracked_seconds = sum(total.seconds for total in totals)
            summaries.append(
                StreamSummary(
                    key=stream.key,
                    name=stream.name,
                    kind=str(stream.kind),
                    exclusive=stream.exclusive,
                    record_count=record_count,
                    tracked_seconds=tracked_seconds,
                    first_day=self._local_date(bounds[0]) if bounds else None,
                    last_day=self._local_date(bounds[1]) if bounds else None,
                    watch_path=stream.watch_path,
                    watch_enabled=stream.watch_enabled,
                )
            )
        return summaries

    def bounds(self, stream_key: str) -> tuple[date, date] | None:
        stream_id = self._stream_id(stream_key)
        observed = self._repository.observed_range(stream_id)
        if observed is None:
            return None
        return self._local_date(observed[0]), self._local_date(observed[1])

    # -- views -----------------------------------------------------------

    def day(self, stream_key: str, day: date, *, prefix: str | None = None) -> DayView:
        stream = self._require_stream(stream_key)
        assert stream.id is not None
        start_epoch, end_epoch = local_day_bounds(day, self._tz)

        if stream.kind is StreamKind.EVENT:
            events = self._repository.events_in_range(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            counts = self._repository.counts_by_category(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            swatches = self._swatches_for(stream.id, (item.category_path for item in counts))
            total_count = sum(item.record_count for item in counts)
            records = self._events(events, swatches)
            totals = _event_slices(counts, swatches, total_count)
            tracked = total_count
        else:
            intervals = self._repository.intervals_in_range(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            interval_totals = self._repository.totals_by_category(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            swatches = self._swatches_for(
                stream.id, (item.category_path for item in interval_totals)
            )
            tracked = sum(total.seconds for total in interval_totals)
            records = self._records(intervals, swatches)
            totals = _slices(interval_totals, swatches, tracked)

        return DayView(
            day=day,
            day_start_epoch=start_epoch,
            day_end_epoch=end_epoch,
            records=records,
            totals=totals,
            hour_marks=[
                HourMark(epoch=epoch, label=self._clock_label(epoch))
                for epoch in hour_boundaries(start_epoch, end_epoch)
            ],
            tracked_seconds=tracked,
            prefix=prefix,
            stream_kind=stream.kind,
        )

    def window(
        self,
        stream_key: str,
        first_day: date,
        last_day: date,
        *,
        prefix: str | None = None,
    ) -> TimelineWindow:
        """Consecutive local days as one continuous timeline.

        Fetched as a single span rather than day by day so that a run crossing
        midnight is one record with no seam, and so extending the window while
        scrolling costs one query rather than one per day.
        """
        if last_day < first_day:
            raise ValueError(f"window ends before it starts: {first_day} -> {last_day}")

        stream = self._require_stream(stream_key)
        assert stream.id is not None
        day_count = (last_day - first_day).days + 1
        start_epoch, end_epoch = local_day_bounds(first_day, self._tz, days=day_count)
        is_event = stream.kind is StreamKind.EVENT

        if is_event:
            events = self._repository.events_in_range(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            swatches = self._swatches_for(stream.id, (item.category_path for item in events))
            records = self._events(events, swatches)
        else:
            intervals = self._repository.intervals_in_range(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            # One swatch lookup for the whole window: the colours must not
            # shift as the reader scrolls into a day where a group happens
            # not to appear.
            swatches = self._swatches_for(stream.id, (item.category_path for item in intervals))
            records = self._records(intervals, swatches)

        days: list[DaySummary] = []
        day_marks: list[DayMark] = []
        for offset in range(day_count):
            moment = first_day + timedelta(days=offset)
            bounds = local_day_bounds(moment, self._tz)
            if is_event:
                counts = self._repository.counts_by_category(
                    stream.id, bounds[0], bounds[1], path_prefix=prefix
                )
                tracked = sum(total.record_count for total in counts)
                day_totals = _event_slices(counts, swatches, tracked)
            else:
                day_interval_totals = self._repository.totals_by_category(
                    stream.id, bounds[0], bounds[1], path_prefix=prefix
                )
                tracked = sum(total.seconds for total in day_interval_totals)
                day_totals = _slices(day_interval_totals, swatches, tracked)
            days.append(
                DaySummary(
                    day=moment,
                    day_start_epoch=bounds[0],
                    day_end_epoch=bounds[1],
                    totals=day_totals,
                    tracked_seconds=tracked,
                )
            )
            day_marks.append(
                DayMark(epoch=bounds[0], day=moment, label=f"{moment:%a} {moment.day} {moment:%b}")
            )

        return TimelineWindow(
            first_day=first_day,
            last_day=last_day,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            records=records,
            hour_marks=[
                HourMark(epoch=epoch, label=self._clock_label(epoch))
                for epoch in hour_boundaries(start_epoch, end_epoch)
            ],
            day_marks=day_marks,
            days=days,
            prefix=prefix,
            stream_kind=stream.kind,
        )

    def range_totals(
        self,
        stream_key: str,
        since: date,
        until: date,
        *,
        prefix: str | None = None,
    ) -> RangeTotals:
        if until < since:
            raise ValueError(f"range ends before it starts: {since} -> {until}")

        stream = self._require_stream(stream_key)
        assert stream.id is not None
        start_epoch, end_epoch = local_day_bounds(since, self._tz, days=(until - since).days + 1)

        if stream.kind is StreamKind.EVENT:
            counts = self._repository.counts_by_category(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            swatches = self._swatches_for(stream.id, (total.category_path for total in counts))
            grand = sum(total.record_count for total in counts)
            slices = _event_slices(counts, swatches, grand)
        else:
            totals = self._repository.totals_by_category(
                stream.id, start_epoch, end_epoch, path_prefix=prefix
            )
            swatches = self._swatches_for(stream.id, (total.category_path for total in totals))
            grand = sum(total.seconds for total in totals)
            slices = _slices(totals, swatches, grand)

        return RangeTotals(
            since=since,
            until=until,
            prefix=prefix,
            slices=slices,
            total_seconds=grand,
            stream_kind=stream.kind,
        )

    def trends(
        self,
        stream_key: str,
        *,
        since: date | None = None,
        until: date | None = None,
        prefix: str | None = None,
        interval: Interval | Granularity = Granularity.AUTO,
        max_series: int = 8,
    ) -> TrendsView:
        """Composition over a range, as stacked series, one column per period.

        Reads the daily rollup table rather than the observations, so the cost
        is proportional to (days x categories) instead of to everything ever
        recorded -- a year of history is a few thousand aggregate rows. Weekly
        and monthly columns are summed from those same daily rows, which is why
        no second rollup period is needed to support them.

        ``prefix`` focuses on one group and splits it into its children, which
        is how per-timer detail is reached without ever putting more than
        ``max_series`` colours on screen at once. Beyond that limit the smallest
        groups fold into a neutral "Other"; a ninth categorical hue would not be
        reliably distinguishable from the eight already in use.

        ``interval`` accepts a bare :class:`Granularity` as well as an
        :class:`Interval`, so ``interval=Granularity.WEEK`` and
        ``interval=Interval(Granularity.WEEK, 2)`` are both ordinary calls.
        """
        requested = Interval.coerce(interval)
        stream = self._require_stream(stream_key)
        assert stream.id is not None
        stream_id = stream.id
        bounds = self.bounds(stream_key)
        if bounds is None:
            return TrendsView(
                columns=[], prefix=prefix, requested_interval=requested, stream_kind=stream.kind
            )

        first_day = since if since is not None else bounds[0]
        last_day = until if until is not None else bounds[1]
        if last_day < first_day:
            raise ValueError(f"range ends before it starts: {first_day} -> {last_day}")

        grouping = columns_for(first_day, last_day, requested)
        columns = [_trend_column(column) for column in grouping]
        empty = TrendsView(
            columns=columns,
            prefix=prefix,
            interval=grouping[0].interval,
            requested_interval=requested,
            since=first_day,
            until=last_day,
            stream_kind=stream.kind,
        )

        rows = self._repository.daily_totals_by_category(
            stream_id,
            start_bucket=first_day.isoformat(),
            end_bucket=last_day.isoformat(),
            path_prefix=prefix,
        )

        # A day belongs to exactly one column, so one lookup built up front
        # beats searching the column list for each of thousands of rollup rows.
        column_of = {
            day.isoformat(): index for index, column in enumerate(grouping) for day in column.days()
        }

        # An event stream's rollup rows carry seconds=0 -- there is nothing to
        # sum there -- so record_count (already summable across an event
        # stream's buckets; see rollups.apply_events) is what a column plots.
        is_event = stream.kind is StreamKind.EVENT

        totals: dict[str, list[int]] = {}
        drillable: set[str] = set()
        for row in rows:
            index = column_of.get(row.bucket)
            if index is None:
                continue
            key = _series_key(row.category_path, prefix)
            weight = row.record_count if is_event else row.seconds
            totals.setdefault(key, [0] * len(columns))[index] += weight
            if row.category_path != key:
                drillable.add(key)

        if not totals:
            return empty

        ranked = sorted(totals.items(), key=lambda item: (-sum(item[1]), item[0]))
        kept, folded = ranked[:max_series], ranked[max_series:]
        swatches = self._series_swatches(stream_id, [key for key, _ in kept], prefix)

        series = [
            TrendSeries(
                label=key,
                swatch=swatches[key],
                values=values,
                total_seconds=sum(values),
                can_drill=key in drillable,
            )
            for key, values in kept
        ]

        if folded:
            merged = [sum(values[index] for _, values in folded) for index in range(len(columns))]
            series.append(
                TrendSeries(
                    label=f"Other ({len(folded)})",
                    swatch=OTHER,
                    values=merged,
                    total_seconds=sum(merged),
                    is_other=True,
                )
            )

        # The y ceiling comes from the busiest column's *daily average*, so the
        # axis means the same thing -- hours in a day -- at every granularity.
        per_column = [sum(item.values[index] for item in series) for index in range(len(columns))]
        averages = [
            total / max(1, column.day_count)
            for total, column in zip(per_column, columns, strict=True)
        ]
        return TrendsView(
            columns=columns,
            series=series,
            prefix=prefix,
            interval=grouping[0].interval,
            requested_interval=requested,
            since=first_day,
            until=last_day,
            total_seconds=sum(per_column),
            peak_daily_seconds=round(max(averages, default=0.0)),
            stream_kind=stream.kind,
        )

    def category_paths(self, stream_key: str) -> list[str]:
        stream_id = self._stream_id(stream_key)
        return sorted(category.path for category in self._repository.list_categories(stream_id))

    # -- declaring streams -------------------------------------------------

    def create_stream(
        self,
        key: str,
        name: str,
        kind: StreamKind,
        *,
        exclusive: bool = False,
        fields: Sequence[FieldSpec] = (),
    ) -> StreamSummary:
        stream = self._repository.create_stream(key, name, kind, exclusive=exclusive, fields=fields)
        return StreamSummary(
            key=stream.key,
            name=stream.name,
            kind=str(stream.kind),
            exclusive=stream.exclusive,
            record_count=0,
            tracked_seconds=0,
            first_day=None,
            last_day=None,
        )

    def inspect_csv(self, path: Path) -> CsvPreview:
        if not path.is_file():
            return CsvPreview(error=f"no such file: {path}")
        try:
            header, sample = read_sample(path)
            fields = guess_fields(header, sample)
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            return CsvPreview(error=str(error))

        return CsvPreview(
            header=tuple(header),
            sample_rows=tuple(
                tuple((row.get(column) or "") for column in header) for row in sample[:5]
            ),
            suggested_fields=tuple(fields),
        )

    # -- importing ---------------------------------------------------------

    def import_file(self, path: Path, *, stream_key: str, force: bool = False) -> ImportOutcome:
        if self._home is None:
            raise RuntimeError(
                "this client has no data directory to archive imports into "
                "(construct InProcessClient with home=...)"
            )

        try:
            importer = importer_for(self._repository, stream_key)
        except (LookupError, ValueError) as error:
            return ImportOutcome(source_name=path.name, stream_key=stream_key, error=str(error))
        if not path.is_file():
            return ImportOutcome(source_name=path.name, error=f"no such file: {path}")

        try:
            report = run_import(self._repository, importer, path, home=self._home, force=force)
        except (OverlapError, ValueError) as error:
            # OverlapError means the data conflicts with what is already
            # stored; ValueError means the importer does not recognise the
            # file's shape at all (e.g. the wrong CSV). Either way nothing was
            # stored, and the reader needs to see why rather than a traceback.
            return ImportOutcome(source_name=path.name, stream_key=stream_key, error=str(error))

        return ImportOutcome(
            source_name=report.source_name,
            stream_key=report.stream_key,
            skipped=report.skipped,
            rows_read=report.rows_read,
            inserted=report.inserted,
            duplicates=report.duplicates,
            rejected=report.rejected,
            notes=tuple(report.notes),
        )

    def set_auto_fetch(self, stream_key: str, *, enabled: bool, path: Path | None = None) -> None:
        stream = self._require_stream(stream_key)
        assert stream.id is not None
        self._repository.set_stream_watch(
            stream.id, path=str(path) if path is not None else None, enabled=enabled
        )

    def correlations(
        self,
        *,
        stream_keys: Sequence[str] | None = None,
        since: date | None = None,
        until: date | None = None,
        sweep: Sequence[Interval] | None = None,
        min_periods: int = 8,
        min_r: float = 0.5,
        max_q: float | None = None,
        top: int | None = 20,
        per_category: bool = False,
        include_fields: bool = True,
        field_sum: bool = False,
        max_series: int = 40,
        all_intervals: bool = False,
    ) -> CorrelationsView:
        report = run_correlate(
            self._repository,
            stream_keys=stream_keys,
            since=since,
            until=until,
            sweep=sweep if sweep is not None else DEFAULT_SWEEP,
            min_periods=min_periods,
            min_r=min_r,
            max_q=max_q,
            top=top,
            per_category=per_category,
            include_fields=include_fields,
            field_sum=field_sum,
            max_series=max_series,
        )

        rows: list[CorrelationRow] = []
        for pair_result in report.pairs:
            shown = pair_result.by_interval if all_intervals else (pair_result.best,)
            for correlation in shown:
                rows.append(
                    CorrelationRow(
                        x_key=correlation.x.key,
                        x_label=correlation.x.label,
                        y_key=correlation.y.key,
                        y_label=correlation.y.label,
                        interval_label=correlation.interval.label,
                        n=correlation.n,
                        r=correlation.r,
                        p_value=correlation.p_value,
                        q_value=correlation.q_value,
                        stability=pair_result.stability,
                        consistent_sign=pair_result.consistent_sign,
                        first_period=correlation.first_period,
                        last_period=correlation.last_period,
                        caveats=correlation.caveats,
                    )
                )

        return CorrelationsView(
            rows=rows,
            skipped_notes=_summarize_skips(report.skipped),
            tests_performed=report.tests_performed,
            series_count=report.series_count,
            truncated_series=report.truncated_series,
            first_day=report.first_day,
            last_day=report.last_day,
        )

    # -- internals -------------------------------------------------------

    def _stream_id(self, stream_key: str) -> int:
        stream = self._require_stream(stream_key)
        assert stream.id is not None
        return stream.id

    def _require_stream(self, stream_key: str) -> Stream:
        """The stream, with its kind -- what ``day``/``window``/``range_totals``/
        ``trends`` all need to decide which observation table to read."""
        stream = self._repository.get_stream(stream_key)
        if stream is None or stream.id is None:
            raise LookupError(f"no stream {stream_key!r}")
        return stream

    def _local_date(self, epoch_seconds: int) -> date:
        return datetime.fromtimestamp(epoch_seconds, tz=self._tz).date()

    def _clock_label(self, epoch_seconds: int) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=self._tz).strftime("%H:%M")

    def _records(
        self, intervals: Sequence[StoredInterval], swatches: dict[str, Swatch]
    ) -> list[TimelineRecord]:
        """Turn stored observations into everything a view needs to draw them.

        Provenance is attached here rather than looked up when a record is
        inspected, so that clicking a block is not a second round trip -- the
        import table is a handful of rows however much history exists.
        """
        sources = {run.id: run.source_name for run in self._repository.list_imports()}
        records: list[TimelineRecord] = []
        for item in intervals:
            top_level = paths.top_level(item.category_path)
            records.append(
                TimelineRecord(
                    id=item.id,
                    category_path=item.category_path,
                    recorded_path=item.recorded_path,
                    top_level=top_level,
                    swatch=swatches.get(top_level, OTHER),
                    start_epoch=item.start.epoch_seconds,
                    end_epoch=item.end.epoch_seconds,
                    duration_seconds=item.duration_seconds,
                    # The record's own stored offset, not the client's timezone:
                    # a run recorded in another timezone should read as the clock
                    # the user actually saw at the time.
                    start_label=item.start.local_datetime.strftime("%H:%M"),
                    end_label=item.end.local_datetime.strftime("%H:%M"),
                    start_detail=item.start.local_datetime.strftime("%Y-%m-%d %H:%M:%S %z"),
                    end_detail=item.end.local_datetime.strftime("%Y-%m-%d %H:%M:%S %z"),
                    import_run_id=item.import_run_id,
                    source_name=sources.get(item.import_run_id, ""),
                )
            )
        return records

    def _events(
        self, events: Sequence[StoredEvent], swatches: dict[str, Swatch]
    ) -> list[TimelineRecord]:
        """The point-in-time analogue of :meth:`_records`.

        ``start_epoch == end_epoch`` and ``duration_seconds == 0``: there is
        no span, only an instant, so a renderer draws a marker rather than a
        block.
        """
        sources = {run.id: run.source_name for run in self._repository.list_imports()}
        records: list[TimelineRecord] = []
        for item in events:
            top_level = paths.top_level(item.category_path)
            clock = item.at.local_datetime.strftime("%H:%M")
            detail = item.at.local_datetime.strftime("%Y-%m-%d %H:%M:%S %z")
            records.append(
                TimelineRecord(
                    id=item.id,
                    category_path=item.category_path,
                    recorded_path=item.recorded_path,
                    top_level=top_level,
                    swatch=swatches.get(top_level, OTHER),
                    start_epoch=item.at.epoch_seconds,
                    end_epoch=item.at.epoch_seconds,
                    duration_seconds=0,
                    start_label=clock,
                    end_label=clock,
                    start_detail=detail,
                    end_detail=detail,
                    import_run_id=item.import_run_id,
                    source_name=sources.get(item.import_run_id, ""),
                    is_instant=True,
                    fields=tuple(
                        (value.label, _format_field_value(value.value, value.unit))
                        for value in item.values
                    ),
                )
            )
        return records

    def _series_swatches(
        self, stream_id: int, keys: Sequence[str], prefix: str | None
    ) -> dict[str, Swatch]:
        """Colours for trend series, stable against re-ranking.

        At the top level the stored per-group slots are used, so a group keeps
        its colour across sessions and matches the day timeline. Inside a focus,
        slots are assigned by sorted key order -- deterministic for that focus,
        and unaffected by which group happens to be largest today.
        """
        if prefix is None:
            slots = self._repository.ensure_group_slots(stream_id, sorted(keys))
            return {key: swatch_for_slot(slots[key]) for key in keys}
        return {key: swatch_for_slot(index) for index, key in enumerate(sorted(keys))}

    def _swatches_for(self, stream_id: int, category_paths: Iterable[str]) -> dict[str, Swatch]:
        """Ensure every group mentioned has a stored slot, then map it to a swatch."""
        groups = {paths.top_level(path) for path in category_paths}
        slots = self._repository.ensure_group_slots(stream_id, sorted(groups))
        return {group: swatch_for_slot(slot) for group, slot in slots.items()}


def _trend_column(column: Column) -> TrendColumn:
    """Attach the labels a chart draws to a calendar column.

    Built here rather than in the view for the same reason hour labels are:
    turning a span into words is a presentation decision every client must make
    identically, and a view computing it from an index would have to know the
    calendar rules all over again.
    """
    start, end = column.start, column.end
    single_month = column.granularity is Granularity.MONTH and column.interval.count == 1

    if single_month:
        axis = f"{start:%b} {start:%y}"
    elif column.granularity is Granularity.MONTH:
        # "Apr 26" on a column covering April through June reads as April alone,
        # because a month name is a complete answer in a way a start date is
        # not. A day or week column can name its first day and be understood.
        axis = f"{start:%b}–{end:%b} {end:%y}"  # noqa: RUF001
    else:
        axis = f"{start.day} {start:%b}"

    if start == end:
        # A one-day column, or a longer one clipped down to a single day. Either
        # way the honest label is the day itself, not the period it came from.
        detail = f"{start:%A} {start.day} {start:%b %Y}"
    elif single_month:
        detail = f"{start:%B %Y}"
    elif column.granularity is Granularity.MONTH:
        detail = _month_span(start, end)
    else:
        detail = _day_span(start, end)

    return TrendColumn(
        start=start,
        end=end,
        day_count=column.day_count,
        axis_label=axis,
        detail_label=detail,
        clipped=column.clipped,
    )


# An en dash is the correct mark for a span of dates.
_DASH = "–"  # noqa: RUF001


def _day_span(start: date, end: date) -> str:
    """``5 – 11 Jun 2026``, repeating only the parts that actually change."""  # noqa: RUF002
    if start.year != end.year:
        return f"{start.day} {start:%b %Y} {_DASH} {end.day} {end:%b %Y}"
    if start.month != end.month:
        return f"{start.day} {start:%b} {_DASH} {end.day} {end:%b %Y}"
    return f"{start.day} {_DASH} {end.day} {end:%b %Y}"


def _month_span(start: date, end: date) -> str:
    """``January – March 2026``, for a column covering several whole months."""  # noqa: RUF002
    if start.year != end.year:
        return f"{start:%B %Y} {_DASH} {end:%B %Y}"
    return f"{start:%B} {_DASH} {end:%B %Y}"


def _series_key(category_path: str, prefix: str | None) -> str:
    """The group a category belongs to in a trends view.

    Without a focus that is the top-level group (``media`` for
    ``media/gaming/chess``). Inside a focus it is one level deeper, so focusing
    ``media`` splits it into ``media/anime``, ``media/gaming``, and so on --
    which is how per-timer detail is reached one readable level at a time.
    """
    components = paths.components(category_path)
    depth = len(paths.components(prefix)) + 1 if prefix else 1
    return paths.SEPARATOR.join(components[:depth])


def _slices(
    totals: Sequence[CategoryTotal], swatches: dict[str, Swatch], grand_total: int
) -> list[CategorySlice]:
    result: list[CategorySlice] = []
    for total in totals:
        top_level = paths.top_level(total.category_path)
        result.append(
            CategorySlice(
                category_path=total.category_path,
                top_level=top_level,
                swatch=swatches.get(top_level, OTHER),
                seconds=total.seconds,
                record_count=total.record_count,
                share=(total.seconds / grand_total) if grand_total else 0.0,
            )
        )
    return result


def _event_slices(
    totals: Sequence[EventCategoryTotal], swatches: dict[str, Swatch], grand_count: int
) -> list[CategorySlice]:
    """The event analogue of :func:`_slices`.

    ``seconds`` is always 0 here -- an event has no duration -- and
    ``record_count`` is the count a reader actually wants; ``share`` is
    computed against it instead of against seconds.
    """
    result: list[CategorySlice] = []
    for total in totals:
        top_level = paths.top_level(total.category_path)
        result.append(
            CategorySlice(
                category_path=total.category_path,
                top_level=top_level,
                swatch=swatches.get(top_level, OTHER),
                seconds=0,
                record_count=total.record_count,
                share=(total.record_count / grand_count) if grand_count else 0.0,
            )
        )
    return result


def _format_field_value(value: float | str, unit: str | None) -> str:
    """A declared field's value, formatted for display.

    Numbers get a fixed-but-trimmed decimal form (``61`` not ``61.0``,
    ``47.6062`` kept exact) and their declared unit, if any; text is shown
    verbatim.
    """
    if isinstance(value, str):
        return value
    text = f"{value:g}"
    return f"{text} {unit}" if unit else text


def _summarize_skips(skipped: Sequence[Skipped]) -> list[str]:
    """Group skipped (pair, interval) combinations into a few readable lines.

    Not one line per skip: a sweep with a short date range can skip most of
    its combinations at the coarser intervals, and a reader needs "3 weeks:
    140 combinations skipped" rather than 140 near-identical lines.
    """
    if not skipped:
        return []
    by_interval: dict[Interval, list[Skipped]] = {}
    for skip in skipped:
        by_interval.setdefault(skip.interval, []).append(skip)

    notes: list[str] = []
    for interval, entries in sorted(by_interval.items(), key=lambda kv: (kv[0].unit, kv[0].count)):
        best_n = max(entry.n for entry in entries)
        notes.append(
            f"{interval.label}: {len(entries)} combination(s) skipped "
            f"(best case had {best_n} usable period(s))"
        )
    return notes
