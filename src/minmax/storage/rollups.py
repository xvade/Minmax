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

__all__ = ["apply_event_values", "apply_events", "apply_intervals", "rebuild"]

_UPSERT = """
INSERT INTO rollup (stream_id, category_id, period, bucket, seconds, record_count)
VALUES (?, ?, ?, ?, ?, 1)
ON CONFLICT (stream_id, category_id, period, bucket) DO UPDATE SET
    seconds      = seconds + excluded.seconds,
    record_count = record_count + excluded.record_count
"""

_FIELD_UPSERT = """
INSERT INTO field_rollup
    (stream_id, category_id, field_id, period, bucket, value_sum, value_count, value_min, value_max)
VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
ON CONFLICT (stream_id, category_id, field_id, period, bucket) DO UPDATE SET
    value_sum   = value_sum + excluded.value_sum,
    value_count = value_count + excluded.value_count,
    value_min   = MIN(value_min, excluded.value_min),
    value_max   = MAX(value_max, excluded.value_max)
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


def apply_events(
    conn: sqlite3.Connection,
    stream_id: int,
    events: Iterable[tuple[int, Instant]],
) -> None:
    """Fold ``(category_id, at)`` events into the rollup table as counts.

    Unlike an interval, a point-in-time observation touches exactly one
    bucket per period -- there is nothing to split -- so each event
    contributes ``seconds = 0, record_count = 1``. Because of that, an event
    stream's ``record_count`` is summable *across* buckets (ten events today
    plus ten yesterday is twenty events), unlike an interval stream's, which
    is only meaningful *within* one bucket, since a single long run touches
    several without being several observations.

    Must be called inside the caller's transaction, as for
    :func:`apply_intervals`.
    """
    params = [
        (stream_id, category_id, period.value, at.bucket_key(period), 0)
        for category_id, at in events
        for period in Period
    ]
    if params:
        conn.executemany(_UPSERT, params)


def apply_event_values(
    conn: sqlite3.Connection,
    stream_id: int,
    values: Iterable[tuple[int, int, Instant, float]],
) -> None:
    """Fold ``(category_id, field_id, at, value)`` numeric readings into
    ``field_rollup``.

    The mean is deliberately never stored, only ``value_sum`` and
    ``value_count``, with the mean always derived as their ratio at read
    time. Storing a mean would make a multi-day column's figure the mean of
    daily means rather than the correct value-weighted one -- silently wrong
    the moment two days have different counts.

    Must be called inside the caller's transaction, as for
    :func:`apply_intervals`.
    """
    params = [
        (
            stream_id,
            category_id,
            field_id,
            period.value,
            at.bucket_key(period),
            value,
            value,
            value,
        )
        for category_id, field_id, at, value in values
        for period in Period
    ]
    if params:
        conn.executemany(_FIELD_UPSERT, params)


def rebuild(conn: sqlite3.Connection, stream_id: int | None = None) -> None:
    """Recompute rollups from the observation tables.

    Scoped to one stream when ``stream_id`` is given, otherwise every stream.
    Used after reverting an import, and as the correctness oracle for the
    incremental path. Dispatches per stream by kind, since an interval
    stream's rollups come from ``interval_record`` and a point-in-time
    stream's come from ``event_record``/``event_value``.
    """
    if stream_id is None:
        conn.execute("DELETE FROM rollup")
        conn.execute("DELETE FROM field_rollup")
        streams = conn.execute("SELECT id, kind FROM stream").fetchall()
    else:
        conn.execute("DELETE FROM rollup WHERE stream_id = ?", (stream_id,))
        conn.execute("DELETE FROM field_rollup WHERE stream_id = ?", (stream_id,))
        streams = conn.execute("SELECT id, kind FROM stream WHERE id = ?", (stream_id,)).fetchall()

    for row in streams:
        sid, kind = int(row["id"]), row["kind"]
        if kind == "interval":
            _rebuild_intervals(conn, sid)
        elif kind == "event":
            _rebuild_events(conn, sid)


def _rebuild_intervals(conn: sqlite3.Connection, stream_id: int) -> None:
    rows = conn.execute(
        "SELECT category_id, started_at, start_offset_seconds, ended_at, end_offset_seconds"
        " FROM interval_record WHERE stream_id = ?",
        (stream_id,),
    ).fetchall()
    apply_intervals(
        conn,
        stream_id,
        (
            (
                int(row["category_id"]),
                Instant(int(row["started_at"]), int(row["start_offset_seconds"])),
                Instant(int(row["ended_at"]), int(row["end_offset_seconds"])),
            )
            for row in rows
        ),
    )


def _rebuild_events(conn: sqlite3.Connection, stream_id: int) -> None:
    rows = conn.execute(
        "SELECT category_id, occurred_at, offset_seconds FROM event_record WHERE stream_id = ?",
        (stream_id,),
    ).fetchall()
    apply_events(
        conn,
        stream_id,
        (
            (int(row["category_id"]), Instant(int(row["occurred_at"]), int(row["offset_seconds"])))
            for row in rows
        ),
    )

    value_rows = conn.execute(
        "SELECT e.category_id, v.field_id, e.occurred_at, e.offset_seconds, v.num"
        " FROM event_value v JOIN event_record e ON e.id = v.record_id"
        " WHERE e.stream_id = ? AND v.num IS NOT NULL",
        (stream_id,),
    ).fetchall()
    apply_event_values(
        conn,
        stream_id,
        (
            (
                int(row["category_id"]),
                int(row["field_id"]),
                Instant(int(row["occurred_at"]), int(row["offset_seconds"])),
                float(row["num"]),
            )
            for row in value_rows
        ),
    )
