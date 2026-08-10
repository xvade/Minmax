"""Tests for the command-line interface.

The CLI is the only way to use the feature until the UI lands, so its exit
codes and failure messages are part of the contract.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dtime
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
        assert "no stream 'fitbit'" in capsys.readouterr().err

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


class TestStreamCommands:
    def test_stream_add_creates_a_stream_from_a_sniffed_header(
        self, home: Path, heart_rate_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "heart_rate",
                "--name",
                "Heart Rate",
                "--from",
                str(heart_rate_csv),
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "created stream 'heart_rate'" in out
        assert "measured_at" in out and "-> at" in out
        # The fixture's bpm column includes one deliberately bad value (see
        # tests/fixtures/heart_rate.csv), so the sniff conservatively guesses
        # text rather than number -- a wrong "number" guess would silently
        # reject every row with a non-numeric cell instead of flagging the
        # mapping as needing a --role override.
        assert "bpm" in out and "-> text" in out

        assert run(home, "streams") == 0
        listed = capsys.readouterr().out
        assert "heart_rate  (event)" in listed
        assert "text" in listed

    def test_role_overrides_win_over_the_guessed_role(
        self, home: Path, location_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Unaided, 'device' and 'fix_id' both guess as text -- nothing about
        # their values says "category" or "source id".
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "location",
                "--name",
                "Location",
                "--from",
                str(location_csv),
                "--role",
                "device=category",
                "--role",
                "fix_id=source_id",
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "device               -> category" in out
        assert "fix_id               -> source_id" in out

    def test_stream_add_rejects_a_duplicate_key(
        self, home: Path, heart_rate_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = (
            "stream",
            "add",
            "--key",
            "heart_rate",
            "--name",
            "Heart Rate",
            "--from",
            str(heart_rate_csv),
        )
        assert run(home, *args) == 0
        capsys.readouterr()
        assert run(home, *args) == 2
        assert "already exists" in capsys.readouterr().err

    def test_stream_add_rejects_an_unknown_role(
        self, home: Path, heart_rate_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "heart_rate",
                "--name",
                "Heart Rate",
                "--from",
                str(heart_rate_csv),
                "--role",
                "bpm=bogus",
            )
            == 2
        )
        assert "unknown role" in capsys.readouterr().err

    def test_stream_add_requires_exactly_one_time_column(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        no_time = tmp_path / "no_time.csv"
        no_time.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        assert (
            run(home, "stream", "add", "--key", "odd", "--name", "Odd", "--from", str(no_time)) == 2
        )
        assert "exactly one column must have role 'at'" in capsys.readouterr().err

    def test_importing_into_a_declared_stream_and_reading_it_back(
        self, home: Path, pickups_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "pickups",
                "--name",
                "Pickups",
                "--from",
                str(pickups_csv),
            )
            == 0
        )
        capsys.readouterr()

        assert run(home, "import", str(pickups_csv), "--stream", "pickups") == 0
        out = capsys.readouterr().out
        assert "3 new" in out

        # This is the case that would previously divide by zero: an event
        # stream's rollup rows carry seconds=0.
        assert run(home, "periods", "--stream", "pickups", "--period", "day") == 0
        assert "2026-06-06" in capsys.readouterr().out

    def test_totals_and_day_refuse_a_non_interval_stream(
        self, home: Path, pickups_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            home,
            "stream",
            "add",
            "--key",
            "pickups",
            "--name",
            "Pickups",
            "--from",
            str(pickups_csv),
        )
        run(home, "import", str(pickups_csv), "--stream", "pickups")
        capsys.readouterr()

        assert run(home, "totals", "--stream", "pickups") == 2
        assert "try 'periods'" in capsys.readouterr().err

        assert run(home, "day", "--stream", "pickups", "2026-06-06") == 2
        assert "only supports interval streams" in capsys.readouterr().err


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


class TestCorrelateCommand:
    """The statistics are already pinned down in ``tests/test_analysis_*.py``;
    this checks the command's own responsibilities -- exit codes, the scipy
    install hint, and that ``--json`` actually parses.
    """

    @pytest.fixture
    def two_correlated_streams(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> Path:
        """A 20-day interval stream and a point-in-time stream whose daily
        count is an exact linear function of the same day index -- enough to
        clear every gate at daily granularity, deterministically."""
        interval_csv = tmp_path / "focus.csv"
        event_csv = tmp_path / "pickups.csv"
        start = date(2026, 3, 2)

        interval_lines = ["timer_id,timer_name,started_at,stopped_at,duration_seconds"]
        event_lines = ["id,at"]
        pickup_id = 0
        for i in range(20):
            day = start + timedelta(days=i)
            duration = 600 + 60 * i
            end = datetime.combine(day, dtime(9, 0)) + timedelta(seconds=duration)
            interval_lines.append(
                f"work,work,{day.isoformat()}T09:00:00-07:00,"
                f"{end.strftime('%Y-%m-%dT%H:%M:%S')}-07:00,{duration}"
            )
            for minute in range(2 + i):  # linear in i, same slope sign as duration
                at = datetime.combine(day, dtime(8, 0)) + timedelta(minutes=minute)
                event_lines.append(f"{pickup_id},{at.strftime('%Y-%m-%dT%H:%M:%S')}-07:00")
                pickup_id += 1

        interval_csv.write_text("\n".join(interval_lines) + "\n", encoding="utf-8")
        event_csv.write_text("\n".join(event_lines) + "\n", encoding="utf-8")

        assert run(home, "import", str(interval_csv), "--stream", "timecard") == 0
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "pickups",
                "--name",
                "Pickups",
                "--from",
                str(event_csv),
            )
            == 0
        )
        assert run(home, "import", str(event_csv), "--stream", "pickups") == 0
        capsys.readouterr()
        return home

    def test_reports_a_correlated_pair(
        self, two_correlated_streams: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(two_correlated_streams, "correlate", "--min-r", "0.5", "--min-periods", "5") == 0
        out = capsys.readouterr().out
        assert "timecard" in out
        assert "pickups" in out
        assert "leads to test, not findings" in out

    def test_an_empty_database_reports_nothing_cleared_rather_than_erroring(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "correlate") == 0
        assert "nothing cleared" in capsys.readouterr().out

    def test_an_unknown_stream_exits_nonzero(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "correlate", "--stream", "fitbit") == 2
        assert "no stream 'fitbit'" in capsys.readouterr().err

    def test_json_output_parses_and_carries_the_row_count(
        self, two_correlated_streams: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        assert (
            run(
                two_correlated_streams,
                "correlate",
                "--json",
                "--min-r",
                "0.5",
                "--min-periods",
                "5",
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["tests_performed"] > 0
        assert len(payload["rows"]) >= 1
        assert payload["rows"][0]["r"]

    def test_min_r_excludes_everything_when_set_above_one(
        self, two_correlated_streams: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # |r| can never exceed 1, so this is a robust way to assert the
        # filter actually runs, regardless of how correlated the fixture is.
        assert run(two_correlated_streams, "correlate", "--min-r", "2.0", "--min-periods", "5") == 0
        assert "nothing cleared" in capsys.readouterr().out

    def test_top_limits_how_many_pairs_are_shown(
        self, two_correlated_streams: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        assert (
            run(
                two_correlated_streams,
                "correlate",
                "--json",
                "--min-r",
                "0.0",
                "--min-periods",
                "5",
                "--top",
                "1",
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["rows"]) == 1

    def test_an_invalid_interval_is_a_usage_error(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            run(home, "correlate", "--interval", "fortnight")
        assert "unknown interval unit" in capsys.readouterr().err

    def test_missing_scipy_prints_an_install_hint(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "scipy":
                raise ImportError("no module named scipy")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        assert run(home, "correlate") == 2
        err = capsys.readouterr().err
        assert "scipy" in err
        assert "uv sync --extra analysis" in err
