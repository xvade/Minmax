"""The read/write API over SQLite.

This module is the *seam* of the whole project. Everything above it -- ingest,
analysis, the API facade, the UI -- reaches storage only through
:class:`Repository`, and nothing above it contains SQL. That indirection is
what allows the analytics engine to change later (DuckDB reads SQLite files
directly, so it can be attached as a read-only reader behind these same
methods) without touching a single caller.

All mutating methods are individually atomic and compose: calling several
inside one :func:`~minmax.storage.database.transaction` makes the group atomic,
which is how an import applies as a single unit or not at all.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from minmax.core import paths
from minmax.core.models import (
    Category,
    CategoryTotal,
    ImportRun,
    IntervalRecord,
    PeriodTotal,
    StoredInterval,
    Stream,
    StreamKind,
)
from minmax.core.timeref import Instant, Period
from minmax.storage import rollups
from minmax.storage.database import transaction

__all__ = ["InsertOutcome", "OverlapError", "Repository"]


class OverlapError(RuntimeError):
    """Raised when an exclusive stream would gain overlapping observations.

    An exclusive stream promises at most one observation is true at a time, so
    an overlap means the incoming data contradicts what is already stored --
    typically a run whose times were edited in Timecard after an earlier export.
    Keeping both would silently corrupt every total that covers that moment, so
    the import is aborted whole and the conflict reported. The operator resolves
    it by reverting the earlier import (``minmax revert``) and re-importing.
    """


@dataclass(frozen=True, slots=True)
class InsertOutcome:
    inserted: int
    duplicates: int


class Repository:
    """Reads and writes Minmax's stored data. Owns every SQL statement."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    def ensure_stream(
        self, key: str, name: str, kind: StreamKind, *, exclusive: bool = False
    ) -> Stream:
        """Fetch the stream with this key, creating it if absent."""
        existing = self.get_stream(key)
        if existing is not None:
            return existing
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO stream (key, name, kind, exclusive, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, name, kind.value, int(exclusive), int(time.time())),
            )
        return Stream(
            key=key, name=name, kind=kind, exclusive=exclusive, id=int(cursor.lastrowid or 0)
        )

    def get_stream(self, key: str) -> Stream | None:
        row = self._conn.execute("SELECT * FROM stream WHERE key = ?", (key,)).fetchone()
        return _stream_from_row(row) if row is not None else None

    def list_streams(self) -> list[Stream]:
        rows = self._conn.execute("SELECT * FROM stream ORDER BY key").fetchall()
        return [_stream_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    def ensure_category(
        self,
        stream_id: int,
        *,
        source_id: str | None,
        path: str,
        first_seen_at: int,
        last_seen_at: int,
    ) -> int:
        """Fetch or create a category, returning its id.

        Identity is the source's own id when it has one, so a renamed or moved
        category keeps its history. ``path`` is therefore updated only when this
        observation is *newer* than any seen so far -- importing an old export
        after a new one must not roll a category's name backwards.
        """
        path = paths.normalize(path)
        if source_id is not None:
            row = self._conn.execute(
                "SELECT id, path, first_seen_at, last_seen_at FROM category"
                " WHERE stream_id = ? AND source_id = ?",
                (stream_id, source_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id, path, first_seen_at, last_seen_at FROM category"
                " WHERE stream_id = ? AND source_id IS NULL AND path = ?",
                (stream_id, path),
            ).fetchone()

        with transaction(self._conn) as conn:
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO category"
                    " (stream_id, source_id, path, top_level, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        stream_id,
                        source_id,
                        path,
                        paths.top_level(path),
                        first_seen_at,
                        last_seen_at,
                    ),
                )
                return int(cursor.lastrowid or 0)

            category_id = int(row["id"])
            newest_path = path if last_seen_at >= int(row["last_seen_at"]) else row["path"]
            conn.execute(
                "UPDATE category SET path = ?, top_level = ?, first_seen_at = ?,"
                " last_seen_at = ? WHERE id = ?",
                (
                    newest_path,
                    paths.top_level(newest_path),
                    min(int(row["first_seen_at"]), first_seen_at),
                    max(int(row["last_seen_at"]), last_seen_at),
                    category_id,
                ),
            )
            return category_id

    def list_categories(self, stream_id: int) -> list[Category]:
        rows = self._conn.execute(
            "SELECT id, stream_id, source_id, path FROM category WHERE stream_id = ? ORDER BY path",
            (stream_id,),
        ).fetchall()
        return [
            Category(
                id=int(row["id"]),
                stream_id=int(row["stream_id"]),
                source_id=row["source_id"],
                path=row["path"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Import provenance
    # ------------------------------------------------------------------

    def find_import_by_hash(self, stream_id: int, source_sha256: str) -> ImportRun | None:
        """The most recent completed import of this exact file content, if any.

        Lets an importer skip re-reading a file it has already ingested. Note
        that skipping is an optimization, not the correctness mechanism -- the
        natural-key constraint is what actually makes imports idempotent.
        """
        row = self._conn.execute(
            "SELECT * FROM import_run WHERE stream_id = ? AND source_sha256 = ?"
            " AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (stream_id, source_sha256),
        ).fetchone()
        return _import_run_from_row(row) if row is not None else None

    def begin_import(
        self,
        stream_id: int,
        *,
        importer: str,
        source_name: str,
        source_sha256: str,
        archived_path: str | None,
    ) -> int:
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO import_run"
                " (stream_id, importer, source_name, source_sha256, archived_path, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stream_id,
                    importer,
                    source_name,
                    source_sha256,
                    archived_path,
                    int(time.time()),
                ),
            )
        return int(cursor.lastrowid or 0)

    def finish_import(
        self,
        import_run_id: int,
        *,
        rows_read: int,
        rows_inserted: int,
        rows_duplicate: int,
        rows_rejected: int,
        notes: str | None = None,
    ) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE import_run SET finished_at = ?, rows_read = ?, rows_inserted = ?,"
                " rows_duplicate = ?, rows_rejected = ?, notes = ? WHERE id = ?",
                (
                    int(time.time()),
                    rows_read,
                    rows_inserted,
                    rows_duplicate,
                    rows_rejected,
                    notes,
                    import_run_id,
                ),
            )

    def fail_import(self, import_run_id: int, error: str) -> None:
        """Record why an import failed, leaving ``finished_at`` NULL.

        Deliberately not "finishing" the run: an unfinished run is what marks
        the attempt as failed, and it keeps :meth:`find_import_by_hash` from
        treating a later retry of the same file as already done.
        """
        with transaction(self._conn) as conn:
            conn.execute(
                "UPDATE import_run SET notes = ? WHERE id = ?",
                (f"failed: {error}", import_run_id),
            )

    def list_imports(self, stream_id: int | None = None) -> list[ImportRun]:
        if stream_id is None:
            rows = self._conn.execute("SELECT * FROM import_run ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM import_run WHERE stream_id = ? ORDER BY id", (stream_id,)
            ).fetchall()
        return [_import_run_from_row(row) for row in rows]

    def revert_import(self, import_run_id: int) -> int:
        """Delete every observation an import produced, returning the row count.

        This is why each observation carries its ``import_run_id``: a bad
        parser or a corrupted export can be undone exactly, without touching
        rows that other imports contributed. Rollups are rebuilt from what
        remains rather than decremented, so a revert cannot leave them drifted.
        """
        with transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT stream_id FROM import_run WHERE id = ?", (import_run_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"no import run with id {import_run_id}")
            stream_id = int(row["stream_id"])

            cursor = conn.execute(
                "DELETE FROM interval_record WHERE import_run_id = ?", (import_run_id,)
            )
            deleted = cursor.rowcount
            conn.execute(
                "UPDATE import_run SET notes = COALESCE(notes || ' | ', '') || 'reverted'"
                " WHERE id = ?",
                (import_run_id,),
            )
            rollups.rebuild(conn, stream_id)
        return deleted

    # ------------------------------------------------------------------
    # Interval observations
    # ------------------------------------------------------------------

    def insert_intervals(
        self, stream: Stream, import_run_id: int, records: Sequence[IntervalRecord]
    ) -> InsertOutcome:
        """Store interval observations, resolving categories and rollups atomically.

        Duplicates are *not* an error: the natural-key index means re-importing
        a file inserts nothing and reports how many rows it recognized. That is
        the mechanism behind the idempotency invariant -- importers do not have
        to detect duplicates themselves, and cannot get it wrong.
        """
        if stream.kind is not StreamKind.INTERVAL:
            raise ValueError(f"stream {stream.key!r} is a {stream.kind} stream, not interval")
        if stream.id is None:
            raise ValueError("stream must be persisted before inserting observations")
        if not records:
            return InsertOutcome(inserted=0, duplicates=0)

        stream_id = stream.id
        with transaction(self._conn) as conn:
            category_ids = self._resolve_categories(stream_id, records)
            resolved = [(category_ids[_category_key(r)], r) for r in records]

            if stream.exclusive:
                self._reject_overlaps(stream_id, resolved)

            inserted: list[tuple[int, Instant, Instant]] = []
            for category_id, record in resolved:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO interval_record"
                    " (stream_id, category_id, import_run_id, started_at, ended_at,"
                    "  start_offset_seconds, end_offset_seconds, duration_seconds,"
                    "  recorded_path)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stream_id,
                        category_id,
                        import_run_id,
                        record.start.epoch_seconds,
                        record.end.epoch_seconds,
                        record.start.utc_offset_seconds,
                        record.end.utc_offset_seconds,
                        record.duration_seconds,
                        record.category_path,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted.append((category_id, record.start, record.end))

            rollups.apply_intervals(conn, stream_id, inserted)

        return InsertOutcome(inserted=len(inserted), duplicates=len(records) - len(inserted))

    def _resolve_categories(
        self, stream_id: int, records: Sequence[IntervalRecord]
    ) -> dict[tuple[str, str], int]:
        """Ensure every category the batch mentions exists, once each.

        Grouped first so that a category appearing in 200 records costs one
        upsert rather than 200, and so its first/last-seen window is computed
        from the whole batch instead of whichever row happened to come last.
        """
        grouped: dict[tuple[str, str], tuple[str, int, int]] = {}
        for record in records:
            key = _category_key(record)
            start = record.start.epoch_seconds
            current = grouped.get(key)
            if current is None:
                grouped[key] = (record.category_path, start, start)
                continue
            newest_path, first_seen, last_seen = current
            grouped[key] = (
                record.category_path if start >= last_seen else newest_path,
                min(first_seen, start),
                max(last_seen, start),
            )

        return {
            key: self.ensure_category(
                stream_id,
                source_id=key[1] if key[0] == "source" else None,
                path=path,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
            )
            for key, (path, first_seen, last_seen) in grouped.items()
        }

    def _reject_overlaps(
        self, stream_id: int, resolved: Sequence[tuple[int, IntervalRecord]]
    ) -> None:
        """Abort the import if an exclusive stream would gain overlapping rows.

        Checked in two directions: within the incoming batch, and against what
        is already stored. Exact natural-key matches are excluded from both,
        since a re-import legitimately presents rows identical to stored ones.
        """
        ordered = sorted(
            resolved, key=lambda pair: (pair[1].start.epoch_seconds, pair[1].end.epoch_seconds)
        )
        for (left_id, left), (right_id, right) in pairwise(ordered):
            identical = (
                left_id == right_id
                and left.start.epoch_seconds == right.start.epoch_seconds
                and left.end.epoch_seconds == right.end.epoch_seconds
            )
            if not identical and right.start.epoch_seconds < left.end.epoch_seconds:
                raise OverlapError(
                    f"incoming rows overlap in exclusive stream: "
                    f"{left.category_path} ending {left.end.local_datetime.isoformat()} "
                    f"overlaps {right.category_path} starting "
                    f"{right.start.local_datetime.isoformat()}"
                )

        for category_id, record in resolved:
            clash = self._conn.execute(
                "SELECT recorded_path, started_at, ended_at FROM interval_record"
                " WHERE stream_id = ? AND started_at < ? AND ended_at > ?"
                "   AND NOT (category_id = ? AND started_at = ? AND ended_at = ?)"
                " LIMIT 1",
                (
                    stream_id,
                    record.end.epoch_seconds,
                    record.start.epoch_seconds,
                    category_id,
                    record.start.epoch_seconds,
                    record.end.epoch_seconds,
                ),
            ).fetchone()
            if clash is not None:
                raise OverlapError(
                    f"incoming {record.category_path} "
                    f"({record.start.local_datetime.isoformat()} -> "
                    f"{record.end.local_datetime.isoformat()}) overlaps stored "
                    f"{clash['recorded_path']}; revert the earlier import to replace it"
                )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def intervals_in_range(
        self,
        stream_id: int,
        start_epoch: int,
        end_epoch: int,
        *,
        path_prefix: str | None = None,
    ) -> list[StoredInterval]:
        """Observations overlapping the half-open range ``[start, end)``.

        Overlapping, not contained: a sleep run that begins the previous evening
        must appear when today is displayed, which is exactly how Timecard draws
        a midnight-crossing run as one block.
        """
        clause, params = _prefix_clause(path_prefix)
        rows = self._conn.execute(
            "SELECT i.id, i.stream_id, i.category_id, i.import_run_id, i.recorded_path,"
            "       i.started_at, i.ended_at, i.start_offset_seconds, i.end_offset_seconds,"
            "       c.path AS category_path"
            " FROM interval_record i JOIN category c ON c.id = i.category_id"
            " WHERE i.stream_id = ? AND i.started_at < ? AND i.ended_at > ?"
            + clause
            + " ORDER BY i.started_at, i.ended_at",
            (stream_id, end_epoch, start_epoch, *params),
        ).fetchall()
        return [_interval_from_row(row) for row in rows]

    def totals_by_category(
        self,
        stream_id: int,
        start_epoch: int,
        end_epoch: int,
        *,
        path_prefix: str | None = None,
    ) -> list[CategoryTotal]:
        """Seconds per category over a range, clipped exactly to its bounds.

        Computed from observations rather than rollups so that an arbitrary
        range -- not just whole hours or days -- is exact. Rollups serve the
        bucket-aligned queries in :meth:`totals_by_period`.
        """
        clause, params = _prefix_clause(path_prefix)
        rows = self._conn.execute(
            "SELECT c.id AS category_id, c.path AS category_path,"
            "       SUM(MIN(i.ended_at, ?) - MAX(i.started_at, ?)) AS seconds,"
            "       COUNT(*) AS record_count"
            " FROM interval_record i JOIN category c ON c.id = i.category_id"
            " WHERE i.stream_id = ? AND i.started_at < ? AND i.ended_at > ?"
            + clause
            + " GROUP BY c.id, c.path ORDER BY seconds DESC, c.path",
            (end_epoch, start_epoch, stream_id, end_epoch, start_epoch, *params),
        ).fetchall()
        return [
            CategoryTotal(
                category_id=int(row["category_id"]),
                category_path=row["category_path"],
                seconds=int(row["seconds"]),
                record_count=int(row["record_count"]),
            )
            for row in rows
        ]

    def totals_by_period(
        self,
        stream_id: int,
        period: Period,
        *,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[PeriodTotal]:
        """Seconds per local bucket, read from the rollup table.

        Bucket keys are inclusive on both ends and compare as strings, which
        works because the key format sorts chronologically.
        """
        clause, params = _prefix_clause(path_prefix)
        bounds = ""
        bound_params: list[str] = []
        if start_bucket is not None:
            bounds += " AND r.bucket >= ?"
            bound_params.append(start_bucket)
        if end_bucket is not None:
            bounds += " AND r.bucket <= ?"
            bound_params.append(end_bucket)

        rows = self._conn.execute(
            "SELECT r.bucket, SUM(r.seconds) AS seconds, SUM(r.record_count) AS record_count"
            " FROM rollup r JOIN category c ON c.id = r.category_id"
            " WHERE r.stream_id = ? AND r.period = ?"
            + bounds
            + clause
            + " GROUP BY r.bucket ORDER BY r.bucket",
            (stream_id, period.value, *bound_params, *params),
        ).fetchall()
        return [
            PeriodTotal(
                bucket=row["bucket"],
                seconds=int(row["seconds"]),
                record_count=int(row["record_count"]),
            )
            for row in rows
        ]

    def observed_range(self, stream_id: int) -> tuple[int, int] | None:
        """Epoch bounds of everything stored for a stream, or None if empty."""
        row = self._conn.execute(
            "SELECT MIN(started_at) AS lo, MAX(ended_at) AS hi FROM interval_record"
            " WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])


# ----------------------------------------------------------------------
# Row mapping
# ----------------------------------------------------------------------


def _category_key(record: IntervalRecord) -> tuple[str, str]:
    if record.category_source_id is not None:
        return ("source", record.category_source_id)
    return ("path", record.category_path)


def _prefix_clause(path_prefix: str | None) -> tuple[str, tuple[str, ...]]:
    """Restrict to a category and its descendants.

    Matching is ``= prefix OR LIKE prefix || '/%'`` rather than a bare
    ``LIKE prefix || '%'``, so ``media`` does not also match ``mediation``.
    """
    if path_prefix is None:
        return "", ()
    normalized = paths.normalize(path_prefix)
    return " AND (c.path = ? OR c.path LIKE ? ESCAPE '\\')", (
        normalized,
        _escape_like(normalized) + "/%",
    )


def _escape_like(value: str) -> str:
    """Neutralize LIKE wildcards in a category path used as a literal prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _stream_from_row(row: sqlite3.Row) -> Stream:
    return Stream(
        id=int(row["id"]),
        key=row["key"],
        name=row["name"],
        kind=StreamKind(row["kind"]),
        exclusive=bool(row["exclusive"]),
    )


def _interval_from_row(row: sqlite3.Row) -> StoredInterval:
    return StoredInterval(
        id=int(row["id"]),
        stream_id=int(row["stream_id"]),
        category_id=int(row["category_id"]),
        category_path=row["category_path"],
        recorded_path=row["recorded_path"],
        start=Instant(int(row["started_at"]), int(row["start_offset_seconds"])),
        end=Instant(int(row["ended_at"]), int(row["end_offset_seconds"])),
        import_run_id=int(row["import_run_id"]),
    )


def _import_run_from_row(row: sqlite3.Row) -> ImportRun:
    return ImportRun(
        id=int(row["id"]),
        stream_id=int(row["stream_id"]),
        importer=row["importer"],
        source_name=row["source_name"],
        source_sha256=row["source_sha256"],
        archived_path=row["archived_path"],
        started_at=int(row["started_at"]),
        finished_at=None if row["finished_at"] is None else int(row["finished_at"]),
        rows_read=int(row["rows_read"]),
        rows_inserted=int(row["rows_inserted"]),
        rows_duplicate=int(row["rows_duplicate"]),
        rows_rejected=int(row["rows_rejected"]),
        notes=row["notes"],
    )
