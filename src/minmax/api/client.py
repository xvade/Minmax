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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Protocol, runtime_checkable

from minmax.core import paths
from minmax.core.models import CategoryTotal
from minmax.core.palette import OTHER, Swatch, swatch_for_slot
from minmax.core.timeline import hour_boundaries
from minmax.core.timeref import local_day_bounds
from minmax.storage.repository import Repository

__all__ = [
    "CategorySlice",
    "DayView",
    "HourMark",
    "InProcessClient",
    "MinmaxClient",
    "RangeTotals",
    "StreamSummary",
    "TimelineRecord",
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
class StreamSummary:
    key: str
    name: str
    kind: str
    exclusive: bool
    record_count: int
    tracked_seconds: int
    first_day: date | None
    last_day: date | None


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
class DayView:
    """Everything the timeline needs for one local day, in one call."""

    day: date
    day_start_epoch: int
    day_end_epoch: int
    records: list[TimelineRecord] = field(default_factory=list)
    totals: list[CategorySlice] = field(default_factory=list)
    hour_marks: list[HourMark] = field(default_factory=list)
    tracked_seconds: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.records


@dataclass(frozen=True, slots=True)
class TrendSeries:
    """One band of the stacked area: a category group across every day."""

    label: str
    swatch: Swatch
    values: list[int]
    """Seconds on each day, aligned index-for-index with :attr:`TrendsView.days`."""

    total_seconds: int
    is_other: bool = False
    """True for the folded tail. It carries a neutral colour, not a hue."""

    can_drill: bool = False
    """Whether this group has sub-categories worth focusing into."""


@dataclass(frozen=True, slots=True)
class TrendsView:
    """A stacked area over consecutive local days.

    Two properties matter for correctness rather than looks:

    **Every day between the first and last is present**, including days with no
    data. The first real export has a two-month gap in it; compressing that away
    would turn "I stopped tracking" into "these days were adjacent", which is a
    different and false claim.

    **The series set is fixed for the whole range**, decided by totals over all
    of it rather than over whatever is on screen. Scrolling therefore never
    changes which groups exist or what colour they are -- only the percentages,
    which describe the visible window.
    """

    days: list[date]
    series: list[TrendSeries] = field(default_factory=list)
    prefix: str | None = None
    total_seconds: int = 0
    peak_day_seconds: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.days or not self.series


@dataclass(frozen=True, slots=True)
class RangeTotals:
    since: date
    until: date
    prefix: str | None
    slices: list[CategorySlice] = field(default_factory=list)
    total_seconds: int = 0


# ----------------------------------------------------------------------
# Protocol
# ----------------------------------------------------------------------


@runtime_checkable
class MinmaxClient(Protocol):
    """The whole surface the UI is permitted to use."""

    def streams(self) -> list[StreamSummary]: ...

    def bounds(self, stream_key: str) -> tuple[date, date] | None:
        """First and last local day with data, or None when the stream is empty."""

    def day(self, stream_key: str, day: date) -> DayView: ...

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
        max_series: int = 8,
    ) -> TrendsView: ...

    def category_paths(self, stream_key: str) -> list[str]:
        """Every category path in the stream, sorted -- for filter completion."""


# ----------------------------------------------------------------------
# In-process implementation
# ----------------------------------------------------------------------


class InProcessClient:
    """Binds the protocol straight onto the repository, with no transport."""

    def __init__(self, repository: Repository, *, timezone: tzinfo | None = None) -> None:
        self._repository = repository
        self._tz = timezone if timezone is not None else system_timezone()

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
            totals = (
                self._repository.totals_by_category(stream.id, bounds[0], bounds[1])
                if bounds is not None
                else []
            )
            summaries.append(
                StreamSummary(
                    key=stream.key,
                    name=stream.name,
                    kind=str(stream.kind),
                    exclusive=stream.exclusive,
                    record_count=sum(total.record_count for total in totals),
                    tracked_seconds=sum(total.seconds for total in totals),
                    first_day=self._local_date(bounds[0]) if bounds else None,
                    last_day=self._local_date(bounds[1]) if bounds else None,
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

    def day(self, stream_key: str, day: date) -> DayView:
        stream_id = self._stream_id(stream_key)
        start_epoch, end_epoch = local_day_bounds(day, self._tz)

        intervals = self._repository.intervals_in_range(stream_id, start_epoch, end_epoch)
        totals = self._repository.totals_by_category(stream_id, start_epoch, end_epoch)
        swatches = self._swatches_for(stream_id, (item.category_path for item in totals))

        records = [
            TimelineRecord(
                id=item.id,
                category_path=item.category_path,
                recorded_path=item.recorded_path,
                top_level=paths.top_level(item.category_path),
                swatch=swatches[paths.top_level(item.category_path)],
                start_epoch=item.start.epoch_seconds,
                end_epoch=item.end.epoch_seconds,
                duration_seconds=item.duration_seconds,
                # The record's own stored offset, not the client's timezone: a
                # run recorded in another timezone should read as the clock the
                # user actually saw at the time.
                start_label=item.start.local_datetime.strftime("%H:%M"),
                end_label=item.end.local_datetime.strftime("%H:%M"),
            )
            for item in intervals
        ]

        tracked = sum(total.seconds for total in totals)
        return DayView(
            day=day,
            day_start_epoch=start_epoch,
            day_end_epoch=end_epoch,
            records=records,
            totals=_slices(totals, swatches, tracked),
            hour_marks=[
                HourMark(epoch=epoch, label=self._clock_label(epoch))
                for epoch in hour_boundaries(start_epoch, end_epoch)
            ],
            tracked_seconds=tracked,
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

        stream_id = self._stream_id(stream_key)
        start_epoch, end_epoch = local_day_bounds(since, self._tz, days=(until - since).days + 1)
        totals = self._repository.totals_by_category(
            stream_id, start_epoch, end_epoch, path_prefix=prefix
        )
        swatches = self._swatches_for(stream_id, (total.category_path for total in totals))
        grand = sum(total.seconds for total in totals)

        return RangeTotals(
            since=since,
            until=until,
            prefix=prefix,
            slices=_slices(totals, swatches, grand),
            total_seconds=grand,
        )

    def trends(
        self,
        stream_key: str,
        *,
        since: date | None = None,
        until: date | None = None,
        prefix: str | None = None,
        max_series: int = 8,
    ) -> TrendsView:
        """Daily composition over a range, as stacked series.

        Reads the daily rollup table rather than the observations, so the cost
        is proportional to (days x categories) instead of to everything ever
        recorded -- a year of history is a few thousand aggregate rows.

        ``prefix`` focuses on one group and splits it into its children, which
        is how per-timer detail is reached without ever putting more than
        ``max_series`` colours on screen at once. Beyond that limit the smallest
        groups fold into a neutral "Other"; a ninth categorical hue would not be
        reliably distinguishable from the eight already in use.
        """
        stream_id = self._stream_id(stream_key)
        bounds = self.bounds(stream_key)
        if bounds is None:
            return TrendsView(days=[], prefix=prefix)

        first_day = since if since is not None else bounds[0]
        last_day = until if until is not None else bounds[1]
        if last_day < first_day:
            raise ValueError(f"range ends before it starts: {first_day} -> {last_day}")

        rows = self._repository.daily_totals_by_category(
            stream_id,
            start_bucket=first_day.isoformat(),
            end_bucket=last_day.isoformat(),
            path_prefix=prefix,
        )

        # Every day in the range gets a column, including the empty ones.
        days = [
            first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)
        ]
        index_of = {day.isoformat(): position for position, day in enumerate(days)}

        totals: dict[str, list[int]] = {}
        drillable: set[str] = set()
        for row in rows:
            key = _series_key(row.category_path, prefix)
            position = index_of.get(row.bucket)
            if position is None:
                continue
            totals.setdefault(key, [0] * len(days))[position] += row.seconds
            if row.category_path != key:
                drillable.add(key)

        if not totals:
            return TrendsView(days=days, prefix=prefix)

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
            merged = [sum(values[day] for _, values in folded) for day in range(len(days))]
            series.append(
                TrendSeries(
                    label=f"Other ({len(folded)})",
                    swatch=OTHER,
                    values=merged,
                    total_seconds=sum(merged),
                    is_other=True,
                )
            )

        per_day = [sum(item.values[day] for item in series) for day in range(len(days))]
        return TrendsView(
            days=days,
            series=series,
            prefix=prefix,
            total_seconds=sum(per_day),
            peak_day_seconds=max(per_day, default=0),
        )

    def category_paths(self, stream_key: str) -> list[str]:
        stream_id = self._stream_id(stream_key)
        return sorted(category.path for category in self._repository.list_categories(stream_id))

    # -- internals -------------------------------------------------------

    def _stream_id(self, stream_key: str) -> int:
        stream = self._repository.get_stream(stream_key)
        if stream is None or stream.id is None:
            raise LookupError(f"no stream {stream_key!r}")
        return stream.id

    def _local_date(self, epoch_seconds: int) -> date:
        return datetime.fromtimestamp(epoch_seconds, tz=self._tz).date()

    def _clock_label(self, epoch_seconds: int) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=self._tz).strftime("%H:%M")

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
