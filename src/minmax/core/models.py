"""Minmax's domain model.

Everything here is a plain, frozen value object with no I/O and no knowledge of
SQL. Importers produce these; the repository stores and returns them.

The central abstraction is :class:`StreamKind`. Every datastream Minmax will
ever ingest reduces to one of three shapes, and modelling the *shapes* rather
than the sources is what keeps the eventual correlation engine from needing a
special case per source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from minmax.core import paths
from minmax.core.timeref import Instant

__all__ = [
    "Category",
    "CategoryTotal",
    "ImportRun",
    "IntervalRecord",
    "PeriodTotal",
    "StoredInterval",
    "Stream",
    "StreamKind",
]


class StreamKind(StrEnum):
    """The shape of a datastream's observations.

    - ``INTERVAL``: something was true from a start to an end (timetracking,
      focused app, sleep phases). ~10^5-10^6 rows/year.
    - ``EVENT``: something happened at an instant (commits, purchases, doses).
      ~10^3-10^5 rows/year.
    - ``SAMPLE``: a number had a value at an instant (heart rate, weight, CPU
      load). ~10^6-10^8 rows/year -- the only kind with a scale problem, and the
      reason rollups exist.

    Only ``INTERVAL`` has storage implemented today; the other two get their
    tables when the first such stream lands. Adding a table is a migration, not
    a redesign, so there is nothing to gain by creating them empty now.
    """

    INTERVAL = "interval"
    EVENT = "event"
    SAMPLE = "sample"


@dataclass(frozen=True, slots=True)
class Stream:
    """One source of time-bound data.

    ``exclusive`` records that at most one observation in this stream can be
    true at a time, so overlapping records are a data error rather than a normal
    occurrence. Timecard is exclusive (one timer runs at a time), and so is a
    focused-app stream; a calendar stream would not be. It is a per-stream
    property precisely because it is not true of interval streams in general.
    """

    key: str
    name: str
    kind: StreamKind
    exclusive: bool = False
    id: int | None = None


@dataclass(frozen=True, slots=True)
class Category:
    """A label within a stream, identified by a hierarchical path.

    ``source_id`` is the source's own stable identifier where it provides one.
    Timecard's ``timer_id`` survives renames and folder moves, so it -- not the
    path -- is the category's identity. ``path`` holds the most recently seen
    path for the category; the path in effect when a given observation was
    recorded is kept on that observation instead.

    In the first Timecard export, one timer appears under two names
    (``social`` and later ``social/friends``), which is exactly the case this
    split exists to handle without splitting that timer's history in two.
    """

    stream_id: int
    source_id: str | None
    path: str
    id: int | None = None

    @property
    def leaf(self) -> str:
        return paths.leaf(self.path)

    @property
    def top_level(self) -> str:
        return paths.top_level(self.path)


@dataclass(frozen=True, slots=True)
class IntervalRecord:
    """One interval observation, as produced by an importer before storage.

    ``duration_seconds`` is always *derived* from the timestamps rather than
    taken from the source. Timecard's exported ``duration_seconds`` disagrees
    with ``stopped_at - started_at`` on 27% of rows, always by exactly one
    second, because the export truncates sub-second components independently.
    Trusting the source's figure would mean a day's durations no longer summed
    to the time the timeline visibly covers. The source's own figure is kept in
    ``source_duration_seconds`` for auditing, never for arithmetic.
    """

    category_source_id: str | None
    category_path: str
    start: Instant
    end: Instant
    source_duration_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.end.epoch_seconds < self.start.epoch_seconds:
            raise ValueError(f"interval ends before it starts: {self.start!r} -> {self.end!r}")
        if self.category_source_id is None and not self.category_path:
            raise ValueError("an interval needs either a category source id or a path")
        # Normalizing here means a stray space in a source file cannot silently
        # create a near-duplicate category.
        object.__setattr__(self, "category_path", paths.normalize(self.category_path))

    @property
    def duration_seconds(self) -> int:
        return self.end.epoch_seconds - self.start.epoch_seconds

    @property
    def source_duration_drift(self) -> int | None:
        """How far the source's reported duration is from the derived one."""
        if self.source_duration_seconds is None:
            return None
        return self.source_duration_seconds - self.duration_seconds


@dataclass(frozen=True, slots=True)
class StoredInterval:
    """An interval as read back from storage."""

    id: int
    stream_id: int
    category_id: int
    category_path: str
    recorded_path: str
    start: Instant
    end: Instant
    import_run_id: int

    @property
    def duration_seconds(self) -> int:
        return self.end.epoch_seconds - self.start.epoch_seconds


@dataclass(frozen=True, slots=True)
class ImportRun:
    """Provenance for one import: which file, when, and what it did.

    Every observation carries the id of the run that produced it, which is what
    makes an import reversible. ``source_sha256`` identifies the file by content
    rather than by name, so a re-downloaded export is recognized as the same
    data even under a different filename.
    """

    id: int
    stream_id: int
    importer: str
    source_name: str
    source_sha256: str
    archived_path: str | None
    started_at: int
    finished_at: int | None
    rows_read: int
    rows_inserted: int
    rows_duplicate: int
    rows_rejected: int
    notes: str | None

    @property
    def completed(self) -> bool:
        return self.finished_at is not None


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    """Time attributed to one category over a queried range."""

    category_id: int
    category_path: str
    seconds: int
    record_count: int


@dataclass(frozen=True, slots=True)
class PeriodTotal:
    """Time attributed to one local-time bucket (see :class:`~minmax.core.timeref.Period`)."""

    bucket: str
    seconds: int
    record_count: int
