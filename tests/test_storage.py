"""Tests for the schema, the repository, and rollup maintenance.

Two categories of test here matter more than the rest:

- **Constraint tests** assert the database *rejects* bad data. The data-safety
  invariant is only as good as what the schema refuses.
- **The rollup equivalence test** asserts the fast incremental path agrees with
  a full recompute. Aggregates that silently drift would be worse than no
  aggregates at all, and drift is otherwise invisible.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from minmax.core.models import IntervalRecord, Stream, StreamKind
from minmax.core.timeref import Instant, Period
from minmax.storage import rollups
from minmax.storage.database import migrate, open_database
from minmax.storage.repository import OverlapError, Repository

HOUR = 3600


def at(text: str) -> Instant:
    return Instant.from_iso(text)


def record(path: str, start: str, end: str, *, source_id: str | None = None) -> IntervalRecord:
    return IntervalRecord(
        category_source_id=source_id if source_id is not None else path,
        category_path=path,
        start=at(start),
        end=at(end),
    )


@pytest.fixture
def stream(repository: Repository) -> Stream:
    return repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL, exclusive=True)


@pytest.fixture
def run_id(repository: Repository, stream: Stream) -> int:
    assert stream.id is not None
    return repository.begin_import(
        stream.id,
        importer="test",
        source_name="test.csv",
        source_sha256="0" * 64,
        archived_path=None,
    )


class TestMigrations:
    def test_fresh_database_is_at_the_latest_version(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 1

    def test_migrating_again_is_a_no_op(self, connection: sqlite3.Connection) -> None:
        before = migrate(connection)
        assert migrate(connection) == before

    def test_reopening_an_existing_database_preserves_data(
        self, home: Path, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        assert stream.id is not None
        repository.insert_intervals(
            stream,
            run_id,
            [record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")],
        )
        repository.connection.close()

        reopened = open_database(home / "minmax.db")
        try:
            count = reopened.execute("SELECT COUNT(*) FROM interval_record").fetchone()[0]
            assert count == 1
        finally:
            reopened.close()

    def test_foreign_keys_are_enforced(self, connection: sqlite3.Connection) -> None:
        # Off by default in SQLite and per-connection, so worth asserting.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO category (stream_id, source_id, path, top_level,"
                " first_seen_at, last_seen_at) VALUES (999, 'x', 'p', 'p', 0, 0)"
            )


class TestConstraints:
    def test_duration_must_match_the_timestamps(
        self, connection: sqlite3.Connection, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        assert stream.id is not None
        category_id = repository.ensure_category(
            stream.id, source_id="x", path="admin", first_seen_at=0, last_seen_at=0
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO interval_record (stream_id, category_id, import_run_id,"
                " started_at, ended_at, start_offset_seconds, end_offset_seconds,"
                " duration_seconds, recorded_path) VALUES (?, ?, ?, 100, 200, 0, 0, 999, 'admin')",
                (stream.id, category_id, run_id),
            )

    def test_an_interval_cannot_end_before_it_starts(
        self, connection: sqlite3.Connection, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        assert stream.id is not None
        category_id = repository.ensure_category(
            stream.id, source_id="x", path="admin", first_seen_at=0, last_seen_at=0
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO interval_record (stream_id, category_id, import_run_id,"
                " started_at, ended_at, start_offset_seconds, end_offset_seconds,"
                " duration_seconds, recorded_path) VALUES (?, ?, ?, 200, 100, 0, 0, -100, 'admin')",
                (stream.id, category_id, run_id),
            )

    def test_the_model_rejects_an_inverted_interval_before_sql_does(self) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            record("admin", "2026-06-06T10:00:00-07:00", "2026-06-06T09:00:00-07:00")


class TestStreamsAndCategories:
    def test_ensure_stream_is_idempotent(self, repository: Repository) -> None:
        first = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        second = repository.ensure_stream("timecard", "Renamed", StreamKind.INTERVAL)
        assert first.id == second.id
        assert len(repository.list_streams()) == 1

    def test_a_renamed_category_keeps_its_identity(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # The real export contains exactly this: one timer_id under both
        # 'social' and, later, 'social/friends'.
        assert stream.id is not None
        repository.insert_intervals(
            stream,
            run_id,
            [
                record(
                    "social",
                    "2026-06-06T07:00:00-07:00",
                    "2026-06-06T08:00:00-07:00",
                    source_id="B",
                ),
                record(
                    "social/friends",
                    "2026-06-06T09:00:00-07:00",
                    "2026-06-06T10:00:00-07:00",
                    source_id="B",
                ),
            ],
        )
        categories = repository.list_categories(stream.id)
        assert len(categories) == 1
        assert categories[0].path == "social/friends"

        # Both runs still belong to it, each remembering the path it was
        # recorded under.
        stored = repository.intervals_in_range(stream.id, 0, 2**31)
        assert [item.recorded_path for item in stored] == ["social", "social/friends"]
        assert {item.category_id for item in stored} == {categories[0].id}

    def test_importing_older_data_does_not_roll_a_name_backwards(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        assert stream.id is not None
        repository.insert_intervals(
            stream,
            run_id,
            [
                record(
                    "social/friends",
                    "2026-06-06T09:00:00-07:00",
                    "2026-06-06T10:00:00-07:00",
                    source_id="B",
                )
            ],
        )
        repository.insert_intervals(
            stream,
            run_id,
            [
                record(
                    "social",
                    "2026-06-01T09:00:00-07:00",
                    "2026-06-01T10:00:00-07:00",
                    source_id="B",
                )
            ],
        )
        assert repository.list_categories(stream.id)[0].path == "social/friends"

    def test_categories_are_created_once_per_batch(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        assert stream.id is not None
        repository.insert_intervals(
            stream,
            run_id,
            [
                record(
                    "admin", f"2026-06-06T{h:02d}:00:00-07:00", f"2026-06-06T{h:02d}:30:00-07:00"
                )
                for h in range(9, 15)
            ],
        )
        assert len(repository.list_categories(stream.id)) == 1


class TestIdempotency:
    def test_reinserting_the_same_records_inserts_nothing(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        records = [
            record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
            record("media/youtube", "2026-06-06T10:00:00-07:00", "2026-06-06T11:00:00-07:00"),
        ]
        first = repository.insert_intervals(stream, run_id, records)
        second = repository.insert_intervals(stream, run_id, records)

        assert (first.inserted, first.duplicates) == (2, 0)
        assert (second.inserted, second.duplicates) == (0, 2)
        assert stream.id is not None
        assert len(repository.intervals_in_range(stream.id, 0, 2**31)) == 2

    def test_duplicate_rows_do_not_double_count_rollups(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        records = [record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")]
        repository.insert_intervals(stream, run_id, records)
        repository.insert_intervals(stream, run_id, records)

        assert stream.id is not None
        totals = repository.totals_by_period(stream.id, Period.DAY)
        assert [(t.bucket, t.seconds) for t in totals] == [("2026-06-06", HOUR)]


class TestExclusiveStreams:
    def test_overlapping_incoming_rows_are_rejected(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        with pytest.raises(OverlapError, match="incoming rows overlap"):
            repository.insert_intervals(
                stream,
                run_id,
                [
                    record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                    record("social", "2026-06-06T09:30:00-07:00", "2026-06-06T10:30:00-07:00"),
                ],
            )

    def test_an_overlap_with_stored_data_is_rejected(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        repository.insert_intervals(
            stream,
            run_id,
            [record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")],
        )
        with pytest.raises(OverlapError, match="overlaps stored"):
            repository.insert_intervals(
                stream,
                run_id,
                [record("social", "2026-06-06T09:30:00-07:00", "2026-06-06T11:00:00-07:00")],
            )

    def test_a_rejected_batch_stores_nothing(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # The whole import must roll back, not just the offending row.
        with pytest.raises(OverlapError):
            repository.insert_intervals(
                stream,
                run_id,
                [
                    record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                    record("social", "2026-06-06T09:30:00-07:00", "2026-06-06T10:30:00-07:00"),
                ],
            )
        assert stream.id is not None
        assert repository.intervals_in_range(stream.id, 0, 2**31) == []

    def test_abutting_runs_are_not_overlaps(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # 325 of 337 adjacent runs in the real export share an exact boundary,
        # because switching timers closes one run and opens the next.
        outcome = repository.insert_intervals(
            stream,
            run_id,
            [
                record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                record("social", "2026-06-06T10:00:00-07:00", "2026-06-06T11:00:00-07:00"),
            ],
        )
        assert outcome.inserted == 2

    def test_a_non_exclusive_stream_permits_overlaps(self, repository: Repository) -> None:
        calendar = repository.ensure_stream(
            "calendar", "Calendar", StreamKind.INTERVAL, exclusive=False
        )
        assert calendar.id is not None
        run = repository.begin_import(
            calendar.id,
            importer="test",
            source_name="c.ics",
            source_sha256="1" * 64,
            archived_path=None,
        )
        outcome = repository.insert_intervals(
            calendar,
            run,
            [
                record("work/standup", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                record("work/review", "2026-06-06T09:30:00-07:00", "2026-06-06T10:30:00-07:00"),
            ],
        )
        assert outcome.inserted == 2


class TestRetrieval:
    @pytest.fixture
    def populated(self, repository: Repository, stream: Stream, run_id: int) -> Stream:
        repository.insert_intervals(
            stream,
            run_id,
            [
                record("sleep/night", "2026-06-05T23:00:00-07:00", "2026-06-06T07:00:00-07:00"),
                record(
                    "media/gaming/chess", "2026-06-06T07:00:00-07:00", "2026-06-06T08:00:00-07:00"
                ),
                record("media/youtube", "2026-06-06T08:00:00-07:00", "2026-06-06T09:00:00-07:00"),
                record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T11:00:00-07:00"),
            ],
        )
        return stream

    def test_range_query_includes_partially_overlapping_records(
        self, repository: Repository, populated: Stream
    ) -> None:
        assert populated.id is not None
        # A sleep run beginning the previous evening must appear when the 6th is
        # displayed -- the same rule that makes Timecard draw it as one block.
        day_start = at("2026-06-06T00:00:00-07:00").epoch_seconds
        day_end = at("2026-06-07T00:00:00-07:00").epoch_seconds
        found = repository.intervals_in_range(populated.id, day_start, day_end)
        assert [item.recorded_path for item in found] == [
            "sleep/night",
            "media/gaming/chess",
            "media/youtube",
            "admin",
        ]

    def test_totals_clip_to_the_queried_range(
        self, repository: Repository, populated: Stream
    ) -> None:
        assert populated.id is not None
        day_start = at("2026-06-06T00:00:00-07:00").epoch_seconds
        day_end = at("2026-06-07T00:00:00-07:00").epoch_seconds
        totals = {
            t.category_path: t.seconds
            for t in repository.totals_by_category(populated.id, day_start, day_end)
        }
        # Only the portion of the sleep run that falls on the 6th.
        assert totals["sleep/night"] == 7 * HOUR
        assert totals["admin"] == 2 * HOUR

    def test_prefix_restricts_to_a_subtree(self, repository: Repository, populated: Stream) -> None:
        assert populated.id is not None
        totals = repository.totals_by_category(populated.id, 0, 2**31, path_prefix="media")
        assert {t.category_path for t in totals} == {"media/gaming/chess", "media/youtube"}

    def test_prefix_matching_is_component_wise(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        repository.insert_intervals(
            stream,
            run_id,
            [
                record("media/youtube", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                record("mediation/notes", "2026-06-06T10:00:00-07:00", "2026-06-06T11:00:00-07:00"),
            ],
        )
        assert stream.id is not None
        totals = repository.totals_by_category(stream.id, 0, 2**31, path_prefix="media")
        assert {t.category_path for t in totals} == {"media/youtube"}

    def test_like_wildcards_in_a_category_name_are_literal(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # A category actually named '100%' must not behave as a LIKE wildcard.
        repository.insert_intervals(
            stream,
            run_id,
            [
                record("100%/focus", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                record("1000/other", "2026-06-06T10:00:00-07:00", "2026-06-06T11:00:00-07:00"),
            ],
        )
        assert stream.id is not None
        totals = repository.totals_by_category(stream.id, 0, 2**31, path_prefix="100%")
        assert {t.category_path for t in totals} == {"100%/focus"}

    def test_observed_range_spans_everything_stored(
        self, repository: Repository, populated: Stream
    ) -> None:
        assert populated.id is not None
        bounds = repository.observed_range(populated.id)
        assert bounds == (
            at("2026-06-05T23:00:00-07:00").epoch_seconds,
            at("2026-06-06T11:00:00-07:00").epoch_seconds,
        )

    def test_observed_range_is_none_when_empty(
        self, repository: Repository, stream: Stream
    ) -> None:
        assert stream.id is not None
        assert repository.observed_range(stream.id) is None


class TestRollups:
    def test_a_midnight_crossing_run_is_attributed_to_both_days(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        repository.insert_intervals(
            stream,
            run_id,
            [record("sleep/night", "2026-06-05T23:00:00-07:00", "2026-06-06T07:00:00-07:00")],
        )
        assert stream.id is not None
        totals = repository.totals_by_period(stream.id, Period.DAY)
        assert [(t.bucket, t.seconds) for t in totals] == [
            ("2026-06-05", HOUR),
            ("2026-06-06", 7 * HOUR),
        ]

    def test_incremental_rollups_match_a_full_rebuild(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # The guard against silent aggregate drift. Inserted in several batches
        # so the incremental path is genuinely exercised, and offset from the
        # hour so that runs straddle hourly buckets as well as daily ones.
        for hour in range(9, 20, 2):
            repository.insert_intervals(
                stream,
                run_id,
                [
                    record(
                        "admin" if hour % 4 else "media/youtube",
                        f"2026-06-06T{hour:02d}:17:00-07:00",
                        f"2026-06-06T{hour + 1:02d}:43:00-07:00",
                    )
                ],
            )
        repository.insert_intervals(
            stream,
            run_id,
            [record("sleep/night", "2026-06-06T22:00:00-07:00", "2026-06-07T06:30:00-07:00")],
        )

        def snapshot() -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in repository.connection.execute(
                    "SELECT category_id, period, bucket, seconds, record_count FROM rollup"
                    " ORDER BY period, bucket, category_id"
                ).fetchall()
            ]

        incremental = snapshot()
        rollups.rebuild(repository.connection)
        assert snapshot() == incremental
        assert incremental, "expected some rollup rows"

    def test_rollup_seconds_reconcile_with_the_observations(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        repository.insert_intervals(
            stream,
            run_id,
            [
                record("sleep/night", "2026-06-05T23:00:00-07:00", "2026-06-06T07:00:00-07:00"),
                record("admin", "2026-06-06T07:00:00-07:00", "2026-06-06T09:30:00-07:00"),
            ],
        )
        assert stream.id is not None
        from_rollups = sum(t.seconds for t in repository.totals_by_period(stream.id, Period.DAY))
        from_records = sum(t.seconds for t in repository.totals_by_category(stream.id, 0, 2**31))
        assert from_rollups == from_records

    def test_hour_and_day_rollups_agree(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        repository.insert_intervals(
            stream,
            run_id,
            [record("sleep/night", "2026-06-05T23:00:00-07:00", "2026-06-06T07:00:00-07:00")],
        )
        assert stream.id is not None
        hourly = sum(t.seconds for t in repository.totals_by_period(stream.id, Period.HOUR))
        daily = sum(t.seconds for t in repository.totals_by_period(stream.id, Period.DAY))
        assert hourly == daily == 8 * HOUR


class TestRevert:
    def test_reverting_removes_only_that_import(
        self, repository: Repository, stream: Stream
    ) -> None:
        assert stream.id is not None
        first = repository.begin_import(
            stream.id,
            importer="test",
            source_name="a.csv",
            source_sha256="a" * 64,
            archived_path=None,
        )
        second = repository.begin_import(
            stream.id,
            importer="test",
            source_name="b.csv",
            source_sha256="b" * 64,
            archived_path=None,
        )
        repository.insert_intervals(
            stream,
            first,
            [record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")],
        )
        repository.insert_intervals(
            stream,
            second,
            [record("social", "2026-06-06T10:00:00-07:00", "2026-06-06T11:00:00-07:00")],
        )

        assert repository.revert_import(first) == 1
        remaining = repository.intervals_in_range(stream.id, 0, 2**31)
        assert [item.recorded_path for item in remaining] == ["social"]

    def test_reverting_rebuilds_rollups(self, repository: Repository, stream: Stream) -> None:
        assert stream.id is not None
        run = repository.begin_import(
            stream.id,
            importer="test",
            source_name="a.csv",
            source_sha256="a" * 64,
            archived_path=None,
        )
        repository.insert_intervals(
            stream, run, [record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")]
        )
        repository.revert_import(run)
        assert repository.totals_by_period(stream.id, Period.DAY) == []

    def test_reverting_an_unknown_import_raises(self, repository: Repository) -> None:
        with pytest.raises(LookupError):
            repository.revert_import(12345)
