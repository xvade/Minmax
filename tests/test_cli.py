"""Tests for the command-line interface.

The CLI is the only way to use the feature until the UI lands, so its exit
codes and failure messages are part of the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minmax.cli import format_duration, main


def run(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


@pytest.fixture
def imported(home: Path, sample_csv: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    assert run(home, "import", str(sample_csv)) == 0
    capsys.readouterr()
    return home


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (59, "59s"),
            (60, "1m"),
            (679, "11m"),
            (3600, "1h"),
            (3660, "1h 01m"),
            (28_800, "8h"),
            (32_400, "9h"),
        ],
    )
    def test_reads_the_way_a_person_would_say_it(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestImportCommand:
    def test_reports_what_it_stored(
        self, home: Path, sample_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "import", str(sample_csv)) == 0
        out = capsys.readouterr().out
        assert "4 new" in out
        assert "archived:" in out

    def test_a_second_run_reports_a_skip(
        self, imported: Path, sample_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "import", str(sample_csv)) == 0
        assert "skipped" in capsys.readouterr().out

    def test_a_missing_file_exits_nonzero(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "import", str(home / "nope.csv")) == 2
        assert "no such file" in capsys.readouterr().err

    def test_an_unknown_stream_exits_nonzero(
        self, home: Path, sample_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "import", str(sample_csv), "--stream", "fitbit") == 2
        assert "unknown stream" in capsys.readouterr().err

    def test_malformed_rows_are_surfaced_not_swallowed(
        self, home: Path, malformed_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "import", str(malformed_csv)) == 0
        captured = capsys.readouterr()
        assert "1 new" in captured.out
        assert "6 rejected" in captured.out
        assert "rejected line" in captured.err

    def test_an_overlap_aborts_with_a_message_and_stores_nothing(
        self, imported: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conflicting = tmp_path / "edited.csv"
        conflicting.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "ZZZZ,admin,2026-06-06T07:30:00-07:00,2026-06-06T08:30:00-07:00,3600\n",
            encoding="utf-8",
        )
        assert run(imported, "import", str(conflicting)) == 1
        assert "nothing was stored" in capsys.readouterr().err


class TestReadCommands:
    def test_status_summarizes_the_stream(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "status") == 0
        out = capsys.readouterr().out
        assert "timecard" in out
        assert "3 categories" in out
        assert "10h" in out  # 36,000 seconds tracked

    def test_status_on_an_empty_database_says_so(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "status") == 0
        assert "no streams yet" in capsys.readouterr().out

    def test_totals_lists_categories_with_shares(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "totals") == 0
        out = capsys.readouterr().out
        assert "sleep/night" in out
        assert "total" in out

    def test_totals_honours_a_prefix(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "totals", "--prefix", "media") == 0
        out = capsys.readouterr().out
        assert "media/gaming/chess" in out
        assert "sleep/night" not in out

    def test_day_marks_a_run_that_began_the_night_before(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "day", "2026-06-06") == 0
        out = capsys.readouterr().out
        assert "sleep/night" in out
        assert "<" in out, "a run starting before the shown day should be marked"

    def test_periods_reads_from_rollups(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "periods", "--period", "day") == 0
        out = capsys.readouterr().out
        assert "2026-06-05" in out
        assert "2026-06-06" in out

    def test_querying_an_unknown_stream_exits_nonzero(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "totals", "--stream", "fitbit") == 2
        assert "no stream" in capsys.readouterr().err

    def test_an_invalid_date_is_rejected_by_the_parser(self, imported: Path) -> None:
        with pytest.raises(SystemExit):
            run(imported, "day", "not-a-date")


class TestMaintenanceCommands:
    def test_revert_removes_an_import(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "revert", "1") == 0
        assert "4 observations removed" in capsys.readouterr().out

        assert run(imported, "totals") == 0
        assert "no observations" in capsys.readouterr().out

    def test_rebuild_rollups_leaves_totals_unchanged(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "periods") == 0
        before = capsys.readouterr().out

        assert run(imported, "rebuild-rollups") == 0
        capsys.readouterr()

        assert run(imported, "periods") == 0
        assert capsys.readouterr().out == before
