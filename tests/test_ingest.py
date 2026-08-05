"""Tests for the Timecard parser and the shared import pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import data_snapshot
from minmax.core.timeref import Instant, Period
from minmax.ingest import TimecardCsvImporter, run_import
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
