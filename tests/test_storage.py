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

from minmax.core.models import (
    EventRecord,
    FieldRole,
    FieldSpec,
    IntervalRecord,
    Stream,
    StreamKind,
    implicit_category_path,
)
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


def event(
    path: str,
    when: str,
    *,
    source_id: str | None = None,
    values: dict[str, float | str] | None = None,
) -> EventRecord:
    return EventRecord(
        category_source_id=source_id,
        category_path=path,
        at=at(when),
        values=values or {},
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


@pytest.fixture
def heart_rate_stream(repository: Repository) -> Stream:
    """One numeric field: the shape a bare measurement stream takes."""
    return repository.create_stream(
        "heart_rate",
        "Heart Rate",
        StreamKind.EVENT,
        fields=[
            FieldSpec(
                key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
            )
        ],
    )


@pytest.fixture
def pickups_stream(repository: Repository) -> Stream:
    """No declared fields at all: the shape a bare "it happened" stream takes."""
    return repository.create_stream("pickups", "Phone Pickups", StreamKind.EVENT)


@pytest.fixture
def event_run_id(repository: Repository, heart_rate_stream: Stream) -> int:
    assert heart_rate_stream.id is not None
    return repository.begin_import(
        heart_rate_stream.id,
        importer="test",
        source_name="test.csv",
        source_sha256="e" * 64,
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

    def test_a_new_stream_has_no_watch_configured(self, repository: Repository) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.watch_path is None
        assert stream.watch_enabled is False

    def test_set_stream_watch_persists_the_path_and_enabled_flag(
        self, repository: Repository, stream: Stream
    ) -> None:
        assert stream.id is not None
        repository.set_stream_watch(stream.id, path="/data/feed.csv", enabled=True)

        reloaded = repository.get_stream(stream.key)
        assert reloaded is not None
        assert reloaded.watch_path == "/data/feed.csv"
        assert reloaded.watch_enabled is True

    def test_set_stream_watch_with_no_path_leaves_the_remembered_one_alone(
        self, repository: Repository, stream: Stream
    ) -> None:
        assert stream.id is not None
        repository.set_stream_watch(stream.id, path="/data/feed.csv", enabled=True)

        # Pausing (or resuming) auto-fetch should not require re-stating the path.
        repository.set_stream_watch(stream.id, path=None, enabled=False)

        reloaded = repository.get_stream(stream.key)
        assert reloaded is not None
        assert reloaded.watch_path == "/data/feed.csv"
        assert reloaded.watch_enabled is False

    def test_list_streams_carries_the_watch_state_too(
        self, repository: Repository, stream: Stream
    ) -> None:
        assert stream.id is not None
        repository.set_stream_watch(stream.id, path="/data/feed.csv", enabled=True)

        (reloaded,) = [s for s in repository.list_streams() if s.key == stream.key]
        assert reloaded.watch_path == "/data/feed.csv"
        assert reloaded.watch_enabled is True

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


class TestEventStreams:
    """Point-in-time streams: the schema added for migration 0004.

    Mirrors the interval test classes above wherever the same invariant
    applies, plus the invariants unique to a payload-hash natural key and a
    per-field rollup.
    """

    def test_migration_lands_at_version_4(self, connection: sqlite3.Connection) -> None:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 4

    def test_create_stream_declares_its_fields_in_order(
        self, repository: Repository, heart_rate_stream: Stream
    ) -> None:
        assert heart_rate_stream.id is not None
        fields = repository.stream_fields(heart_rate_stream.id)
        assert [f.key for f in fields] == ["bpm"]
        assert fields[0].role is FieldRole.NUMBER
        assert fields[0].unit == "bpm"

    def test_create_stream_rejects_a_duplicate_key(
        self, repository: Repository, heart_rate_stream: Stream
    ) -> None:
        with pytest.raises(ValueError, match="already exists"):
            repository.create_stream("heart_rate", "Heart Rate Again", StreamKind.EVENT)

    def test_a_stream_with_no_category_column_files_under_the_stream_key(
        self, repository: Repository, pickups_stream: Stream
    ) -> None:
        assert pickups_stream.id is not None
        run = repository.begin_import(
            pickups_stream.id,
            importer="test",
            source_name="pickups.csv",
            source_sha256="p" * 64,
            archived_path=None,
        )
        implicit = implicit_category_path("pickups")
        repository.insert_events(
            pickups_stream, run, [event(implicit, "2026-06-06T09:00:00-07:00")]
        )
        categories = repository.list_categories(pickups_stream.id)
        assert [c.path for c in categories] == [implicit]

    def test_two_distinct_readings_at_the_same_instant_both_survive(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        # Two GPS fixes (or heart-rate readings) at the same second are
        # different observations; the payload distinguishes them.
        outcome = repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0}),
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 62.0}),
            ],
        )
        assert (outcome.inserted, outcome.duplicates) == (2, 0)

    def test_reimporting_identical_events_inserts_nothing(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        records = [event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0})]
        first = repository.insert_events(heart_rate_stream, event_run_id, records)
        second = repository.insert_events(heart_rate_stream, event_run_id, records)
        assert (first.inserted, first.duplicates) == (1, 0)
        assert (second.inserted, second.duplicates) == (0, 1)

    def test_field_less_events_at_the_same_instant_collapse(
        self, repository: Repository, pickups_stream: Stream
    ) -> None:
        # Named limitation (docs/architecture.md §6.1): with no declared
        # fields the payload hash is constant, so two field-less events at
        # the same instant are indistinguishable under the natural key.
        assert pickups_stream.id is not None
        run = repository.begin_import(
            pickups_stream.id,
            importer="test",
            source_name="pickups.csv",
            source_sha256="q" * 64,
            archived_path=None,
        )
        implicit = implicit_category_path("pickups")
        outcome = repository.insert_events(
            pickups_stream,
            run,
            [
                event(implicit, "2026-06-06T09:00:00-07:00"),
                event(implicit, "2026-06-06T09:00:00-07:00"),
            ],
        )
        assert (outcome.inserted, outcome.duplicates) == (1, 1)

    def test_event_value_requires_exactly_one_of_number_or_text(
        self, connection: sqlite3.Connection, repository: Repository, heart_rate_stream: Stream
    ) -> None:
        assert heart_rate_stream.id is not None
        category_id = repository.ensure_category(
            heart_rate_stream.id, source_id=None, path="heart_rate", first_seen_at=0, last_seen_at=0
        )
        run = repository.begin_import(
            heart_rate_stream.id,
            importer="test",
            source_name="x.csv",
            source_sha256="z" * 64,
            archived_path=None,
        )
        cursor = connection.execute(
            "INSERT INTO event_record (stream_id, category_id, import_run_id, occurred_at,"
            " offset_seconds, recorded_path, payload_hash)"
            " VALUES (?, ?, ?, 0, 0, 'heart_rate', 'h')",
            (heart_rate_stream.id, category_id, run),
        )
        field_id = repository.stream_fields(heart_rate_stream.id)[0].id
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO event_value (record_id, field_id, num, txt) VALUES (?, ?, NULL, NULL)",
                (cursor.lastrowid, field_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO event_value (record_id, field_id, num, txt) VALUES (?, ?, 1.0, 'x')",
                (cursor.lastrowid, field_id),
            )

    def test_deleting_an_event_cascades_to_its_values(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0})],
        )
        conn = repository.connection
        record_id = conn.execute("SELECT id FROM event_record").fetchone()[0]
        conn.execute("DELETE FROM event_record WHERE id = ?", (record_id,))
        assert conn.execute("SELECT COUNT(*) FROM event_value").fetchone()[0] == 0

    def test_events_in_range_returns_declared_field_values_in_order(
        self, repository: Repository
    ) -> None:
        location = repository.create_stream(
            "location",
            "Location",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="lat", label="Latitude", role=FieldRole.NUMBER, source_column="lat"),
                FieldSpec(key="lon", label="Longitude", role=FieldRole.NUMBER, source_column="lon"),
                FieldSpec(key="note", label="Note", role=FieldRole.TEXT, source_column="note"),
            ],
        )
        assert location.id is not None
        run = repository.begin_import(
            location.id,
            importer="test",
            source_name="loc.csv",
            source_sha256="l" * 64,
            archived_path=None,
        )
        repository.insert_events(
            location,
            run,
            [
                event(
                    "location",
                    "2026-06-06T09:00:00-07:00",
                    values={"lat": 47.6, "lon": -122.3, "note": "home"},
                )
            ],
        )
        [found] = repository.events_in_range(location.id, 0, 2**31)
        assert [(v.key, v.value) for v in found.values] == [
            ("lat", 47.6),
            ("lon", -122.3),
            ("note", "home"),
        ]

    def test_counts_by_category_is_exact_for_any_bounds(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", values={"bpm": 63.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 65.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        day_start = at("2026-06-06T00:00:00-07:00").epoch_seconds
        day_end = at("2026-06-07T00:00:00-07:00").epoch_seconds
        [total] = repository.counts_by_category(heart_rate_stream.id, day_start, day_end)
        assert total.record_count == 2

    def test_field_rollup_folds_sum_count_min_max(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 60.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", values={"bpm": 80.0}),
                event("heart_rate", "2026-06-06T11:00:00-07:00", values={"bpm": 70.0}),
            ],
        )
        row = repository.connection.execute(
            "SELECT value_sum, value_count, value_min, value_max FROM field_rollup"
            " WHERE period = 'day' AND bucket = '2026-06-06'"
        ).fetchone()
        assert (row["value_sum"], row["value_count"], row["value_min"], row["value_max"]) == (
            210.0,
            3,
            60.0,
            80.0,
        )

    def test_incremental_field_rollups_match_a_full_rebuild(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        for hour in range(8, 20, 2):
            repository.insert_events(
                heart_rate_stream,
                event_run_id,
                [
                    event(
                        "heart_rate",
                        f"2026-06-06T{hour:02d}:15:00-07:00",
                        values={"bpm": float(60 + hour)},
                    )
                ],
            )
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 72.0})],
        )

        def snapshot(table: str) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in repository.connection.execute(
                    f"SELECT * FROM {table} ORDER BY period, bucket, category_id"
                ).fetchall()
            ]

        rollup_before, field_before = snapshot("rollup"), snapshot("field_rollup")
        assert heart_rate_stream.id is not None
        rollups.rebuild(repository.connection, heart_rate_stream.id)
        assert snapshot("rollup") == rollup_before
        assert snapshot("field_rollup") == field_before
        assert field_before, "expected some field_rollup rows"

    def test_reverting_an_event_import_removes_events_and_rebuilds_both_rollups(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0})],
        )
        assert heart_rate_stream.id is not None
        assert repository.revert_import(event_run_id) == 1
        assert repository.events_in_range(heart_rate_stream.id, 0, 2**31) == []
        assert repository.connection.execute("SELECT COUNT(*) FROM rollup").fetchone()[0] == 0
        assert repository.connection.execute("SELECT COUNT(*) FROM field_rollup").fetchone()[0] == 0

    def test_observed_range_works_for_an_event_only_stream(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 65.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        bounds = repository.observed_range(heart_rate_stream.id)
        assert bounds == (
            at("2026-06-06T09:00:00-07:00").epoch_seconds,
            at("2026-06-07T09:00:00-07:00").epoch_seconds,
        )

    def test_insert_records_dispatches_by_stream_kind(
        self,
        repository: Repository,
        stream: Stream,
        run_id: int,
        heart_rate_stream: Stream,
        event_run_id: int,
    ) -> None:
        interval_records = [
            record("admin", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")
        ]
        outcome = repository.insert_records(stream, run_id, interval_records)
        assert outcome.inserted == 1

        event_records = [event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 61.0})]
        outcome = repository.insert_records(heart_rate_stream, event_run_id, event_records)
        assert outcome.inserted == 1

    def test_insert_records_rejects_a_kind_mismatch_instead_of_silently_dropping(
        self, repository: Repository, stream: Stream, run_id: int
    ) -> None:
        # The real failure mode this guards: an event fed through the interval
        # path would otherwise vanish silently, since a zero-length span
        # produces no rollup bucket and no error.
        misrouted = [event("admin", "2026-06-06T09:00:00-07:00")]
        with pytest.raises(ValueError, match="interval"):
            repository.insert_records(stream, run_id, misrouted)  # type: ignore[arg-type]


class TestDailyFieldTotals:
    """``daily_field_totals_by_category`` is the first reader ``field_rollup``
    has ever had -- it was write-only from migration 0004 until now."""

    def test_returns_sum_count_min_max_per_day(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 60.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", values={"bpm": 80.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 65.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        totals = repository.daily_field_totals_by_category(heart_rate_stream.id)

        by_bucket = {t.bucket: t for t in totals}
        assert by_bucket["2026-06-06"].value_sum == 140.0
        assert by_bucket["2026-06-06"].value_count == 2
        assert by_bucket["2026-06-06"].value_min == 60.0
        assert by_bucket["2026-06-06"].value_max == 80.0
        assert by_bucket["2026-06-07"].value_sum == 65.0
        assert by_bucket["2026-06-07"].value_count == 1
        assert all(t.field_key == "bpm" for t in totals)

    def test_never_derives_a_mean(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        # Two readings averaging 70 one day, one reading of 100 the next: the
        # correct multi-day mean is value-weighted (240 / 3 = 80), which is
        # only computable from sum/count kept separate -- so nothing here may
        # precompute a mean that would silently be the wrong one.
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 60.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", values={"bpm": 80.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 100.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        totals = repository.daily_field_totals_by_category(heart_rate_stream.id)
        assert sum(t.value_sum for t in totals) / sum(t.value_count for t in totals) == 80.0

    def test_bucket_bounds_are_inclusive(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-05T09:00:00-07:00", values={"bpm": 60.0}),
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 70.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 80.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        totals = repository.daily_field_totals_by_category(
            heart_rate_stream.id, start_bucket="2026-06-05", end_bucket="2026-06-06"
        )
        assert {t.bucket for t in totals} == {"2026-06-05", "2026-06-06"}

    def test_path_prefix_matches_component_wise(self, repository: Repository) -> None:
        stream = repository.create_stream(
            "location",
            "Location",
            StreamKind.EVENT,
            fields=[
                FieldSpec(
                    key="device", label="Device", role=FieldRole.CATEGORY, source_column="device"
                ),
                FieldSpec(key="lat", label="Latitude", role=FieldRole.NUMBER, source_column="lat"),
            ],
        )
        assert stream.id is not None
        run = repository.begin_import(
            stream.id,
            importer="test",
            source_name="t.csv",
            source_sha256="f" * 64,
            archived_path=None,
        )
        repository.insert_events(
            stream,
            run,
            [
                event("device", "2026-06-06T09:00:00-07:00", values={"lat": 1.0}),
                event("deviceX", "2026-06-06T09:00:00-07:00", values={"lat": 2.0}),
            ],
        )
        # "device" must not also match "deviceX" -- the same rule
        # _prefix_clause already enforces for durations/counts.
        totals = repository.daily_field_totals_by_category(stream.id, path_prefix="device")
        assert [t.value_sum for t in totals] == [1.0]

    def test_field_id_isolates_one_field(self, repository: Repository) -> None:
        stream = repository.create_stream(
            "location",
            "Location",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="lat", label="Latitude", role=FieldRole.NUMBER, source_column="lat"),
                FieldSpec(key="lon", label="Longitude", role=FieldRole.NUMBER, source_column="lon"),
            ],
        )
        assert stream.id is not None
        run = repository.begin_import(
            stream.id,
            importer="test",
            source_name="t.csv",
            source_sha256="a" * 64,
            archived_path=None,
        )
        repository.insert_events(
            stream,
            run,
            [event("location", "2026-06-06T09:00:00-07:00", values={"lat": 1.0, "lon": 2.0})],
        )
        [lat_field, _lon_field] = repository.stream_fields(stream.id)

        lat_totals = repository.daily_field_totals_by_category(stream.id, field_id=lat_field.id)
        assert [t.field_key for t in lat_totals] == ["lat"]
        assert [t.value_sum for t in lat_totals] == [1.0]

    def test_empty_for_a_stream_with_no_numeric_fields(
        self, repository: Repository, stream: Stream
    ) -> None:
        # `stream` is an INTERVAL stream (the `timecard` fixture): it never
        # writes to field_rollup at all, so this must return nothing rather
        # than raise.
        assert stream.id is not None
        assert repository.daily_field_totals_by_category(stream.id) == []

    def test_agrees_with_a_rollups_rebuild_round_trip(
        self, repository: Repository, heart_rate_stream: Stream, event_run_id: int
    ) -> None:
        repository.insert_events(
            heart_rate_stream,
            event_run_id,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", values={"bpm": 60.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", values={"bpm": 70.0}),
            ],
        )
        assert heart_rate_stream.id is not None
        before = repository.daily_field_totals_by_category(heart_rate_stream.id)
        rollups.rebuild(repository.connection, heart_rate_stream.id)
        after = repository.daily_field_totals_by_category(heart_rate_stream.id)
        assert before == after
