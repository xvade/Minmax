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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from minmax.core import palette, paths
from minmax.core.models import (
    ArchivedImport,
    Category,
    CategoryTotal,
    DailyCategoryTotal,
    DailyFieldTotal,
    EventCategoryTotal,
    EventRecord,
    FieldRole,
    FieldSpec,
    FieldValue,
    ImportRun,
    IntervalRecord,
    PeriodTotal,
    StoredEvent,
    StoredInterval,
    Stream,
    StreamField,
    StreamKind,
    View,
)
from minmax.core.timeref import Instant, Period
from minmax.core.windows import TimeMask
from minmax.storage import _sql, masked, rollups
from minmax.storage.database import connect, transaction

__all__ = ["InsertOutcome", "OverlapError", "Repository"]

Record = IntervalRecord | EventRecord


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
        # Read here, on the thread that owns the connection: a worker thread
        # calling open_sibling() may not touch ``conn`` to look it up.
        self._db_file: str = conn.execute("PRAGMA database_list").fetchone()[2]
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def open_sibling(self) -> Repository | None:
        """A second repository on the same database file, with its own connection.

        A SQLite connection cannot be used from two threads, and the WAL journal
        lets one connection read while another writes. Safe to call from any
        thread, unlike everything else here. Work meant for a
        background thread -- an import -- therefore opens a sibling and closes it
        when done, rather than sharing this one. ``None`` for a database with no
        file behind it (in memory), which cannot be reopened; the caller then has
        to stay on the thread that owns this connection.
        """
        if not self._db_file:
            return None
        return Repository(connect(self._db_file))

    def close(self) -> None:
        """Close this repository's connection."""
        self._conn.close()

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

    def create_stream(
        self,
        key: str,
        name: str,
        kind: StreamKind,
        *,
        exclusive: bool = False,
        fields: Sequence[FieldSpec] = (),
    ) -> Stream:
        """Create a new stream and declare its fields, atomically.

        Unlike :meth:`ensure_stream`, this is for a user explicitly adding a
        stream: it fails if the key is already taken rather than silently
        returning the existing one, because a user picking a name that turns
        out to be in use almost certainly meant to create a new stream, not
        fetch an old one.
        """
        if self.get_stream(key) is not None:
            raise ValueError(f"a stream named {key!r} already exists")
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO stream (key, name, kind, exclusive, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, name, kind.value, int(exclusive), int(time.time())),
            )
            stream_id = int(cursor.lastrowid or 0)
            for position, spec in enumerate(fields):
                conn.execute(
                    "INSERT INTO stream_field"
                    " (stream_id, key, label, role, unit, position, source_column)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        stream_id,
                        spec.key,
                        spec.label,
                        spec.role.value,
                        spec.unit,
                        position,
                        spec.source_column,
                    ),
                )
        return Stream(key=key, name=name, kind=kind, exclusive=exclusive, id=stream_id)

    def delete_stream(self, key: str) -> Stream:
        """Permanently delete a stream and everything derived from it.

        Every table below ``stream`` -- categories, records, values, rollups,
        ``stream_field``, import runs -- declares ``REFERENCES stream (id) ON
        DELETE CASCADE``, and foreign keys are on for every connection (see
        ``storage/database.py``). So one ``DELETE`` is the entire operation:
        there is no cascade to hand-roll and, unlike :meth:`revert_import`, no
        rollup to rebuild afterward, since nothing of the stream is left for a
        rollup to be wrong about.

        Archived raw files under ``home/archive/<key>/`` are deliberately left
        on disk: :func:`~minmax.ingest.archive.archive_file`'s own contract is
        "never modified or overwritten, only added," and keeping them lets the
        same source be re-imported if the stream is ever recreated.
        """
        with transaction(self._conn) as conn:
            row = conn.execute("SELECT * FROM stream WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise LookupError(f"no stream {key!r}")
            stream = _stream_from_row(row)
            conn.execute("DELETE FROM stream WHERE id = ?", (stream.id,))
        return stream

    def set_stream_watch(self, stream_id: int, *, path: str | None, enabled: bool) -> None:
        """Turn a stream's per-minute auto-fetch on or off.

        ``path=None`` leaves the previously remembered path untouched -- the
        only way to change it today is to give a new one, since nothing needs
        to clear it back to unset once a stream has ever been pointed
        somewhere.
        """
        with transaction(self._conn) as conn:
            if path is None:
                conn.execute(
                    "UPDATE stream SET watch_enabled = ? WHERE id = ?", (int(enabled), stream_id)
                )
            else:
                conn.execute(
                    "UPDATE stream SET watch_path = ?, watch_enabled = ? WHERE id = ?",
                    (path, int(enabled), stream_id),
                )

    def stream_fields(self, stream_id: int) -> list[StreamField]:
        """A stream's declared column mapping, in display/import order."""
        rows = self._conn.execute(
            "SELECT id, stream_id, key, label, role, unit, position, source_column"
            " FROM stream_field WHERE stream_id = ? ORDER BY position",
            (stream_id,),
        ).fetchall()
        return [
            StreamField(
                id=int(row["id"]),
                stream_id=int(row["stream_id"]),
                key=row["key"],
                label=row["label"],
                role=FieldRole(row["role"]),
                unit=row["unit"],
                position=int(row["position"]),
                source_column=row["source_column"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def create_view(
        self,
        key: str,
        name: str,
        source_stream_key: str,
        *,
        category_path: str | None = None,
    ) -> View:
        """Save a new view, restricted to an interval stream's category.

        Only an interval stream records when a category was active, so that
        is the only source kind a view can be built on today -- a later view
        kind (an event stream's own timestamps, say) would need its own
        ``source_kind`` discriminator, deliberately not added ahead of time.
        """
        if self.get_view(key) is not None:
            raise ValueError(f"a view named {key!r} already exists")
        stream = self.get_stream(source_stream_key)
        if stream is None:
            raise LookupError(f"no stream {source_stream_key!r}")
        if stream.kind is not StreamKind.INTERVAL:
            raise ValueError(
                f"view {key!r} needs an interval stream, but"
                f" {source_stream_key!r} is a {stream.kind} stream"
            )
        normalized = paths.normalize(category_path) if category_path is not None else None
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO view (key, name, source_stream_id, category_path, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, name, stream.id, normalized, int(time.time())),
            )
        return View(
            key=key,
            name=name,
            source_stream_key=source_stream_key,
            category_path=normalized,
            id=int(cursor.lastrowid or 0),
        )

    def get_view(self, key: str) -> View | None:
        row = self._conn.execute(
            "SELECT v.id, v.key, v.name, v.category_path, s.key AS source_stream_key"
            " FROM view v JOIN stream s ON s.id = v.source_stream_id"
            " WHERE v.key = ?",
            (key,),
        ).fetchone()
        return _view_from_row(row) if row is not None else None

    def list_views(self) -> list[View]:
        rows = self._conn.execute(
            "SELECT v.id, v.key, v.name, v.category_path, s.key AS source_stream_key"
            " FROM view v JOIN stream s ON s.id = v.source_stream_id"
            " ORDER BY v.key"
        ).fetchall()
        return [_view_from_row(row) for row in rows]

    def delete_view(self, key: str) -> View:
        with transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT v.id, v.key, v.name, v.category_path, s.key AS source_stream_key"
                " FROM view v JOIN stream s ON s.id = v.source_stream_id"
                " WHERE v.key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise LookupError(f"no view {key!r}")
            view = _view_from_row(row)
            conn.execute("DELETE FROM view WHERE id = ?", (view.id,))
        return view

    def active_windows(
        self,
        stream_id: int,
        start_epoch: int,
        end_epoch: int,
        *,
        path_prefix: str | None = None,
    ) -> TimeMask:
        """When a category (or, with no prefix, anything) in this stream was
        active within ``[start_epoch, end_epoch)``.

        Clipped exactly to the bounds in SQL -- the same clipping arithmetic
        :meth:`totals_by_category` uses -- then merged into a
        :class:`~minmax.core.windows.TimeMask`, the primitive a view's masked
        reads are built from.
        """
        clause, params = _sql.prefix_clause(path_prefix)
        rows = self._conn.execute(
            "SELECT max(i.started_at, ?) AS clipped_start, min(i.ended_at, ?) AS clipped_end"
            " FROM interval_record i JOIN category c ON c.id = i.category_id"
            " WHERE i.stream_id = ? AND i.started_at < ? AND i.ended_at > ?" + clause,
            (start_epoch, end_epoch, stream_id, end_epoch, start_epoch, *params),
        ).fetchall()
        return TimeMask.from_spans(
            (int(row["clipped_start"]), int(row["clipped_end"])) for row in rows
        )

    def mask_for_view(self, view: View, start_epoch: int, end_epoch: int) -> TimeMask:
        """The time mask a saved view implies over ``[start_epoch, end_epoch)``."""
        stream = self.get_stream(view.source_stream_key)
        if stream is None or stream.id is None:
            raise LookupError(
                f"view {view.key!r} references missing stream {view.source_stream_key!r}"
            )
        return self.active_windows(
            stream.id, start_epoch, end_epoch, path_prefix=view.category_path
        )

    def masked_daily_totals_by_category(
        self,
        stream_id: int,
        mask: TimeMask,
        *,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[DailyCategoryTotal]:
        """Seconds per (local day, category), restricted to ``mask``.

        The masked analogue of :meth:`daily_totals_by_category`: reads raw
        observations rather than the rollup table, since the rollup was
        never bucketed with an arbitrary mask in mind. See
        ``storage/masked.py``.
        """
        return masked.daily_totals_by_category(
            self._conn,
            stream_id,
            mask,
            start_bucket=start_bucket,
            end_bucket=end_bucket,
            path_prefix=path_prefix,
        )

    def masked_daily_event_counts_by_category(
        self,
        stream_id: int,
        mask: TimeMask,
        *,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[DailyCategoryTotal]:
        """An event stream's masked count per (local day, category).

        The masked analogue of :meth:`daily_totals_by_category`'s
        record-count branch -- an interval stream's masked *seconds* come
        from :meth:`masked_daily_totals_by_category` instead, since an
        interval needs clipping rather than a membership test.
        """
        return masked.daily_event_counts_by_category(
            self._conn,
            stream_id,
            mask,
            start_bucket=start_bucket,
            end_bucket=end_bucket,
            path_prefix=path_prefix,
        )

    def masked_daily_field_totals_by_category(
        self,
        stream_id: int,
        mask: TimeMask,
        *,
        field_id: int | None = None,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[DailyFieldTotal]:
        """The masked analogue of :meth:`daily_field_totals_by_category`."""
        return masked.daily_field_totals_by_category(
            self._conn,
            stream_id,
            mask,
            field_id=field_id,
            start_bucket=start_bucket,
            end_bucket=end_bucket,
            path_prefix=path_prefix,
        )

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
    # Group colours
    # ------------------------------------------------------------------

    def group_slots(self, stream_id: int) -> dict[str, int]:
        """Stored palette slot for each top-level category group in this stream."""
        rows = self._conn.execute(
            "SELECT top_level, slot FROM group_color WHERE stream_id = ?", (stream_id,)
        ).fetchall()
        return {row["top_level"]: int(row["slot"]) for row in rows}

    def ensure_group_slots(self, stream_id: int, top_levels: Sequence[str]) -> dict[str, int]:
        """Assign a palette slot to any group that lacks one, and return them all.

        New groups are assigned in sorted order so that the result does not
        depend on the order rows happened to arrive in an import, and each takes
        the least-used slot so colours stay distinct. Existing assignments are
        never revised -- a group's colour is something the user learns to
        recognize, and it must not shift because another group appeared later.
        """
        assigned = self.group_slots(stream_id)
        missing = sorted(set(top_levels) - assigned.keys())
        if not missing:
            return assigned

        now = int(time.time())
        with transaction(self._conn) as conn:
            for top_level in missing:
                slot = palette.next_slot(assigned.values())
                conn.execute(
                    "INSERT INTO group_color (stream_id, top_level, slot, assigned_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (stream_id, top_level) DO NOTHING",
                    (stream_id, top_level, slot, now),
                )
                assigned[top_level] = slot
        return assigned

    # ------------------------------------------------------------------
    # Manual series colours
    # ------------------------------------------------------------------

    def series_colours(self, stream_id: int) -> dict[str, palette.ColourChoice]:
        """Every colour the user picked by hand in this stream, by category path.

        A series with no entry is on Auto.
        """
        rows = self._conn.execute(
            "SELECT category_path, slot, hex FROM series_colour WHERE stream_id = ?",
            (stream_id,),
        ).fetchall()
        return {
            row["category_path"]: palette.ColourChoice(slot=row["slot"], hex=row["hex"])
            for row in rows
        }

    def set_series_colour(
        self, stream_id: int, category_path: str, choice: palette.ColourChoice | None
    ) -> None:
        """Pick a colour for a series, or pass ``None`` to put it back on Auto."""
        with transaction(self._conn) as conn:
            if choice is None:
                conn.execute(
                    "DELETE FROM series_colour WHERE stream_id = ? AND category_path = ?",
                    (stream_id, category_path),
                )
                return
            conn.execute(
                "INSERT INTO series_colour (stream_id, category_path, slot, hex, chosen_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (stream_id, category_path) DO UPDATE SET"
                " slot = excluded.slot, hex = excluded.hex, chosen_at = excluded.chosen_at",
                (stream_id, category_path, choice.slot, choice.hex, int(time.time())),
            )

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

    def last_archived_at(self, stream_id: int) -> int | None:
        """When this stream's raw file was last actually copied into the archive.

        Epoch seconds of the newest import run that still has an archived copy,
        or ``None`` if there is none. Runs whose copy was skipped (throttled, or
        pruned) have ``archived_path IS NULL`` and do not count, so the answer
        does not slide forward just because imports keep happening.
        """
        row = self._conn.execute(
            "SELECT started_at FROM import_run WHERE stream_id = ? AND archived_path IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (stream_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def archived_imports(self) -> list[ArchivedImport]:
        """Every import run that still points at an archived file, oldest first."""
        rows = self._conn.execute(
            "SELECT import_run.id, stream.key, import_run.source_name,"
            " import_run.started_at, import_run.archived_path"
            " FROM import_run JOIN stream ON stream.id = import_run.stream_id"
            " WHERE import_run.archived_path IS NOT NULL ORDER BY import_run.id"
        ).fetchall()
        return [
            ArchivedImport(
                import_run_id=row[0],
                stream_key=row[1],
                source_name=row[2],
                started_at=row[3],
                path=row[4],
            )
            for row in rows
        ]

    def clear_archived_paths(self, paths: Iterable[str]) -> int:
        """Forget archived files that no longer exist; returns the runs updated.

        One statement over a temporary table rather than one ``UPDATE`` per
        path: ``import_run`` has no index on ``archived_path``, so tens of
        thousands of separate updates would each scan every run.
        """
        with transaction(self._conn) as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS pruned_path (path TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM pruned_path")
            conn.executemany(
                "INSERT OR IGNORE INTO pruned_path (path) VALUES (?)", ((p,) for p in paths)
            )
            cursor = conn.execute(
                "UPDATE import_run SET archived_path = NULL"
                " WHERE archived_path IN (SELECT path FROM pruned_path)"
            )
            conn.execute("DROP TABLE pruned_path")
        return cursor.rowcount

    def import_source_names(self, import_run_ids: Iterable[int]) -> dict[int, str]:
        """File name of each given import run, without loading every run.

        ``list_imports`` builds a full :class:`ImportRun` per run ever made, which
        on a stream that auto-fetches every minute is tens of thousands. A view of
        a few days only ever references a handful.
        """
        wanted = sorted(set(import_run_ids))
        names: dict[int, str] = {}
        # SQLite's variable limit is 32766 on current builds; chunk well below it.
        for offset in range(0, len(wanted), 500):
            chunk = wanted[offset : offset + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT id, source_name FROM import_run WHERE id IN ({marks})",
                chunk,
            )
            names.update({row["id"]: row["source_name"] for row in rows})
        return names

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

            # An import run belongs to exactly one stream, so only the table
            # matching that stream's kind ever has matching rows -- deleting
            # from both is simpler than looking the kind up first, and costs
            # nothing extra since the other DELETE always affects zero rows.
            deleted = conn.execute(
                "DELETE FROM interval_record WHERE import_run_id = ?", (import_run_id,)
            ).rowcount
            deleted += conn.execute(
                "DELETE FROM event_record WHERE import_run_id = ?", (import_run_id,)
            ).rowcount
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
        self, stream_id: int, records: Sequence[Record]
    ) -> dict[tuple[str, str], int]:
        """Ensure every category the batch mentions exists, once each.

        Grouped first so that a category appearing in 200 records costs one
        upsert rather than 200, and so its first/last-seen window is computed
        from the whole batch instead of whichever row happened to come last.
        Works for either record kind: only ``category_source_id``,
        ``category_path``, and an instant to seed the seen-window are used,
        and every kind of record carries those.
        """
        grouped: dict[tuple[str, str], tuple[str, int, int]] = {}
        for record in records:
            key = _category_key(record)
            start = _record_instant(record).epoch_seconds
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

        # Against what is stored. A row whose natural key is already stored is
        # skipped: it was checked when it was first inserted, and a re-import of a
        # growing file presents tens of thousands of those for a handful of new
        # rows. Without the skip this loop ran one query per stored row and was
        # quadratic -- 19 s per auto-fetch tick on a 33k-row stream.
        starts = [record.start.epoch_seconds for _, record in resolved]
        already_stored = {
            (row["category_id"], row["started_at"], row["ended_at"])
            for row in self._conn.execute(
                "SELECT category_id, started_at, ended_at FROM interval_record"
                " WHERE stream_id = ? AND started_at BETWEEN ? AND ?",
                (stream_id, min(starts), max(starts)),
            )
        }
        fresh = [
            record
            for category_id, record in resolved
            if (category_id, record.start.epoch_seconds, record.end.epoch_seconds)
            not in already_stored
        ]
        if not fresh:
            return

        # A stored row overlapping [start, end) must satisfy ended_at > start, and
        # ended_at = started_at + duration <= started_at + longest, so its start
        # lies after ``start - longest``. That bound lets the query use the
        # (stream_id, started_at) index instead of scanning every earlier row.
        longest = self._conn.execute(
            "SELECT COALESCE(MAX(duration_seconds), 0) FROM interval_record WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()[0]
        for record in fresh:
            clash = self._conn.execute(
                "SELECT recorded_path, started_at, ended_at FROM interval_record"
                " WHERE stream_id = ? AND started_at > ? AND started_at < ? AND ended_at > ?"
                " LIMIT 1",
                (
                    stream_id,
                    record.start.epoch_seconds - longest,
                    record.end.epoch_seconds,
                    record.start.epoch_seconds,
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
    # Point-in-time observations
    # ------------------------------------------------------------------

    def insert_events(
        self, stream: Stream, import_run_id: int, records: Sequence[EventRecord]
    ) -> InsertOutcome:
        """Store point-in-time observations, resolving categories and rollups
        atomically. The event analogue of :meth:`insert_intervals`.

        The natural key additionally includes each record's payload hash, so
        two distinct readings at the same instant both survive while a
        re-import of the same file is still a no-op.
        """
        if stream.kind is not StreamKind.EVENT:
            raise ValueError(f"stream {stream.key!r} is a {stream.kind} stream, not event")
        if stream.id is None:
            raise ValueError("stream must be persisted before inserting observations")
        if not records:
            return InsertOutcome(inserted=0, duplicates=0)

        stream_id = stream.id
        field_ids = {
            declared.key: declared.id
            for declared in self.stream_fields(stream_id)
            if declared.role in (FieldRole.NUMBER, FieldRole.TEXT)
        }

        with transaction(self._conn) as conn:
            category_ids = self._resolve_categories(stream_id, records)
            resolved = [(category_ids[_category_key(r)], r) for r in records]

            inserted_events: list[tuple[int, Instant]] = []
            inserted_values: list[tuple[int, int, Instant, float]] = []
            for category_id, record in resolved:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO event_record"
                    " (stream_id, category_id, import_run_id, occurred_at, offset_seconds,"
                    "  recorded_path, payload_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        stream_id,
                        category_id,
                        import_run_id,
                        record.at.epoch_seconds,
                        record.at.utc_offset_seconds,
                        record.category_path,
                        record.payload_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    continue

                record_id = cursor.lastrowid
                for field_key, value in record.values.items():
                    field_id = field_ids.get(field_key)
                    if field_id is None:
                        raise ValueError(
                            f"event carries undeclared field {field_key!r} for stream"
                            f" {stream.key!r}"
                        )
                    if isinstance(value, str):
                        conn.execute(
                            "INSERT INTO event_value (record_id, field_id, num, txt)"
                            " VALUES (?, ?, NULL, ?)",
                            (record_id, field_id, value),
                        )
                    else:
                        numeric = float(value)
                        conn.execute(
                            "INSERT INTO event_value (record_id, field_id, num, txt)"
                            " VALUES (?, ?, ?, NULL)",
                            (record_id, field_id, numeric),
                        )
                        inserted_values.append((category_id, field_id, record.at, numeric))
                inserted_events.append((category_id, record.at))

            rollups.apply_events(conn, stream_id, inserted_events)
            rollups.apply_event_values(conn, stream_id, inserted_values)

        return InsertOutcome(
            inserted=len(inserted_events), duplicates=len(records) - len(inserted_events)
        )

    def insert_records(
        self, stream: Stream, import_run_id: int, records: Sequence[Record]
    ) -> InsertOutcome:
        """Store observations, routing to the table that matches the stream's kind.

        The *stream's* kind decides the table, not whatever happens to be in
        ``records`` -- feeding an event through the interval path (or an
        interval through the event path) raises rather than silently storing
        nothing, which is what an interval importer handed to the wrong
        stream would otherwise do (a zero-length span produces no rollup
        buckets and no error).
        """
        if stream.kind is StreamKind.INTERVAL:
            intervals: list[IntervalRecord] = []
            for record in records:
                if not isinstance(record, IntervalRecord):
                    raise ValueError(
                        f"stream {stream.key!r} is interval; got a {type(record).__name__}"
                    )
                intervals.append(record)
            return self.insert_intervals(stream, import_run_id, intervals)
        if stream.kind is StreamKind.EVENT:
            events: list[EventRecord] = []
            for record in records:
                if not isinstance(record, EventRecord):
                    raise ValueError(
                        f"stream {stream.key!r} is event; got a {type(record).__name__}"
                    )
                events.append(record)
            return self.insert_events(stream, import_run_id, events)
        raise ValueError(f"no insert path for stream kind {stream.kind}")

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
        clause, params = _sql.prefix_clause(path_prefix)
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
        clause, params = _sql.prefix_clause(path_prefix)
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
        clause, params = _sql.prefix_clause(path_prefix)
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

    def daily_totals_by_category(
        self,
        stream_id: int,
        *,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[DailyCategoryTotal]:
        """Seconds per (local day, category), read from the rollup table.

        This is the query the trends view is built on, and the reason rollups
        exist: a stacked area over a year of data is a scan of a few thousand
        aggregate rows rather than of every observation in the range.

        Buckets are inclusive at both ends and compare as strings, which works
        because the key format sorts chronologically.
        """
        clause, params = _sql.prefix_clause(path_prefix)
        bounds = ""
        bound_params: list[str] = []
        if start_bucket is not None:
            bounds += " AND r.bucket >= ?"
            bound_params.append(start_bucket)
        if end_bucket is not None:
            bounds += " AND r.bucket <= ?"
            bound_params.append(end_bucket)

        rows = self._conn.execute(
            "SELECT r.bucket, c.id AS category_id, c.path AS category_path,"
            "       r.seconds, r.record_count"
            " FROM rollup r JOIN category c ON c.id = r.category_id"
            " WHERE r.stream_id = ? AND r.period = 'day'"
            + bounds
            + clause
            + " ORDER BY r.bucket, c.path",
            (stream_id, *bound_params, *params),
        ).fetchall()

        return [
            DailyCategoryTotal(
                bucket=row["bucket"],
                category_id=int(row["category_id"]),
                category_path=row["category_path"],
                seconds=int(row["seconds"]),
                record_count=int(row["record_count"]),
            )
            for row in rows
        ]

    def daily_field_totals_by_category(
        self,
        stream_id: int,
        *,
        field_id: int | None = None,
        start_bucket: str | None = None,
        end_bucket: str | None = None,
        path_prefix: str | None = None,
    ) -> list[DailyFieldTotal]:
        """A numeric field's sum/count/min/max per (local day, category).

        The ``field_rollup`` analogue of :meth:`daily_totals_by_category` --
        same bucket-bound and prefix-matching rules, same rationale (a scan of
        precomputed aggregates rather than of every event). Never derives a
        mean here: a caller wanting a multi-day mean must sum ``value_sum`` and
        ``value_count`` themselves and divide once, the way
        :class:`~minmax.core.models.DailyFieldTotal` documents, since deriving
        one per row and averaging *those* would silently be the wrong number
        the moment two days have different counts.
        """
        clause, params = _sql.prefix_clause(path_prefix)
        bounds = ""
        bound_params: list[str | int] = []
        if field_id is not None:
            bounds += " AND fr.field_id = ?"
            bound_params.append(field_id)
        if start_bucket is not None:
            bounds += " AND fr.bucket >= ?"
            bound_params.append(start_bucket)
        if end_bucket is not None:
            bounds += " AND fr.bucket <= ?"
            bound_params.append(end_bucket)

        rows = self._conn.execute(
            "SELECT fr.bucket, c.id AS category_id, c.path AS category_path,"
            "       f.id AS field_id, f.key AS field_key,"
            "       fr.value_sum, fr.value_count, fr.value_min, fr.value_max"
            " FROM field_rollup fr"
            " JOIN category c ON c.id = fr.category_id"
            " JOIN stream_field f ON f.id = fr.field_id"
            " WHERE fr.stream_id = ? AND fr.period = 'day'"
            + bounds
            + clause
            + " ORDER BY fr.bucket, c.path, f.key",
            (stream_id, *bound_params, *params),
        ).fetchall()

        return [
            DailyFieldTotal(
                bucket=row["bucket"],
                category_id=int(row["category_id"]),
                category_path=row["category_path"],
                field_id=int(row["field_id"]),
                field_key=row["field_key"],
                value_sum=float(row["value_sum"]),
                value_count=int(row["value_count"]),
                value_min=None if row["value_min"] is None else float(row["value_min"]),
                value_max=None if row["value_max"] is None else float(row["value_max"]),
            )
            for row in rows
        ]

    def observed_range(self, stream_id: int) -> tuple[int, int] | None:
        """Epoch bounds of everything stored for a stream, or None if empty.

        Unions both observation tables rather than assuming a kind, so this
        keeps working (and so does everything built on it -- window-loading
        policy included) for a point-in-time stream, which has nothing in
        ``interval_record`` at all.
        """
        row = self._conn.execute(
            "SELECT MIN(lo) AS lo, MAX(hi) AS hi FROM ("
            "  SELECT MIN(started_at) AS lo, MAX(ended_at) AS hi FROM interval_record"
            "  WHERE stream_id = ?"
            "  UNION ALL"
            "  SELECT MIN(occurred_at), MAX(occurred_at) FROM event_record WHERE stream_id = ?"
            ")",
            (stream_id, stream_id),
        ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])

    def events_in_range(
        self,
        stream_id: int,
        start_epoch: int,
        end_epoch: int,
        *,
        path_prefix: str | None = None,
    ) -> list[StoredEvent]:
        """Observations landing in the half-open range ``[start, end)``.

        Contained, not overlapping, unlike :meth:`intervals_in_range`: a
        point-in-time observation has no span to spill across the boundary.
        """
        clause, params = _sql.prefix_clause(path_prefix)
        rows = self._conn.execute(
            "SELECT e.id, e.stream_id, e.category_id, e.import_run_id, e.recorded_path,"
            "       e.occurred_at, e.offset_seconds, c.path AS category_path"
            " FROM event_record e JOIN category c ON c.id = e.category_id"
            " WHERE e.stream_id = ? AND e.occurred_at >= ? AND e.occurred_at < ?"
            + clause
            + " ORDER BY e.occurred_at",
            (stream_id, start_epoch, end_epoch, *params),
        ).fetchall()
        events = [_event_from_row(row) for row in rows]
        if not events:
            return events

        values_by_record = self._values_for(stream_id, [event.id for event in events])
        return [replace(event, values=values_by_record.get(event.id, ())) for event in events]

    def counts_by_category(
        self,
        stream_id: int,
        start_epoch: int,
        end_epoch: int,
        *,
        path_prefix: str | None = None,
    ) -> list[EventCategoryTotal]:
        """Event counts per category over a range, exact for any bounds.

        The event analogue of :meth:`totals_by_category`: an instant either
        falls in ``[start, end)`` or it does not, so there is no clipped
        arithmetic to do.
        """
        clause, params = _sql.prefix_clause(path_prefix)
        rows = self._conn.execute(
            "SELECT c.id AS category_id, c.path AS category_path, COUNT(*) AS record_count"
            " FROM event_record e JOIN category c ON c.id = e.category_id"
            " WHERE e.stream_id = ? AND e.occurred_at >= ? AND e.occurred_at < ?"
            + clause
            + " GROUP BY c.id, c.path ORDER BY record_count DESC, c.path",
            (stream_id, start_epoch, end_epoch, *params),
        ).fetchall()
        return [
            EventCategoryTotal(
                category_id=int(row["category_id"]),
                category_path=row["category_path"],
                record_count=int(row["record_count"]),
            )
            for row in rows
        ]

    def _values_for(
        self, stream_id: int, record_ids: Sequence[int]
    ) -> dict[int, tuple[FieldValue, ...]]:
        """Declared-field values for a batch of events, in declared order."""
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        rows = self._conn.execute(
            "SELECT v.record_id, f.key, f.label, f.unit, v.num, v.txt"
            " FROM event_value v JOIN stream_field f ON f.id = v.field_id"
            f" WHERE f.stream_id = ? AND v.record_id IN ({placeholders})"
            " ORDER BY v.record_id, f.position",
            (stream_id, *record_ids),
        ).fetchall()
        grouped: dict[int, list[FieldValue]] = {}
        for row in rows:
            value: float | str = row["txt"] if row["txt"] is not None else float(row["num"])
            grouped.setdefault(int(row["record_id"]), []).append(
                FieldValue(key=row["key"], label=row["label"], unit=row["unit"], value=value)
            )
        return {record_id: tuple(values) for record_id, values in grouped.items()}


# ----------------------------------------------------------------------
# Row mapping
# ----------------------------------------------------------------------


def _category_key(record: Record) -> tuple[str, str]:
    if record.category_source_id is not None:
        return ("source", record.category_source_id)
    return ("path", record.category_path)


def _record_instant(record: Record) -> Instant:
    return record.start if isinstance(record, IntervalRecord) else record.at


def _stream_from_row(row: sqlite3.Row) -> Stream:
    return Stream(
        id=int(row["id"]),
        key=row["key"],
        name=row["name"],
        kind=StreamKind(row["kind"]),
        exclusive=bool(row["exclusive"]),
        watch_path=row["watch_path"],
        watch_enabled=bool(row["watch_enabled"]),
    )


def _view_from_row(row: sqlite3.Row) -> View:
    return View(
        id=int(row["id"]),
        key=row["key"],
        name=row["name"],
        source_stream_key=row["source_stream_key"],
        category_path=row["category_path"],
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


def _event_from_row(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        id=int(row["id"]),
        stream_id=int(row["stream_id"]),
        category_id=int(row["category_id"]),
        category_path=row["category_path"],
        recorded_path=row["recorded_path"],
        at=Instant(int(row["occurred_at"]), int(row["offset_seconds"])),
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
