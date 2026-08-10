"""Tests for the Timecard parser and the shared import pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import data_snapshot
from minmax.core.models import FieldRole, FieldSpec, StreamKind
from minmax.core.timeref import Instant, Period
from minmax.ingest import (
    CsvStreamImporter,
    IntervalCsvImporter,
    TimecardCsvImporter,
    importer_for,
    run_import,
)
from minmax.ingest.archive import sha256_of
from minmax.storage.repository import OverlapError, Repository

HOUR = 3600


class TestTimecardParser:
    def test_parses_the_sample_export(self, sample_csv: Path) -> None:
        result = TimecardCsvImporter().parse(sample_csv)
        assert len(result.records) == 4
        assert result.rejections == []
        assert [r.category_path for r in result.records] == [
            "sleep/night",
            "social",
            "media/gaming/chess",
            "social/friends",
        ]

    def test_duration_is_derived_not_taken_from_the_file(self, sample_csv: Path) -> None:
        # The 'social' row reports 680s but its timestamps span 679s -- the
        # sub-second truncation present on 27% of the real export's rows.
        social = TimecardCsvImporter().parse(sample_csv).records[1]
        assert social.source_duration_seconds == 680
        assert social.duration_seconds == 679
        assert social.source_duration_drift == 1

    def test_drift_is_reported_rather_than_hidden(self, sample_csv: Path) -> None:
        notes = " ".join(TimecardCsvImporter().parse(sample_csv).notes)
        assert "derived from the timestamps" in notes
        assert "+1s x1" in notes

    def test_offsets_are_preserved_from_the_source(self, sample_csv: Path) -> None:
        first = TimecardCsvImporter().parse(sample_csv).records[0]
        assert first.start.utc_offset_seconds == -7 * HOUR
        assert first.start.local_date.isoformat() == "2026-06-05"
        assert first.end.local_date.isoformat() == "2026-06-06"

    def test_quoted_fields_containing_commas_survive(self, malformed_csv: Path) -> None:
        good = TimecardCsvImporter().parse(malformed_csv).records
        assert [r.category_path for r in good] == ["admin, general"]

    def test_each_bad_row_is_rejected_with_a_reason(self, malformed_csv: Path) -> None:
        # One malformed row must not cost the user the rest of the file.
        result = TimecardCsvImporter().parse(malformed_csv)
        assert len(result.records) == 1
        reasons = [rejection.reason for rejection in result.rejections]
        assert len(reasons) == 6
        assert any("timer_name is empty" in r for r in reasons)
        assert any("no UTC offset" in r for r in reasons)
        assert any("before started_at" in r for r in reasons)
        assert any("truncation tolerance" in r for r in reasons)
        assert any("not an ISO 8601 timestamp" in r for r in reasons)
        assert any("not an integer" in r for r in reasons)

    def test_rejections_carry_the_source_line(self, malformed_csv: Path) -> None:
        result = TimecardCsvImporter().parse(malformed_csv)
        assert [rejection.line for rejection in result.rejections] == [3, 4, 5, 6, 7, 8]

    def test_a_file_that_is_not_a_timecard_export_fails_loudly(self, tmp_path: Path) -> None:
        # Silently importing zero rows from the wrong file would look like
        # success. It must not.
        wrong = tmp_path / "wrong.csv"
        wrong.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a Timecard export"):
            TimecardCsvImporter().parse(wrong)

    def test_a_byte_order_mark_does_not_break_the_header(
        self, tmp_path: Path, sample_csv: Path
    ) -> None:
        with_bom = tmp_path / "bom.csv"
        with_bom.write_bytes(b"\xef\xbb\xbf" + sample_csv.read_bytes())
        assert len(TimecardCsvImporter().parse(with_bom).records) == 4

    def test_an_export_with_only_a_header_yields_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n", encoding="utf-8"
        )
        result = TimecardCsvImporter().parse(empty)
        assert result.records == [] and result.rejections == []


class TestImportPipeline:
    def test_a_first_import_stores_everything(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        report = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        assert (report.inserted, report.duplicates, report.rejected) == (4, 0, 0)
        assert not report.skipped

    def test_the_raw_file_is_archived_byte_for_byte(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        # If the parser turns out to be wrong, the fix must be a re-import --
        # which is only possible if the original bytes survived.
        report = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        assert report.archived_path is not None
        assert report.archived_path.read_bytes() == sample_csv.read_bytes()
        assert sha256_of(report.archived_path) == report.source_sha256

    def test_reimporting_the_same_file_is_skipped(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        second = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        assert second.skipped
        assert second.inserted == 0

    def test_a_forced_reimport_changes_no_data(
        self, repository: Repository, connection: sqlite3.Connection, home: Path, sample_csv: Path
    ) -> None:
        # The idempotency invariant, tested where it actually matters: bypass
        # the content-hash shortcut and confirm the data itself is untouched.
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        before = data_snapshot(connection)

        forced = run_import(repository, TimecardCsvImporter(), sample_csv, home=home, force=True)
        assert (forced.inserted, forced.duplicates) == (0, 4)
        assert data_snapshot(connection) == before

    def test_a_renamed_copy_of_the_same_content_is_still_recognized(
        self, repository: Repository, home: Path, sample_csv: Path, tmp_path: Path
    ) -> None:
        # Identity is the content hash, not the filename.
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        copy = tmp_path / "Timecard-different-name.csv"
        copy.write_bytes(sample_csv.read_bytes())
        assert run_import(repository, TimecardCsvImporter(), copy, home=home).skipped

    def test_provenance_is_recorded(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        report = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None

        runs = repository.list_imports(stream.id)
        assert len(runs) == 1
        assert runs[0].completed
        assert runs[0].source_sha256 == report.source_sha256
        assert runs[0].rows_inserted == 4
        assert runs[0].importer == "timecard-csv"

    def test_every_stored_row_points_at_its_import(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        report = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        stored = repository.intervals_in_range(stream.id, 0, 2**31)
        assert {item.import_run_id for item in stored} == {report.import_run_id}

    def test_reverting_an_import_undoes_it_completely(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        report = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        assert report.import_run_id is not None
        assert repository.revert_import(report.import_run_id) == 4

        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        assert repository.intervals_in_range(stream.id, 0, 2**31) == []
        assert repository.totals_by_period(stream.id, Period.DAY) == []

    def test_the_stream_is_created_with_the_importer_s_properties(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None
        assert stream.exclusive is True
        assert stream.kind.value == "interval"

    def test_a_failed_import_stores_nothing_and_is_marked_failed(
        self, repository: Repository, home: Path, sample_csv: Path, tmp_path: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)

        # A second export where a run's times were edited, so it overlaps a
        # stored run without duplicating it.
        conflicting = tmp_path / "edited.csv"
        conflicting.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "ZZZZ,admin,2026-06-06T07:30:00-07:00,2026-06-06T08:30:00-07:00,3600\n",
            encoding="utf-8",
        )
        with pytest.raises(OverlapError):
            run_import(repository, TimecardCsvImporter(), conflicting, home=home)

        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        assert len(repository.intervals_in_range(stream.id, 0, 2**31)) == 4

        failed = repository.list_imports(stream.id)[-1]
        assert not failed.completed
        assert failed.notes is not None and failed.notes.startswith("failed:")

    def test_a_failed_import_can_be_retried_after_the_conflict_is_cleared(
        self, repository: Repository, home: Path, sample_csv: Path, tmp_path: Path
    ) -> None:
        # An unfinished run must not make a retry look already-done.
        first = run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        conflicting = tmp_path / "edited.csv"
        conflicting.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "ZZZZ,admin,2026-06-06T07:30:00-07:00,2026-06-06T08:30:00-07:00,3600\n",
            encoding="utf-8",
        )
        with pytest.raises(OverlapError):
            run_import(repository, TimecardCsvImporter(), conflicting, home=home)

        assert first.import_run_id is not None
        repository.revert_import(first.import_run_id)
        retried = run_import(repository, TimecardCsvImporter(), conflicting, home=home)
        assert retried.inserted == 1


class TestEndToEndTotals:
    """The numbers a user would actually read, checked by hand.

    Sample export: sleep 23:00-07:00 crossing midnight, social 07:00-07:11:19,
    chess 07:11:19-08:00, social/friends 08:00-09:00.
    """

    @pytest.fixture(autouse=True)
    def imported(self, repository: Repository, home: Path, sample_csv: Path) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)

    def test_category_totals_group_a_renamed_timer_together(self, repository: Repository) -> None:
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        totals = {
            t.category_path: t.seconds for t in repository.totals_by_category(stream.id, 0, 2**31)
        }
        # 'social' (679s) and 'social/friends' (3600s) are one timer.
        assert totals == {
            "sleep/night": 8 * HOUR,
            "social/friends": 679 + 3600,
            "media/gaming/chess": 2921,
        }
        assert sum(totals.values()) == 36_000

    def test_daily_rollups_split_the_overnight_run(self, repository: Repository) -> None:
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        totals = {t.bucket: t.seconds for t in repository.totals_by_period(stream.id, Period.DAY)}
        assert totals == {"2026-06-05": HOUR, "2026-06-06": 32_400}

    def test_a_prefix_query_covers_the_whole_subtree(self, repository: Repository) -> None:
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        media = repository.totals_by_category(stream.id, 0, 2**31, path_prefix="media")
        assert sum(t.seconds for t in media) == 2921

    def test_a_single_local_day_query_clips_correctly(self, repository: Repository) -> None:
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        start = Instant.from_iso("2026-06-06T00:00:00-07:00").epoch_seconds
        end = Instant.from_iso("2026-06-07T00:00:00-07:00").epoch_seconds
        totals = {
            t.category_path: t.seconds for t in repository.totals_by_category(stream.id, start, end)
        }
        assert totals["sleep/night"] == 7 * HOUR
        assert sum(totals.values()) == 32_400


class TestCsvStreamImporter:
    """The generic, config-driven importer for user-declared point-in-time streams."""

    def test_a_bare_instant_only_stream_parses_with_the_implicit_category(
        self, repository: Repository, pickups_csv: Path
    ) -> None:
        stream = repository.create_stream(
            "pickups",
            "Pickups",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="occurred_at")
            ],
        )
        assert stream.id is not None
        importer = CsvStreamImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(pickups_csv)
        assert len(result.records) == 3
        assert result.rejections == []
        assert all(r.category_path == "pickups" and not r.values for r in result.records)

    def test_a_numeric_field_is_read_and_bad_cells_are_rejected(
        self, repository: Repository, heart_rate_csv: Path
    ) -> None:
        stream = repository.create_stream(
            "heart_rate",
            "Heart Rate",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="measured_at"),
                FieldSpec(
                    key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
                ),
            ],
        )
        assert stream.id is not None
        importer = CsvStreamImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(heart_rate_csv)
        assert [r.values["bpm"] for r in result.records] == [58.0, 62.0, 71.0]
        assert len(result.rejections) == 1
        assert "not a number" in result.rejections[0].reason

    def test_category_source_id_and_optional_text_round_trip(
        self, repository: Repository, location_csv: Path
    ) -> None:
        stream = repository.create_stream(
            "location",
            "Location",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="captured_at"),
                FieldSpec(
                    key="device", label="Device", role=FieldRole.CATEGORY, source_column="device"
                ),
                FieldSpec(
                    key="fix", label="Fix id", role=FieldRole.SOURCE_ID, source_column="fix_id"
                ),
                FieldSpec(key="lat", label="Latitude", role=FieldRole.NUMBER, source_column="lat"),
                FieldSpec(key="lon", label="Longitude", role=FieldRole.NUMBER, source_column="lon"),
                FieldSpec(key="note", label="Note", role=FieldRole.TEXT, source_column="note"),
            ],
        )
        assert stream.id is not None
        importer = CsvStreamImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(location_csv)
        assert [r.category_path for r in result.records] == ["phone", "watch"]
        assert [r.category_source_id for r in result.records] == ["F1", "F2"]
        assert result.records[0].values == {"lat": 47.6062, "lon": -122.3321, "note": "home"}
        # The second row's note was blank -- omitted, not stored as "".
        assert "note" not in result.records[1].values

    def test_a_missing_declared_column_rejects_the_whole_file(
        self, repository: Repository, tmp_path: Path
    ) -> None:
        stream = repository.create_stream(
            "heart_rate",
            "Heart Rate",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="measured_at"),
                FieldSpec(key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm"),
            ],
        )
        wrong = tmp_path / "wrong.csv"
        wrong.write_text("timestamp,value\n2026-06-06T08:00:00-07:00,58\n", encoding="utf-8")
        assert stream.id is not None
        importer = CsvStreamImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        with pytest.raises(ValueError, match="missing"):
            importer.parse(wrong)

    def test_the_generic_path_agrees_with_timecard_on_the_same_shape_of_data(
        self, repository: Repository, sample_csv: Path
    ) -> None:
        # Not a golden-file test of Timecard's own quirks (duration-drift
        # tolerance stays hand-written) -- just proof the generic parser reads
        # a Timecard-shaped file the same way for the fields both share.
        stream = repository.create_stream(
            "focused_app",
            "Focused App",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="started_at"),
                FieldSpec(
                    key="app", label="App", role=FieldRole.CATEGORY, source_column="timer_name"
                ),
                FieldSpec(key="id", label="Id", role=FieldRole.SOURCE_ID, source_column="timer_id"),
            ],
        )
        assert stream.id is not None
        importer = CsvStreamImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        generic = importer.parse(sample_csv)
        timecard = TimecardCsvImporter().parse(sample_csv)
        assert [r.category_path for r in generic.records] == [
            r.category_path for r in timecard.records
        ]
        assert [r.at.epoch_seconds for r in generic.records] == [
            r.start.epoch_seconds for r in timecard.records
        ]

    def test_import_twice_leaves_the_database_byte_identical(
        self, repository: Repository, home: Path, heart_rate_csv: Path
    ) -> None:
        repository.create_stream(
            "heart_rate",
            "Heart Rate",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="measured_at"),
                FieldSpec(
                    key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
                ),
            ],
        )
        run_import(repository, importer_for(repository, "heart_rate"), heart_rate_csv, home=home)
        before = data_snapshot(repository.connection)
        run_import(
            repository,
            importer_for(repository, "heart_rate"),
            heart_rate_csv,
            home=home,
            force=True,
        )
        assert data_snapshot(repository.connection) == before


class TestIntervalCsvImporter:
    """The generic, config-driven importer for user-declared interval streams."""

    def _fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                key="start", label="Start", role=FieldRole.START, source_column="session_start"
            ),
            FieldSpec(key="end", label="End", role=FieldRole.END, source_column="session_end"),
            FieldSpec(
                key="project", label="Project", role=FieldRole.CATEGORY, source_column="project"
            ),
            FieldSpec(key="note", label="Note", role=FieldRole.IGNORED, source_column="note"),
        ]

    def test_the_happy_rows_parse_with_derived_durations(
        self, repository: Repository, sessions_csv: Path
    ) -> None:
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, fields=self._fields()
        )
        assert stream.id is not None
        importer = IntervalCsvImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(sessions_csv)
        assert [r.category_path for r in result.records] == ["work", "break"]
        assert [r.duration_seconds for r in result.records] == [3600, 2700]
        assert [r.source_duration_seconds for r in result.records] == [None, None]

    def test_an_inverted_row_is_rejected_not_fatal(
        self, repository: Repository, sessions_csv: Path
    ) -> None:
        # IntervalRecord.__post_init__ raises a bare ValueError for an
        # inverted span, which parse() does not catch -- the importer must
        # pre-validate so one bad row becomes a Rejection, not an aborted
        # import (see IntervalCsvImporter._record_from_row).
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, fields=self._fields()
        )
        assert stream.id is not None
        importer = IntervalCsvImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(sessions_csv)
        reasons = [r.reason for r in result.rejections]
        assert any("before" in reason for reason in reasons)
        assert any("empty" in reason for reason in reasons)
        assert len(result.records) == 2

    def test_an_implicit_category_is_used_when_none_is_declared(
        self, repository: Repository, sessions_csv: Path
    ) -> None:
        fields = [f for f in self._fields() if f.role is not FieldRole.CATEGORY]
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, fields=fields
        )
        assert stream.id is not None
        importer = IntervalCsvImporter(stream.key, stream.name, repository.stream_fields(stream.id))
        result = importer.parse(sessions_csv)
        assert all(r.category_path == "sessions" for r in result.records)

    def test_exclusive_reflects_the_stream_flag(self, repository: Repository) -> None:
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, exclusive=True, fields=self._fields()
        )
        assert stream.id is not None
        importer = IntervalCsvImporter(
            stream.key, stream.name, repository.stream_fields(stream.id), exclusive=stream.exclusive
        )
        assert importer.stream_exclusive is True

    def test_missing_start_or_end_role_is_rejected_at_construction(
        self, repository: Repository
    ) -> None:
        fields = [f for f in self._fields() if f.role is not FieldRole.END]
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, fields=fields
        )
        assert stream.id is not None
        with pytest.raises(ValueError, match="role 'end'"):
            IntervalCsvImporter(stream.key, stream.name, repository.stream_fields(stream.id))

    def test_a_number_role_is_rejected_at_construction(self, repository: Repository) -> None:
        fields = [
            *self._fields(),
            FieldSpec(key="cost", label="Cost", role=FieldRole.NUMBER, source_column="cost"),
        ]
        stream = repository.create_stream(
            "sessions", "Sessions", StreamKind.INTERVAL, fields=fields
        )
        assert stream.id is not None
        with pytest.raises(ValueError, match="cannot store value columns"):
            IntervalCsvImporter(stream.key, stream.name, repository.stream_fields(stream.id))


class TestImporterFor:
    def test_a_hardcoded_stream_resolves_without_being_created_first(
        self, repository: Repository
    ) -> None:
        assert isinstance(importer_for(repository, "timecard"), TimecardCsvImporter)

    def test_an_undeclared_stream_raises(self, repository: Repository) -> None:
        with pytest.raises(LookupError, match="create it first"):
            importer_for(repository, "nonexistent")

    def test_a_sample_stream_is_refused_by_the_generic_path(self, repository: Repository) -> None:
        repository.ensure_stream("cpu", "CPU load", StreamKind.SAMPLE)
        with pytest.raises(ValueError, match="point-in-time and interval"):
            importer_for(repository, "cpu")

    def test_a_declared_event_stream_resolves_to_the_generic_importer(
        self, repository: Repository
    ) -> None:
        repository.create_stream(
            "pickups",
            "Pickups",
            StreamKind.EVENT,
            fields=[
                FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="occurred_at")
            ],
        )
        assert isinstance(importer_for(repository, "pickups"), CsvStreamImporter)

    def test_a_declared_interval_stream_resolves_to_the_generic_importer(
        self, repository: Repository
    ) -> None:
        repository.create_stream(
            "sessions",
            "Sessions",
            StreamKind.INTERVAL,
            fields=[
                FieldSpec(key="start", label="Start", role=FieldRole.START, source_column="start"),
                FieldSpec(key="end", label="End", role=FieldRole.END, source_column="end"),
            ],
        )
        assert isinstance(importer_for(repository, "sessions"), IntervalCsvImporter)

    def test_an_interval_stream_with_no_declared_fields_fails_to_build_an_importer(
        self, repository: Repository
    ) -> None:
        # ensure_stream (the auto-create path run_import uses) never declares
        # fields, so a hand-created interval stream with none is exactly the
        # same shape as one nobody ran `stream add` for.
        repository.ensure_stream("calendar", "Calendar", StreamKind.INTERVAL)
        with pytest.raises(ValueError, match="role 'start'"):
            importer_for(repository, "calendar")
