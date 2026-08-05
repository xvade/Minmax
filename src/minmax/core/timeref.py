"""Time representation for Minmax.

Every instant Minmax stores carries two numbers: the UTC epoch second, and the
UTC offset that was in effect for the user at that moment.

Storing UTC alone would be lossy for our purposes. Nearly every question a life
tracker is asked -- "how much did I sleep Tuesday", "what do my mornings look
like" -- is a question about *local wall-clock time*. Across a daylight-saving
transition or a flight, UTC-only timestamps silently reassign records to the
wrong local day. Keeping the offset makes the original local reading exactly
recoverable, and turns "which local day was this?" into a stored fact rather
than a guess made at query time.

The conversion trick used throughout: local wall-clock seconds are
``epoch_seconds + utc_offset_seconds``, which can then be read as if it were
UTC to recover the local calendar fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from enum import Enum

__all__ = ["Instant", "Period", "local_day_bounds", "split_by_local_period"]

_UTC = UTC
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86_400

# Offsets on Earth run from -12:00 to +14:00. Anything outside that is a parse
# error rather than a real place, and we would rather fail loudly on import.
_MIN_OFFSET_SECONDS = -12 * _SECONDS_PER_HOUR
_MAX_OFFSET_SECONDS = 14 * _SECONDS_PER_HOUR


@dataclass(frozen=True, slots=True, order=True)
class Instant:
    """A point in time, together with the UTC offset in effect there.

    Ordering is chronological -- ``epoch_seconds`` dominates -- with the offset
    as a deterministic tiebreaker so that sorting is a total order. Two instants
    describing the same moment under different offsets are therefore ordered but
    not ``==``: the local reading differs, and preserving that is the whole
    point of the type.
    """

    epoch_seconds: int
    utc_offset_seconds: int

    def __post_init__(self) -> None:
        if not _MIN_OFFSET_SECONDS <= self.utc_offset_seconds <= _MAX_OFFSET_SECONDS:
            raise ValueError(
                f"utc_offset_seconds {self.utc_offset_seconds} is outside the range of "
                f"real UTC offsets ({_MIN_OFFSET_SECONDS}..{_MAX_OFFSET_SECONDS})"
            )

    # -- construction ----------------------------------------------------

    @classmethod
    def from_datetime(cls, value: datetime) -> Instant:
        """Build from a timezone-aware ``datetime``.

        Naive datetimes are rejected: guessing a timezone is exactly the class of
        silent error this type exists to prevent.
        """
        offset = value.utcoffset()
        if offset is None:
            raise ValueError(f"datetime {value!r} is naive; a UTC offset is required")
        return cls(
            epoch_seconds=int(value.timestamp()),
            utc_offset_seconds=int(offset.total_seconds()),
        )

    @classmethod
    def from_iso(cls, text: str) -> Instant:
        """Parse an ISO 8601 timestamp that includes an offset (e.g. Timecard's)."""
        return cls.from_datetime(datetime.fromisoformat(text))

    # -- local readings --------------------------------------------------

    @property
    def local_wall_seconds(self) -> int:
        """Seconds since the epoch as read on a local wall clock."""
        return self.epoch_seconds + self.utc_offset_seconds

    @property
    def local_datetime(self) -> datetime:
        """The local calendar reading, carrying this instant's fixed offset."""
        return datetime.fromtimestamp(
            self.epoch_seconds, tz=timezone(timedelta(seconds=self.utc_offset_seconds))
        )

    @property
    def local_date(self) -> date:
        """The local calendar date -- the day the user would say this happened."""
        return datetime.fromtimestamp(self.local_wall_seconds, tz=_UTC).date()

    def to_utc_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.epoch_seconds, tz=_UTC)

    def bucket_key(self, period: Period) -> str:
        """The local rollup bucket this instant falls in (see :class:`Period`)."""
        return period.key_for_wall_seconds(self.local_wall_seconds)


class Period(Enum):
    """A local-time bucketing granularity for rollups.

    Values are the SQL-friendly bucket key formats. Keys sort lexicographically
    in chronological order, which is what lets range queries over rollup tables
    be plain string comparisons.
    """

    HOUR = "hour"
    DAY = "day"

    @property
    def seconds(self) -> int:
        return _SECONDS_PER_HOUR if self is Period.HOUR else _SECONDS_PER_DAY

    def key_for_wall_seconds(self, wall_seconds: int) -> str:
        moment = datetime.fromtimestamp(wall_seconds, tz=_UTC)
        if self is Period.HOUR:
            return moment.strftime("%Y-%m-%dT%H")
        return moment.strftime("%Y-%m-%d")

    def floor_wall_seconds(self, wall_seconds: int) -> int:
        """Start of the bucket containing ``wall_seconds``, in wall seconds.

        Floor division (not truncation) so this stays correct for the negative
        wall-clock seconds that pre-1970 timestamps would produce.
        """
        return (wall_seconds // self.seconds) * self.seconds


def split_by_local_period(start: Instant, end: Instant, period: Period) -> list[tuple[str, int]]:
    """Split the half-open interval ``[start, end)`` into per-bucket seconds.

    Returns ``(bucket_key, seconds)`` pairs in chronological order, whose seconds
    sum exactly to ``end - start``. An interval that crosses local midnight stays
    a single stored row but is *attributed* across the days it touches -- 29 of
    the 338 runs in the first Timecard export do exactly this (sleep, mostly), so
    without splitting, daily totals would be badly wrong.

    A zero-length or inverted interval yields no buckets.

    Offset handling: bucket boundaries between the two endpoints are computed
    using whichever endpoint's offset is nearer in time. This is exact unless a
    single interval both spans a DST transition *and* crosses a bucket boundary
    on the far side of it, in which case at most one hour is attributed to an
    adjacent bucket. No stored data is affected -- only the derived rollup.
    """
    if end.epoch_seconds <= start.epoch_seconds:
        return []

    pieces: list[tuple[str, int]] = []
    cursor = start.epoch_seconds

    while cursor < end.epoch_seconds:
        offset = _offset_near(cursor, start, end)
        wall = cursor + offset
        bucket = period.key_for_wall_seconds(wall)

        # Epoch second at which the *next* bucket begins, under this offset.
        next_boundary = period.floor_wall_seconds(wall) + period.seconds - offset

        # Guard against an offset change making the boundary non-advancing; a
        # bucket must always consume at least one second or this would not
        # terminate.
        if next_boundary <= cursor:
            next_boundary = cursor + period.seconds

        piece_end = min(next_boundary, end.epoch_seconds)
        pieces.append((bucket, piece_end - cursor))
        cursor = piece_end

    return _merge_adjacent(pieces)


def local_day_bounds(first_day: date, tz: tzinfo, *, days: int = 1) -> tuple[int, int]:
    """Epoch bounds of ``[first_day 00:00, first_day+days 00:00)`` in ``tz``.

    The timezone is required rather than read from the environment, so that
    ``core`` stays free of environment access and a caller can ask about a day
    somewhere other than where the machine happens to be.

    Note that the span is not always ``days * 86400`` seconds: across a DST
    transition a local day is 23 or 25 hours long, and callers that size a
    timeline from these bounds render that correctly for free.
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(first_day + timedelta(days=days), datetime.min.time(), tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _offset_near(epoch_seconds: int, start: Instant, end: Instant) -> int:
    """The offset of whichever endpoint is nearer to ``epoch_seconds``."""
    to_start = abs(epoch_seconds - start.epoch_seconds)
    to_end = abs(end.epoch_seconds - epoch_seconds)
    return start.utc_offset_seconds if to_start <= to_end else end.utc_offset_seconds


def _merge_adjacent(pieces: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Combine consecutive pieces sharing a bucket key.

    Only reachable when an offset change lands two slices in the same local
    bucket, but it keeps the postcondition "one entry per bucket" unconditional.
    """
    merged: list[tuple[str, int]] = []
    for key, seconds in pieces:
        if merged and merged[-1][0] == key:
            merged[-1] = (key, merged[-1][1] + seconds)
        else:
            merged.append((key, seconds))
    return merged
