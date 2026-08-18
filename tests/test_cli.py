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

    def test_stream_add_kind_interval_end_to_end(
        self, home: Path, sessions_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "sessions",
                "--name",
                "Sessions",
                "--kind",
                "interval",
                "--from",
                str(sessions_csv),
                "--role",
                "project=category",
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "created stream 'sessions'" in out
        assert "session_start        -> start" in out
        assert "session_end          -> end" in out
        assert "project              -> category" in out
        assert "note                 -> ignored" in out

        assert run(home, "streams") == 0
        listed = capsys.readouterr().out
        assert "sessions  (interval)" in listed
        # streams' listing skips structural roles (start/end), same as 'at'
        # for an event stream.
        assert "session_start" not in listed
        assert "project" in listed

        assert run(home, "import", str(sessions_csv), "--stream", "sessions") == 0
        out = capsys.readouterr().out
        assert "2 new" in out
        assert "2 rejected" in out

        assert run(home, "day", "--stream", "sessions", "2026-06-06") == 0
        out = capsys.readouterr().out
        assert "work" in out and "break" in out

    def test_stream_add_requires_exactly_one_start_and_end_column(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        no_time = tmp_path / "no_time.csv"
        no_time.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "odd",
                "--name",
                "Odd",
                "--kind",
                "interval",
                "--from",
                str(no_time),
            )
            == 2
        )
        assert "exactly one column must have role 'start'" in capsys.readouterr().err

    def test_stream_add_rejects_a_value_column_on_an_interval_stream(
        self, home: Path, sessions_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "sessions",
                "--name",
                "Sessions",
                "--kind",
                "interval",
                "--from",
                str(sessions_csv),
                "--role",
                "note=number",
            )
            == 2
        )
        assert "cannot store value columns" in capsys.readouterr().err

    def test_exclusive_flag_rejects_overlapping_spans_default_does_not(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        overlapping = tmp_path / "overlapping.csv"
        overlapping.write_text(
            "start,end\n"
            "2026-06-06T08:00:00-07:00,2026-06-06T09:00:00-07:00\n"
            "2026-06-06T08:30:00-07:00,2026-06-06T10:00:00-07:00\n",
            encoding="utf-8",
        )

        run(
            home,
            "stream",
            "add",
            "--key",
            "calendar",
            "--name",
            "Calendar",
            "--kind",
            "interval",
            "--exclusive",
            "--from",
            str(overlapping),
        )
        capsys.readouterr()
        assert run(home, "streams") == 0
        assert "calendar  (interval, exclusive)" in capsys.readouterr().out

        assert run(home, "import", str(overlapping), "--stream", "calendar") == 1
        assert "overlap" in capsys.readouterr().err.lower()

    def test_a_non_exclusive_interval_stream_accepts_overlapping_spans(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        overlapping = tmp_path / "overlapping.csv"
        overlapping.write_text(
            "start,end\n"
            "2026-06-06T08:00:00-07:00,2026-06-06T09:00:00-07:00\n"
            "2026-06-06T08:30:00-07:00,2026-06-06T10:00:00-07:00\n",
            encoding="utf-8",
        )
        run(
            home,
            "stream",
            "add",
            "--key",
            "calendar",
            "--name",
            "Calendar",
            "--kind",
            "interval",
            "--from",
            str(overlapping),
        )
        capsys.readouterr()

        assert run(home, "import", str(overlapping), "--stream", "calendar") == 0
        assert "2 new" in capsys.readouterr().out


class TestStreamDeletion:
    def test_deleting_a_stream_removes_it_and_its_data(
        self, home: Path, heart_rate_csv: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
        run(home, "import", str(heart_rate_csv), "--stream", "heart_rate")
        capsys.readouterr()

        assert run(home, "stream", "delete", "--key", "heart_rate") == 0
        out = capsys.readouterr().out
        assert "deleted stream 'heart_rate' (event)" in out

        assert run(home, "streams") == 0
        assert "no streams yet" in capsys.readouterr().out

    def test_deleting_an_unknown_key_fails_clearly(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(home, "stream", "delete", "--key", "nonexistent") == 2
        assert "no stream" in capsys.readouterr().err


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

    @pytest.fixture
    def numeric_field_stream(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> Path:
        """A point-in-time stream with one numeric field -- enough on its own
        for a sweep to have a count, a mean, and (when included) a sum
        measure to count and pair against each other."""
        csv_path = tmp_path / "readings.csv"
        lines = ["measured_at,value"]
        for i in range(20):
            day = date(2026, 3, 2) + timedelta(days=i)
            lines.append(f"{day.isoformat()}T08:00:00-07:00,{10 + i}")
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert (
            run(
                home,
                "stream",
                "add",
                "--key",
                "readings",
                "--name",
                "Readings",
                "--from",
                str(csv_path),
            )
            == 0
        )
        assert run(home, "import", str(csv_path), "--stream", "readings") == 0
        capsys.readouterr()
        return home

    def test_field_sum_is_a_default_candidate(
        self, numeric_field_stream: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A single numeric-field stream already has three measures to count
        # against each other: its count, its field's mean, and (by default
        # now) its field's sum.
        import json

        assert run(numeric_field_stream, "correlate", "--json", "--min-r", "0.0") == 0
        assert json.loads(capsys.readouterr().out)["series_count"] == 3

    def test_no_field_sum_drops_it(
        self, numeric_field_stream: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        assert (
            run(numeric_field_stream, "correlate", "--json", "--min-r", "0.0", "--no-field-sum")
            == 0
        )
        assert json.loads(capsys.readouterr().out)["series_count"] == 2


class TestViewCommands:
    """``imported`` (via ``sample_csv``) is one interval stream, ``timecard``,
    with ``sleep/night`` running 2026-06-05 23:00-07:00 -> 2026-06-06 07:00-07:00
    and three more runs after it on 2026-06-06 -- see ``timecard_sample.csv``."""

    def test_view_add_creates_a_view(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(
                imported,
                "view",
                "add",
                "--key",
                "sleep-view",
                "--name",
                "Sleep",
                "--stream",
                "timecard",
                "--category",
                "sleep",
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "sleep-view" in out
        assert "timecard/sleep" in out

    def test_view_add_with_no_category_means_any_activity(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run(imported, "view", "add", "--key", "all", "--name", "All", "--stream", "timecard")
            == 0
        )
        assert "any activity" in capsys.readouterr().out

    def test_view_add_on_an_unknown_stream_fails_clearly(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "view", "add", "--key", "x", "--name", "X", "--stream", "fitbit") == 2
        assert "no stream" in capsys.readouterr().err

    def test_view_add_a_duplicate_key_fails_clearly(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "sleep-view",
            "--name",
            "Sleep",
            "--stream",
            "timecard",
        )
        capsys.readouterr()
        assert (
            run(
                imported,
                "view",
                "add",
                "--key",
                "sleep-view",
                "--name",
                "Again",
                "--stream",
                "timecard",
            )
            == 2
        )
        assert "already exists" in capsys.readouterr().err

    def test_view_list_shows_every_saved_view(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "sleep-view",
            "--name",
            "Sleep",
            "--stream",
            "timecard",
        )
        capsys.readouterr()
        assert run(imported, "view", "list") == 0
        assert "sleep-view" in capsys.readouterr().out

    def test_view_list_on_an_empty_database_says_so(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "view", "list") == 0
        assert "no views yet" in capsys.readouterr().out

    def test_view_delete_removes_it(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "sleep-view",
            "--name",
            "Sleep",
            "--stream",
            "timecard",
        )
        capsys.readouterr()
        assert run(imported, "view", "delete", "--key", "sleep-view") == 0
        assert "deleted view 'sleep-view'" in capsys.readouterr().out
        run(imported, "view", "list")
        assert "no views yet" in capsys.readouterr().out

    def test_view_delete_an_unknown_key_fails_clearly(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "view", "delete", "--key", "nonexistent") == 2
        assert "no view" in capsys.readouterr().err

    def test_totals_narrows_under_a_view(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "media-view",
            "--name",
            "Media",
            "--stream",
            "timecard",
            "--category",
            "media",
        )
        capsys.readouterr()

        assert run(imported, "totals", "--stream", "timecard") == 0
        unmasked = capsys.readouterr().out

        assert run(imported, "totals", "--stream", "timecard", "--view", "media-view") == 0
        masked = capsys.readouterr().out

        assert "view media-view" in masked
        assert "sleep/night" not in masked
        assert "media/gaming/chess" in masked
        assert "sleep/night" in unmasked

    def test_totals_with_an_unknown_view_fails_clearly(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(imported, "totals", "--stream", "timecard", "--view", "nonexistent") == 2
        assert "no view" in capsys.readouterr().err

    def test_day_splits_a_record_straddling_the_masks_edge(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # sleep/night runs 23:00 (6/5) -> 07:00 (6/6); a view over just the
        # first half of that hour range clips it rather than dropping it.
        run(
            imported,
            "view",
            "add",
            "--key",
            "morning",
            "--name",
            "Morning",
            "--stream",
            "timecard",
            "--category",
            "sleep",
        )
        capsys.readouterr()
        assert run(imported, "day", "2026-06-06", "--stream", "timecard", "--view", "morning") == 0
        out = capsys.readouterr().out
        assert "sleep/night" in out
        assert "view morning" in out

    def test_periods_narrows_under_a_view(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "media-view",
            "--name",
            "Media",
            "--stream",
            "timecard",
            "--category",
            "media",
        )
        capsys.readouterr()
        assert run(imported, "periods", "--stream", "timecard", "--view", "media-view") == 0
        out = capsys.readouterr().out
        assert "2026-06-06" in out

    def test_periods_hour_with_a_view_is_refused(
        self, imported: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            imported,
            "view",
            "add",
            "--key",
            "media-view",
            "--name",
            "Media",
            "--stream",
            "timecard",
            "--category",
            "media",
        )
        capsys.readouterr()
        assert (
            run(
                imported,
                "periods",
                "--stream",
                "timecard",
                "--period",
                "hour",
                "--view",
                "media-view",
            )
            == 2
        )
        assert "only supports --period day" in capsys.readouterr().err
