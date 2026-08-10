"""Minmax's domain model.

Everything here is a plain, frozen value object with no I/O and no knowledge of
SQL. Importers produce these; the repository stores and returns them.

The central abstraction is :class:`StreamKind`. Every datastream Minmax will
ever ingest reduces to one of three shapes, and modelling the *shapes* rather
than the sources is what keeps the eventual correlation engine from needing a
special case per source.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from minmax.core import paths
from minmax.core.payload import payload_hash
from minmax.core.timeref import Instant

__all__ = [
    "Category",
    "CategoryTotal",
    "DailyCategoryTotal",
    "DailyFieldTotal",
    "EventCategoryTotal",
    "EventRecord",
    "FieldRole",
    "FieldSpec",
    "FieldValue",
    "ImportRun",
    "IntervalRecord",
    "PeriodTotal",
    "StoredEvent",
    "StoredInterval",
    "Stream",
    "StreamField",
    "StreamKind",
    "implicit_category_path",
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
    watch_path: str | None = None
    """The filesystem path Minmax re-imports from once a minute, if
    ``watch_enabled``. Remembered per stream so auto-fetch survives a
    restart; see ``docs/architecture.md`` §6.2."""
    watch_enabled: bool = False


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


class FieldRole(StrEnum):
    """What a declared column contributes to a stream's record, per kind.

    ``CATEGORY``, ``SOURCE_ID``, and ``IGNORED`` are structural in either
    kind and feed the same category-resolution machinery both record kinds
    already share; ``IGNORED`` means the column exists in the source but is
    deliberately not stored (e.g. a duration column redundant with the
    timestamps).

    For an **event** (point-in-time) stream: exactly one column must declare
    ``AT``; any number may declare ``NUMBER``/``TEXT``, which become values
    keyed by the field's :attr:`StreamField.key`.

    For an **interval** (span) stream: exactly one column must declare
    ``START`` and exactly one ``END``. ``NUMBER``/``TEXT`` are not valid --
    ``interval_record`` has fixed columns and no per-row payload table (its
    shape is already fixed, unlike an event stream's -- see
    ``docs/architecture.md`` §6.1), so there is nowhere for an interval
    stream's extra column to be stored; it must be ``IGNORED`` instead.

    In both kinds, at most one column may declare each of ``CATEGORY`` and
    ``SOURCE_ID``.
    """

    AT = "at"
    START = "start"
    END = "end"
    CATEGORY = "category"
    SOURCE_ID = "source_id"
    NUMBER = "number"
    TEXT = "text"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A field as declared before its stream exists.

    What a new-stream dialog (or ``minmax stream add``) builds from a CSV's
    header, and what :meth:`~minmax.storage.repository.Repository.create_stream`
    turns into stored :class:`StreamField` rows -- there is no ``stream_id`` or
    ``position`` yet because the stream does not exist yet either; position is
    the order the specs are given in.
    """

    key: str
    label: str
    role: FieldRole
    source_column: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class StreamField:
    """One declared column of a user-declared stream, and where it reads from.

    Declared once -- the new-stream dialog, or ``minmax stream add`` -- and
    reused for every later import into that stream, so a CSV's header can be
    read once rather than mapped by hand each time. See
    ``docs/architecture.md`` §6.1.
    """

    stream_id: int
    key: str
    """Slug identifying this field within its stream; how a value is looked
    up in :attr:`EventRecord.values` and how a measure names it
    (``field:heart_rate:mean``)."""

    label: str
    role: FieldRole
    source_column: str
    """The CSV header this field reads from. Matched by name on every
    import, so reordering the source file's columns does not break it."""

    unit: str | None = None
    position: int = 0
    id: int | None = None


def implicit_category_path(stream_key: str) -> str:
    """Where a point-in-time stream with no declared category column files
    everything.

    A sentinel path like ``"(all)"`` would need excluding from the filter
    dialog, the drill-down stack, and the palette; the stream key needs
    excluding from nothing, since it already identifies the stream everywhere
    else. The category is created lazily, the first time an import sees it --
    no special-case code is needed downstream of that.
    """
    return stream_key


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One point-in-time observation, as produced by an importer before storage.

    Unlike :class:`IntervalRecord`, the payload beyond the instant is not
    fixed -- a stream may declare nothing, one number, several numbers, or
    text (see ``docs/architecture.md`` §6.1). ``values`` holds whatever the
    stream declared, keyed by field slug (:attr:`StreamField.key`).
    """

    category_source_id: str | None
    category_path: str
    at: Instant
    values: Mapping[str, float | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category_source_id is None and not self.category_path:
            raise ValueError("an event needs either a category source id or a path")
        # Normalizing here means a stray space in a source file cannot
        # silently create a near-duplicate category -- same rule as
        # IntervalRecord, for the same reason.
        object.__setattr__(self, "category_path", paths.normalize(self.category_path))

    @property
    def payload_hash(self) -> str:
        """Digest of :attr:`values`, part of the natural key at storage time.

        Computed here rather than only in the repository so a caller
        constructing an :class:`EventRecord` can already tell whether two of
        them collide, without a database round trip.
        """
        return payload_hash(self.values)


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One field's value on a stored record, carrying enough to display it
    without a second lookup against :class:`StreamField`."""

    key: str
    label: str
    unit: str | None
    value: float | str


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """A point-in-time observation as read back from storage."""

    id: int
    stream_id: int
    category_id: int
    category_path: str
    recorded_path: str
    at: Instant
    import_run_id: int
    values: tuple[FieldValue, ...] = ()


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
class EventCategoryTotal:
    """How many events landed in one category over a queried range.

    The event analogue of :class:`CategoryTotal`: an instant either falls in
    the range or it does not, so there is no clipped-duration arithmetic to
    do -- just a count.
    """

    category_id: int
    category_path: str
    record_count: int


@dataclass(frozen=True, slots=True)
class DailyCategoryTotal:
    """Time attributed to one category on one local day.

    The unit the trends view is built from: a stacked area is this table
    pivoted, one band per category and one column per day.
    """

    bucket: str
    category_id: int
    category_path: str
    seconds: int
    record_count: int


@dataclass(frozen=True, slots=True)
class DailyFieldTotal:
    """A numeric declared field's aggregate for one category on one local day.

    The ``field_rollup`` analogue of :class:`DailyCategoryTotal`. ``value_sum``
    and ``value_count`` are carried separately rather than as a precomputed
    mean -- a multi-day mean is ``sum(value_sum) / sum(value_count)``, the
    correct value-weighted mean, and is not the mean of these rows' own means.
    See ``docs/architecture.md`` §6.1 and migration ``0004``'s comment on
    ``field_rollup``.
    """

    bucket: str
    category_id: int
    category_path: str
    field_id: int
    field_key: str
    value_sum: float
    value_count: int
    value_min: float | None
    value_max: float | None


@dataclass(frozen=True, slots=True)
class PeriodTotal:
    """Time attributed to one local-time bucket (see :class:`~minmax.core.timeref.Period`)."""

    bucket: str
    seconds: int
    record_count: int
