"""Masked substitutes for the rollup-backed daily readers.

A view restricts every screen to the times a category was active elsewhere --
data that was never bucketed with that restriction in mind, so it cannot be
read from ``rollup``/``field_rollup``. Design principle 6 ("analysis reads
aggregates, not raw rows") trades off against this: a masked read goes back
to raw observations, the same way the un-bucketed range readers
(:meth:`~minmax.storage.repository.Repository.totals_by_category`,
:meth:`~minmax.storage.repository.Repository.events_in_range`) already do.
See ``docs/architecture.md``'s decision D9.

The three functions here produce the exact same row shapes
:meth:`~minmax.storage.repository.Repository.daily_totals_by_category` and
:meth:`~minmax.storage.repository.Repository.daily_field_totals_by_category`
do, so every caller downstream -- the trends chart, the correlation engine --
can use either source interchangeably without knowing which one answered.
:func:`daily_event_counts_by_category` is the masked analogue of that same
rollup reader's *other* branch: an event stream has no seconds, so a column's
plotted height there is ``record_count`` instead (see ``rollup``'s schema --
one table serves both an interval stream's seconds and an event stream's
count, and this module keeps that split as two functions only because an
interval and an event need genuinely different raw-row handling: a span to
clip versus an instant to test for membership).

Day attribution reuses :func:`~minmax.core.timeref.split_by_local_period`,
the same function :func:`~minmax.storage.rollups.apply_intervals` uses to
write the rollup table in the first place. Sharing it is what guarantees a
full-coverage mask reproduces the rollup path exactly -- the oracle test in
``tests/test_storage.py``.
"""

from __future__ import annotations

import sqlite3

from minmax.core.models import DailyCategoryTotal, DailyFieldTotal
from minmax.core.timeref import Instant, Period, split_by_local_period
from minmax.core.windows import TimeMask
from minmax.storage._sql import prefix_clause

__all__ = [
    "daily_event_counts_by_category",
    "daily_field_totals_by_category",
    "daily_totals_by_category",
]


def daily_totals_by_category(
    conn: sqlite3.Connection,
    stream_id: int,
    mask: TimeMask,
    *,
    start_bucket: str | None = None,
    end_bucket: str | None = None,
    path_prefix: str | None = None,
) -> list[DailyCategoryTotal]:
    """The masked analogue of ``Repository.daily_totals_by_category``.

    Every interval overlapping the mask's overall span is fetched once, then
    clipped to the mask's own spans in Python (:meth:`TimeMask.clip`) before
    each surviving piece is split at local-day boundaries -- an interval that
    straddles a gap in the mask yields several pieces, exactly as one
    straddling a query range's edge already does in
    :meth:`~minmax.storage.repository.Repository.totals_by_category`.
    """
    bounds = mask.bounds()
    if bounds is None:
        return []
    clause, params = prefix_clause(path_prefix)
    rows = conn.execute(
        "SELECT i.category_id, c.path AS category_path, i.started_at,"
        "       i.start_offset_seconds, i.ended_at, i.end_offset_seconds"
        " FROM interval_record i JOIN category c ON c.id = i.category_id"
        " WHERE i.stream_id = ? AND i.started_at < ? AND i.ended_at > ?" + clause,
        (stream_id, bounds[1], bounds[0], *params),
    ).fetchall()

    totals: dict[tuple[str, int], list[int]] = {}
    paths_by_category: dict[int, str] = {}
    for row in rows:
        category_id = int(row["category_id"])
        paths_by_category[category_id] = row["category_path"]
        start_offset = int(row["start_offset_seconds"])
        end_offset = int(row["end_offset_seconds"])
        for piece_start, piece_end in mask.clip(int(row["started_at"]), int(row["ended_at"])):
            start = Instant(piece_start, start_offset)
            end = Instant(piece_end, end_offset)
            for bucket, seconds in split_by_local_period(start, end, Period.DAY):
                if start_bucket is not None and bucket < start_bucket:
                    continue
                if end_bucket is not None and bucket > end_bucket:
                    continue
                entry = totals.setdefault((bucket, category_id), [0, 0])
                entry[0] += seconds
                entry[1] += 1

    entries = [
        DailyCategoryTotal(
            bucket=bucket,
            category_id=category_id,
            category_path=paths_by_category[category_id],
            seconds=seconds,
            record_count=record_count,
        )
        for (bucket, category_id), (seconds, record_count) in totals.items()
    ]
    entries.sort(key=lambda total: (total.bucket, total.category_path))
    return entries


def daily_event_counts_by_category(
    conn: sqlite3.Connection,
    stream_id: int,
    mask: TimeMask,
    *,
    start_bucket: str | None = None,
    end_bucket: str | None = None,
    path_prefix: str | None = None,
) -> list[DailyCategoryTotal]:
    """An event stream's masked count per (local day, category).

    The masked analogue of ``daily_totals_by_category``'s record-count
    branch (see ``InProcessClient.trends``'s ``is_event`` split): an event
    has no span, so :meth:`TimeMask.contains` is enough, the same as
    :func:`daily_field_totals_by_category`. ``seconds`` is always 0 in the
    result, matching how ``rollups.apply_events`` writes the rollup table.
    """
    bounds = mask.bounds()
    if bounds is None:
        return []
    clause, params = prefix_clause(path_prefix)
    rows = conn.execute(
        "SELECT e.category_id, c.path AS category_path, e.occurred_at, e.offset_seconds"
        " FROM event_record e JOIN category c ON c.id = e.category_id"
        " WHERE e.stream_id = ? AND e.occurred_at >= ? AND e.occurred_at < ?" + clause,
        (stream_id, bounds[0], bounds[1], *params),
    ).fetchall()

    counts: dict[tuple[str, int], int] = {}
    paths_by_category: dict[int, str] = {}
    for row in rows:
        occurred_at = int(row["occurred_at"])
        if not mask.contains(occurred_at):
            continue
        instant = Instant(occurred_at, int(row["offset_seconds"]))
        bucket = instant.bucket_key(Period.DAY)
        if start_bucket is not None and bucket < start_bucket:
            continue
        if end_bucket is not None and bucket > end_bucket:
            continue
        category_id = int(row["category_id"])
        paths_by_category[category_id] = row["category_path"]
        key = (bucket, category_id)
        counts[key] = counts.get(key, 0) + 1

    entries = [
        DailyCategoryTotal(
            bucket=bucket,
            category_id=category_id,
            category_path=paths_by_category[category_id],
            seconds=0,
            record_count=count,
        )
        for (bucket, category_id), count in counts.items()
    ]
    entries.sort(key=lambda total: (total.bucket, total.category_path))
    return entries


def daily_field_totals_by_category(
    conn: sqlite3.Connection,
    stream_id: int,
    mask: TimeMask,
    *,
    field_id: int | None = None,
    start_bucket: str | None = None,
    end_bucket: str | None = None,
    path_prefix: str | None = None,
) -> list[DailyFieldTotal]:
    """The masked analogue of ``Repository.daily_field_totals_by_category``.

    An event has no span to clip, so :meth:`TimeMask.contains` is enough --
    it either happened while the view was active or it did not.
    """
    bounds = mask.bounds()
    if bounds is None:
        return []
    clause, params = prefix_clause(path_prefix)
    field_clause = ""
    field_params: tuple[int, ...] = ()
    if field_id is not None:
        field_clause = " AND f.id = ?"
        field_params = (field_id,)

    rows = conn.execute(
        "SELECT e.category_id, c.path AS category_path, e.occurred_at, e.offset_seconds,"
        "       f.id AS field_id, f.key AS field_key, v.num"
        " FROM event_record e"
        " JOIN category c ON c.id = e.category_id"
        " JOIN event_value v ON v.record_id = e.id"
        " JOIN stream_field f ON f.id = v.field_id"
        " WHERE e.stream_id = ? AND e.occurred_at >= ? AND e.occurred_at < ?"
        "   AND v.num IS NOT NULL" + field_clause + clause,
        (stream_id, bounds[0], bounds[1], *field_params, *params),
    ).fetchall()

    sums: dict[tuple[str, int, int], float] = {}
    counts: dict[tuple[str, int, int], int] = {}
    lows: dict[tuple[str, int, int], float] = {}
    highs: dict[tuple[str, int, int], float] = {}
    paths_by_category: dict[int, str] = {}
    keys_by_field: dict[int, str] = {}

    for row in rows:
        occurred_at = int(row["occurred_at"])
        if not mask.contains(occurred_at):
            continue
        instant = Instant(occurred_at, int(row["offset_seconds"]))
        bucket = instant.bucket_key(Period.DAY)
        if start_bucket is not None and bucket < start_bucket:
            continue
        if end_bucket is not None and bucket > end_bucket:
            continue

        category_id = int(row["category_id"])
        this_field_id = int(row["field_id"])
        paths_by_category[category_id] = row["category_path"]
        keys_by_field[this_field_id] = row["field_key"]

        value = float(row["num"])
        key = (bucket, category_id, this_field_id)
        sums[key] = sums.get(key, 0.0) + value
        counts[key] = counts.get(key, 0) + 1
        lows[key] = value if key not in lows else min(lows[key], value)
        highs[key] = value if key not in highs else max(highs[key], value)

    entries = [
        DailyFieldTotal(
            bucket=bucket,
            category_id=category_id,
            category_path=paths_by_category[category_id],
            field_id=this_field_id,
            field_key=keys_by_field[this_field_id],
            value_sum=total_sum,
            value_count=counts[bucket, category_id, this_field_id],
            value_min=lows[bucket, category_id, this_field_id],
            value_max=highs[bucket, category_id, this_field_id],
        )
        for (bucket, category_id, this_field_id), total_sum in sums.items()
    ]
    entries.sort(key=lambda total: (total.bucket, total.category_path, total.field_key))
    return entries
