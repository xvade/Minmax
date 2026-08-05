"""Precomputed local-time aggregates.

The correlation engine will ask questions like "minutes of ``media/*`` per day".
Answering those from raw observations means scanning everything, every time;
answering them from aggregates is a bounded-size lookup. Since three years
across twenty streams is on the order of 10^5 rollup rows, analysis can scan
rollups exhaustively no matter how large the observation tables grow. That is
what keeps SQLite the right store even once sample streams arrive.

Two paths are provided deliberately:

- :func:`apply_intervals` updates rollups incrementally, in the same
  transaction as the insert that caused them. Cost is proportional to the rows
  inserted, not to the table size.
- :func:`rebuild` recomputes from the observation tables. It is the repair path
  after a revert or a bug, and it is what the incremental path is tested
  against -- a test asserts the two agree, so incremental drift cannot go
  unnoticed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from minmax.core.timeref import Instant, Period, split_by_local_period

__all__ = ["apply_intervals", "rebuild"]

_UPSERT = """
INSERT INTO rollup (stream_id, category_id, period, bucket, seconds, record_count)
VALUES (?, ?, ?, ?, ?, 1)
ON CONFLICT (stream_id, category_id, period, bucket) DO UPDATE SET
    seconds      = seconds + excluded.seconds,
    record_count = record_count + excluded.record_count
"""


def apply_intervals(
    conn: sqlite3.Connection,
    stream_id: int,
    intervals: Iterable[tuple[int, Instant, Instant]],
) -> None:
    """Fold ``(category_id, start, end)`` intervals into the rollup table.

    An interval crossing a bucket boundary is split across the buckets it
    touches, so its seconds are attributed to the right local day even though it
    remains one observation row. ``record_count`` counts observations *touching*
    a bucket, which means it is meaningful within a bucket but must not be
    summed across buckets.

    Must be called inside the caller's transaction so that observations and
    their aggregates can never be committed out of step.
    """
    params: list[tuple[int, int, str, str, int]] = []
    for category_id, start, end in intervals:
        for period in Period:
            for bucket, seconds in split_by_local_period(start, end, period):
                params.append((stream_id, category_id, period.value, bucket, seconds))

    if params:
        conn.executemany(_UPSERT, params)


def rebuild(conn: sqlite3.Connection, stream_id: int | None = None) -> None:
    """Recompute rollups from the observation tables.

    Scoped to one stream when ``stream_id`` is given, otherwise the whole
    database. Used after reverting an import, and as the correctness oracle for
    the incremental path.
    """
    if stream_id is None:
        conn.execute("DELETE FROM rollup")
        stream_ids = [
            int(row[0]) for row in conn.execute("SELECT id FROM stream WHERE kind = 'interval'")
        ]
    else:
        conn.execute("DELETE FROM rollup WHERE stream_id = ?", (stream_id,))
        stream_ids = [stream_id]

    for sid in stream_ids:
        rows = conn.execute(
            """
            SELECT category_id, started_at, start_offset_seconds,
                   ended_at, end_offset_seconds
            FROM interval_record
            WHERE stream_id = ?
            """,
            (sid,),
        ).fetchall()
        apply_intervals(
            conn,
            sid,
            (
                (
                    int(row["category_id"]),
                    Instant(int(row["started_at"]), int(row["start_offset_seconds"])),
                    Instant(int(row["ended_at"]), int(row["end_offset_seconds"])),
                )
                for row in rows
            ),
        )
