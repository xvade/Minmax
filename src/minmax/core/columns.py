"""Grouping a range of local days into the columns a trends chart plots.

The trends screen started with one column per day, which stops working once
history is long: a decade is 3,650 columns, and at the two-pixel floor that is
still seven metres of scrolling to cross. Aggregating into weeks or months is
the fix, and this module is the arithmetic for it.

Columns are **aligned to the calendar**, not to the start of the range. A week
column begins on a Monday and a month column on the first, so the same Tuesday
lands in the same column no matter which day the range happens to begin. The
alternative -- chopping the range into fixed-size chunks from its first day --
produces columns that shift under the reader whenever the range changes, and
"the week of the 5th" stops meaning anything.

The first and last columns are then **clipped to the requested range** rather
than reaching outside it. A range is a claim about which days were asked for,
and a column covering days outside it would report time the reader did not ask
to see. Clipped columns are flagged so the view can say so.

A column covers an :class:`Interval` -- a unit and a count -- so "five days" or
"two weeks" or "a quarter" are all sayable, not just the three units. Multi-unit
intervals are anchored to a fixed epoch rather than to the range, for the same
reason single ones are aligned to the calendar: a fortnight column that begins
wherever the range happens to begin is a different fortnight every time the
range moves, and comparing two such charts would be meaningless. Anchoring makes
the columns a property of the calendar, so the same Tuesday is always in the
same one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

__all__ = [
    "AUTO_INTERVAL",
    "DAILY_INTERVAL",
    "MAX_INTERVAL_COUNT",
    "Column",
    "Granularity",
    "Interval",
    "choose_granularity",
    "columns_for",
]

#: Above this many days a per-day column is too narrow to read, and above the
#: second a per-week column is too. Chosen from the pixel floor: at the two-pixel
#: minimum a 1,000px plot holds roughly 500 columns, so ~120 days keeps a day
#: column comfortably wide, and ~2.2 years of weeks stays under the same bar.
_AUTO_DAY_LIMIT = 120
_AUTO_WEEK_LIMIT = 800

#: A ceiling on the multiplier, so a mistyped count cannot ask for a column
#: measured in centuries. Twelve covers the intervals anyone reads a chart in --
#: a fortnight, a quarter, a year -- with room to spare.
MAX_INTERVAL_COUNT = 12

#: The Monday multi-unit intervals count from. Any fixed date would do; a Monday
#: is chosen so that an interval of seven days lands on exactly the same
#: boundaries as an interval of one week, rather than a day off it.
_ANCHOR = date(1970, 1, 5)


class Granularity(StrEnum):
    """The unit of time one chart column is measured in."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    AUTO = "auto"
    """Resolved against the span by :func:`choose_granularity` before use."""


@dataclass(frozen=True, slots=True)
class Interval:
    """How much time one chart column covers: a count of some unit.

    ``Interval(Granularity.WEEK)`` is one week; ``Interval(Granularity.WEEK, 2)``
    is a fortnight. The count exists because the three units alone are a coarse
    ladder -- a year of history is either 365 unreadable columns or 12 that hide
    every week-to-week change, with nothing in between.
    """

    unit: Granularity
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"an interval covers at least one {self.unit}, got {self.count}")
        if self.count > MAX_INTERVAL_COUNT:
            raise ValueError(f"interval count above {MAX_INTERVAL_COUNT}: {self.count}")
        if self.unit is Granularity.AUTO and self.count != 1:
            raise ValueError("AUTO picks its own unit, so it cannot be multiplied")

    @classmethod
    def coerce(cls, interval: Interval | Granularity) -> Interval:
        """Accept either form, so callers with a bare unit need no ceremony."""
        return interval if isinstance(interval, Interval) else cls(interval)

    @property
    def is_auto(self) -> bool:
        return self.unit is Granularity.AUTO

    @property
    def label(self) -> str:
        """The interval as a noun phrase: ``week``, ``3 days``, ``2 weeks``."""
        if self.count == 1:
            return str(self.unit)
        return f"{self.count} {self.unit}s"

    def resolved(self, day_span: int) -> Interval:
        """This interval with :attr:`Granularity.AUTO` replaced by a real unit."""
        return Interval(choose_granularity(day_span)) if self.is_auto else self


#: Named so they can be dataclass defaults. An ``Interval`` is immutable, but a
#: constructor call in a default is worth avoiding regardless -- it reads as
#: shared mutable state whether or not it is.
AUTO_INTERVAL = Interval(Granularity.AUTO)
DAILY_INTERVAL = Interval(Granularity.DAY)


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a trends chart: an inclusive span of local days."""

    start: date
    end: date
    interval: Interval
    """Always resolved -- its unit is never :attr:`Granularity.AUTO`."""

    clipped: bool = False
    """The interval's period extends past the requested range at one end."""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"column ends before it starts: {self.start} -> {self.end}")
        if self.interval.is_auto:
            raise ValueError("a column needs a resolved granularity, not AUTO")

    @property
    def granularity(self) -> Granularity:
        """The interval's unit. Most readers care about this and not the count."""
        return self.interval.unit

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def days(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(self.day_count)]


def choose_granularity(day_span: int) -> Granularity:
    """The granularity that keeps columns readable for a span of ``day_span`` days.

    Used for :attr:`Granularity.AUTO`, which is the default: a reader opening
    the trends screen on ten years of history should not have to know that
    monthly columns exist before the chart becomes legible.
    """
    if day_span <= _AUTO_DAY_LIMIT:
        return Granularity.DAY
    if day_span <= _AUTO_WEEK_LIMIT:
        return Granularity.WEEK
    return Granularity.MONTH


def columns_for(first: date, last: date, interval: Interval | Granularity) -> list[Column]:
    """Anchored columns of ``interval`` covering ``[first, last]`` inclusive.

    Every day in the range lands in exactly one column, and no column reaches
    outside the range. With :attr:`Granularity.AUTO` the unit is chosen from the
    span first. A bare :class:`Granularity` is accepted and means a count of one.
    """
    if last < first:
        raise ValueError(f"range ends before it starts: {first} -> {last}")

    resolved = Interval.coerce(interval).resolved((last - first).days + 1)

    columns: list[Column] = []
    cursor = first
    while cursor <= last:
        period_start, period_end = _period_bounds(cursor, resolved)
        start = max(period_start, first)
        end = min(period_end, last)
        columns.append(
            Column(
                start=start,
                end=end,
                interval=resolved,
                clipped=start != period_start or end != period_end,
            )
        )
        cursor = end + timedelta(days=1)

    return columns


def _period_bounds(day: date, interval: Interval) -> tuple[date, date]:
    """The period of ``interval`` that ``day`` falls in, unclipped.

    Multi-unit periods are counted from :data:`_ANCHOR` rather than from the
    range, so which period a day belongs to depends only on the day. Floor
    division does the counting, which keeps dates before the anchor working:
    ``-1 // 3`` is ``-1``, so the day lands in the period ending at the anchor
    rather than in the one starting there.
    """
    count = interval.count
    if interval.unit is Granularity.DAY:
        index = (day - _ANCHOR).days // count
        start = _ANCHOR + timedelta(days=index * count)
        return start, start + timedelta(days=count - 1)
    if interval.unit is Granularity.WEEK:
        # ISO weeks: Monday first. weekday() is 0 on Monday, so this walks back
        # to the start of the week the day belongs to.
        week_start = day - timedelta(days=day.weekday())
        index = (week_start - _ANCHOR).days // 7 // count
        start = _ANCHOR + timedelta(weeks=index * count)
        return start, start + timedelta(weeks=count, days=-1)
    if interval.unit is Granularity.MONTH:
        # Months are not a fixed number of days, so they are counted rather than
        # measured. A count of 3 anchored this way gives calendar quarters, and
        # 12 gives calendar years, which is what a reader expects of both.
        index = (day.year * 12 + day.month - 1) // count * count
        start = date(index // 12, index % 12 + 1, 1)
        return start, _add_months(start, count) - timedelta(days=1)
    raise ValueError(f"unresolved granularity: {interval.unit}")


def _add_months(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + first_of_month.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)
