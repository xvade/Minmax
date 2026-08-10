"""Tests for the client facade.

This is also the contract a future ``HttpClient`` must satisfy, so the tests are
written against the :class:`MinmaxClient` protocol rather than against
``InProcessClient``'s internals.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from minmax.api import CsvPreview, DayView, InProcessClient, MinmaxClient
from minmax.core import palette
from minmax.core.models import EventRecord, FieldRole, FieldSpec, IntervalRecord, StreamKind
from minmax.core.timeref import Instant
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository

PACIFIC = ZoneInfo("America/Los_Angeles")
HOUR = 3600


@pytest.fixture
def event_client(repository: Repository, home: Path) -> MinmaxClient:
    """A point-in-time stream (heart rate, one numeric field) with three
    readings spanning two local days -- enough to exercise day, window,
    range, and trend views the way the interval ``client`` fixture does."""
    stream = repository.create_stream(
        "heart_rate",
        "Heart Rate",
        StreamKind.EVENT,
        fields=[
            FieldSpec(
                key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
            )
        ],
    )
    assert stream.id is not None
    run = repository.begin_import(
        stream.id, importer="test", source_name="hr.csv", source_sha256="e" * 64, archived_path=None
    )
    repository.insert_events(
        stream,
        run,
        [
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-06-06T08:00:00-07:00"), {"bpm": 60.0}
            ),
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-06-06T20:00:00-07:00"), {"bpm": 75.0}
            ),
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-06-07T08:00:00-07:00"), {"bpm": 65.0}
            ),
        ],
    )
    return InProcessClient(repository, timezone=PACIFIC, home=home)


@pytest.fixture
def client(repository: Repository, home: Path, sample_csv: Path) -> MinmaxClient:
    run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
    return InProcessClient(repository, timezone=PACIFIC)


class TestProtocolConformance:
    def test_the_in_process_client_satisfies_the_protocol(self, client: MinmaxClient) -> None:
        assert isinstance(client, MinmaxClient)

    def test_nothing_from_storage_crosses_the_boundary(self, client: MinmaxClient) -> None:
        # Everything returned must survive serialization to a future HTTP
        # client, so no live rows, cursors, or connections may leak out.
        view = client.day("timecard", date(2026, 6, 6))
        for record in view.records:
            for value in vars(record).values() if hasattr(record, "__dict__") else []:
                assert isinstance(value, str | int | float | bool | type(None))


class TestStreams:
    def test_summarizes_what_is_stored(self, client: MinmaxClient) -> None:
        [stream] = client.streams()
        assert stream.key == "timecard"
        assert stream.kind == "interval"
        assert stream.exclusive
        assert stream.record_count == 4
        assert stream.tracked_seconds == 36_000
        assert stream.first_day == date(2026, 6, 5)
        assert stream.last_day == date(2026, 6, 6)

    def test_bounds_report_the_local_days_with_data(self, client: MinmaxClient) -> None:
        assert client.bounds("timecard") == (date(2026, 6, 5), date(2026, 6, 6))

    def test_bounds_are_none_for_an_empty_stream(self, repository: Repository) -> None:
        from minmax.core.models import StreamKind

        repository.ensure_stream("empty", "Empty", StreamKind.INTERVAL)
        assert InProcessClient(repository, timezone=PACIFIC).bounds("empty") is None

    def test_an_unknown_stream_raises(self, client: MinmaxClient) -> None:
        with pytest.raises(LookupError, match="no stream"):
            client.day("fitbit", date(2026, 6, 6))


class TestDayView:
    def test_returns_everything_the_timeline_needs_in_one_call(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        assert isinstance(view, DayView)
        assert view.day_end_epoch - view.day_start_epoch == 24 * HOUR
        assert view.tracked_seconds == 32_400
        assert not view.is_empty

    def test_includes_a_run_that_began_the_night_before(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        assert [record.recorded_path for record in view.records] == [
            "sleep/night",
            "social",
            "media/gaming/chess",
            "social/friends",
        ]
        # The record keeps its true extent; clipping is the timeline's job.
        assert view.records[0].start_epoch < view.day_start_epoch

    def test_totals_are_clipped_to_the_day_even_though_records_are_not(
        self, client: MinmaxClient
    ) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        totals = {slice_.category_path: slice_.seconds for slice_ in view.totals}
        assert totals["sleep/night"] == 7 * HOUR
        assert sum(totals.values()) == view.tracked_seconds

    def test_records_are_labelled_with_the_path_recorded_at_the_time(
        self, client: MinmaxClient
    ) -> None:
        # The same timer, under the name it had on each occasion.
        view = client.day("timecard", date(2026, 6, 6))
        social = [r for r in view.records if r.category_path == "social/friends"]
        assert [r.recorded_path for r in social] == ["social", "social/friends"]

    def test_clock_labels_use_the_records_own_offset(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        assert view.records[0].start_label == "23:00"
        assert view.records[0].end_label == "07:00"

    def test_an_empty_day_is_reported_not_raised(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 7, 1))
        assert view.is_empty
        assert view.tracked_seconds == 0
        assert view.totals == []

    def test_shares_sum_to_one_over_a_non_empty_day(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        assert sum(slice_.share for slice_ in view.totals) == pytest.approx(1.0)


class TestHourMarks:
    def test_an_ordinary_day_is_labelled_midnight_to_eleven(self, client: MinmaxClient) -> None:
        marks = client.day("timecard", date(2026, 6, 6)).hour_marks
        assert len(marks) == 24
        assert [mark.label for mark in marks[:3]] == ["00:00", "01:00", "02:00"]
        assert marks[-1].label == "23:00"

    def test_a_fall_back_day_shows_one_o_clock_twice(self, client: MinmaxClient) -> None:
        # The clock really did read 01:00 twice. A view computing labels from
        # elapsed time would instead count up to 24:00, which never happened.
        marks = client.day("timecard", date(2026, 11, 1)).hour_marks
        assert len(marks) == 25
        assert [mark.label for mark in marks].count("01:00") == 2

    def test_a_spring_forward_day_skips_two_o_clock(self, client: MinmaxClient) -> None:
        marks = client.day("timecard", date(2026, 3, 8)).hour_marks
        assert len(marks) == 23
        assert "02:00" not in [mark.label for mark in marks]

    def test_marks_span_the_whole_day(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6))
        assert view.hour_marks[0].epoch == view.day_start_epoch
        assert view.hour_marks[-1].epoch < view.day_end_epoch


class TestRangeTotals:
    def test_covers_the_whole_inclusive_range(self, client: MinmaxClient) -> None:
        totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6))
        assert totals.total_seconds == 36_000
        assert {s.category_path for s in totals.slices} == {
            "sleep/night",
            "social/friends",
            "media/gaming/chess",
        }

    def test_a_single_day_range_matches_that_day_s_view(self, client: MinmaxClient) -> None:
        day = client.day("timecard", date(2026, 6, 6))
        totals = client.range_totals("timecard", date(2026, 6, 6), date(2026, 6, 6))
        assert totals.total_seconds == day.tracked_seconds

    def test_a_prefix_restricts_to_a_subtree(self, client: MinmaxClient) -> None:
        totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6), prefix="media")
        assert totals.total_seconds == 2921
        assert all(s.category_path.startswith("media") for s in totals.slices)

    def test_an_inverted_range_is_rejected(self, client: MinmaxClient) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            client.range_totals("timecard", date(2026, 6, 6), date(2026, 6, 5))

    def test_an_empty_range_reports_zero_rather_than_dividing_by_it(
        self, client: MinmaxClient
    ) -> None:
        totals = client.range_totals("timecard", date(2026, 7, 1), date(2026, 7, 2))
        assert totals.total_seconds == 0
        assert totals.slices == []


class TestColours:
    def test_every_category_in_a_group_shares_one_colour(self, client: MinmaxClient) -> None:
        # Timecard's model: colour belongs to the top-level folder, so a whole
        # branch reads as one colour.
        totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6))
        by_group: dict[str, set[str]] = {}
        for slice_ in totals.slices:
            by_group.setdefault(slice_.top_level, set()).add(slice_.swatch.light)
        assert all(len(colours) == 1 for colours in by_group.values())

    def test_different_groups_get_different_colours(self, client: MinmaxClient) -> None:
        totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6))
        colours = {s.top_level: s.swatch.light for s in totals.slices}
        assert len(set(colours.values())) == len(colours)

    def test_colours_are_stable_across_calls(self, client: MinmaxClient) -> None:
        first = {s.top_level: s.swatch for s in client.day("timecard", date(2026, 6, 6)).totals}
        second = {s.top_level: s.swatch for s in client.day("timecard", date(2026, 6, 6)).totals}
        assert first == second

    def test_a_new_group_does_not_disturb_existing_assignments(
        self, repository: Repository, home: Path, sample_csv: Path, tmp_path: Path
    ) -> None:
        # A colour the user has learned to recognize must not shift because
        # another group appeared later.
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        client = InProcessClient(repository, timezone=PACIFIC)
        before = {s.top_level: s.swatch for s in client.day("timecard", date(2026, 6, 6)).totals}

        later = tmp_path / "later.csv"
        later.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,aardvark/first,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        run_import(repository, TimecardCsvImporter(), later, home=home)

        after = {s.top_level: s.swatch for s in client.day("timecard", date(2026, 6, 6)).totals}
        assert after == before

    def test_all_colours_come_from_the_palette(self, client: MinmaxClient) -> None:
        totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6))
        assert all(s.swatch in palette.PALETTE for s in totals.slices)

    def test_light_and_dark_steps_differ_for_most_hues(self) -> None:
        # A dark surface needs its own steps, not an inversion of the light ones.
        differing = [s for s in palette.PALETTE if s.light != s.dark]
        assert len(differing) >= len(palette.PALETTE) - 1


class TestPalette:
    def test_assignment_spreads_evenly_once_the_palette_is_exhausted(self) -> None:
        assigned: list[int] = []
        for _ in range(len(palette.PALETTE) * 2):
            assigned.append(palette.next_slot(assigned))
        counts = {slot: assigned.count(slot) for slot in range(len(palette.PALETTE))}
        assert set(counts.values()) == {2}

    def test_the_first_assignments_take_the_palette_in_order(self) -> None:
        # "Assign categorical hues in fixed order, never cycled."
        assigned: list[int] = []
        for _ in palette.PALETTE:
            assigned.append(palette.next_slot(assigned))
        assert assigned == list(range(len(palette.PALETTE)))

    def test_slots_beyond_the_palette_wrap_rather_than_fail(self) -> None:
        assert palette.swatch_for_slot(len(palette.PALETTE)) == palette.PALETTE[0]

    @pytest.mark.parametrize("swatch", palette.PALETTE)
    def test_every_palette_colour_gets_a_readable_text_colour(self, swatch: palette.Swatch) -> None:
        for hex_colour in (swatch.light, swatch.dark):
            assert palette.text_colour_on(hex_colour) in {"#000000", "#FFFFFF"}

    def test_the_folded_tail_is_neutral_not_a_hue(self) -> None:
        # "Other" is not an identity; giving it a hue would imply it were one.
        assert palette.OTHER not in palette.PALETTE

    def test_text_is_white_on_dark_and_black_on_light(self) -> None:
        assert palette.text_colour_on("#000000") == "#FFFFFF"
        assert palette.text_colour_on("#FFFFFF") == "#000000"

    def test_a_malformed_colour_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a #RRGGBB colour"):
            palette.text_colour_on("#FFF")


class TestTimezoneHandling:
    def test_the_client_decides_what_a_day_means(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        # The sleep run starts 23:00 Pacific on the 5th, which is already the
        # 6th in UTC. Which day it lands on is the client's timezone's call.
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        pacific = InProcessClient(repository, timezone=PACIFIC)
        utc = InProcessClient(repository, timezone=ZoneInfo("UTC"))

        assert pacific.bounds("timecard") == (date(2026, 6, 5), date(2026, 6, 6))
        assert utc.bounds("timecard") == (date(2026, 6, 6), date(2026, 6, 6))

    def test_totals_still_reconcile_under_a_different_timezone(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        utc = InProcessClient(repository, timezone=ZoneInfo("UTC"))
        totals = utc.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 7))
        assert totals.total_seconds == 36_000


class TestTimelineWindow:
    def test_covers_every_day_in_the_range(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 4), date(2026, 6, 7))
        assert window.first_day == date(2026, 6, 4)
        assert window.last_day == date(2026, 6, 7)
        assert [summary.day for summary in window.days] == [
            date(2026, 6, 4),
            date(2026, 6, 5),
            date(2026, 6, 6),
            date(2026, 6, 7),
        ]

    def test_a_midnight_crossing_run_appears_once_not_twice(self, client: MinmaxClient) -> None:
        # The whole reason the timeline scrolls continuously: the overnight run
        # is one record spanning the boundary, not two halves stitched together.
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6))
        sleep = [record for record in window.records if record.category_path == "sleep/night"]
        assert len(sleep) == 1
        assert sleep[0].duration_seconds == 8 * HOUR

    def test_day_marks_name_each_local_midnight(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6))
        assert [mark.day for mark in window.day_marks] == [date(2026, 6, 5), date(2026, 6, 6)]
        assert window.day_marks[0].epoch == window.start_epoch
        assert "5 Jun" in window.day_marks[0].label

    def test_hour_marks_span_the_whole_window(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6))
        assert len(window.hour_marks) == 48

    def test_per_day_totals_travel_with_the_records(self, client: MinmaxClient) -> None:
        # So scrolling from one day to the next needs no second round trip.
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6))
        assert window.summary_for(date(2026, 6, 5)).tracked_seconds == HOUR
        assert window.summary_for(date(2026, 6, 6)).tracked_seconds == 9 * HOUR

    def test_totals_match_the_single_day_view(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6))
        for summary in window.days:
            view = client.day("timecard", summary.day)
            assert summary.tracked_seconds == view.tracked_seconds

    def test_a_prefix_restricts_records_and_totals_together(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 5), date(2026, 6, 6), prefix="social")
        # Both rows belong to the timer that was renamed, so both carry the
        # category's current path; only what they were *recorded* under differs.
        assert {record.recorded_path for record in window.records} == {"social", "social/friends"}
        assert window.summary_for(date(2026, 6, 6)).tracked_seconds == 679 + 3600
        assert window.prefix == "social"

    def test_covers_reports_whether_a_reload_is_needed(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 6, 4), date(2026, 6, 7))
        assert window.covers(date(2026, 6, 5), date(2026, 6, 6))
        assert not window.covers(date(2026, 6, 1), date(2026, 6, 5))

    def test_an_inverted_window_is_rejected(self, client: MinmaxClient) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            client.window("timecard", date(2026, 6, 7), date(2026, 6, 4))

    def test_an_empty_window_is_reported_not_raised(self, client: MinmaxClient) -> None:
        window = client.window("timecard", date(2026, 7, 1), date(2026, 7, 3))
        assert window.is_empty
        assert len(window.days) == 3


class TestProvenance:
    def test_every_record_names_the_import_it_came_from(self, client: MinmaxClient) -> None:
        # What makes a suspicious block answerable without a database query.
        view = client.day("timecard", date(2026, 6, 6))
        for record in view.records:
            assert record.import_run_id > 0
            assert record.source_name.endswith(".csv")

    def test_exact_instants_are_available_for_inspection(self, client: MinmaxClient) -> None:
        # HH:MM is right on a block and wrong in an inspector: two runs a minute
        # apart would read identically.
        view = client.day("timecard", date(2026, 6, 6))
        social = next(r for r in view.records if r.recorded_path == "social")
        assert social.start_detail == "2026-06-06 07:00:00 -0700"
        assert social.end_detail == "2026-06-06 07:11:19 -0700"


class TestDayFiltering:
    def test_a_prefix_restricts_the_day_view(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6), prefix="social")
        assert {record.recorded_path for record in view.records} == {"social", "social/friends"}
        assert view.tracked_seconds == 679 + 3600
        assert view.prefix == "social"

    def test_filtering_excludes_everything_outside_the_subtree(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6), prefix="media")
        assert {record.category_path for record in view.records} == {"media/gaming/chess"}
        assert view.tracked_seconds == 2921

    def test_shares_are_recomputed_against_the_filtered_total(self, client: MinmaxClient) -> None:
        view = client.day("timecard", date(2026, 6, 6), prefix="social")
        assert sum(slice_.share for slice_ in view.totals) == pytest.approx(1.0)


class TestImportFile:
    """``import_file`` is what the GUI's file upload calls -- it must behave
    exactly like the CLI's ``import`` command, just returning a value instead
    of printing one."""

    def test_a_client_with_no_home_cannot_import(self, repository: Repository) -> None:
        client = InProcessClient(repository, timezone=PACIFIC)
        with pytest.raises(RuntimeError, match="no data directory"):
            client.import_file(Path("whatever.csv"), stream_key="timecard")

    def test_importing_a_fresh_file_stores_it_and_reports_the_counts(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        outcome = client.import_file(sample_csv, stream_key="timecard")
        assert outcome.ok
        assert not outcome.skipped
        assert outcome.stream_key == "timecard"
        assert (outcome.rows_read, outcome.inserted, outcome.duplicates, outcome.rejected) == (
            4,
            4,
            0,
            0,
        )
        assert client.bounds("timecard") == (date(2026, 6, 5), date(2026, 6, 6))

    def test_reimporting_identical_content_is_reported_as_skipped(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        client.import_file(sample_csv, stream_key="timecard")
        outcome = client.import_file(sample_csv, stream_key="timecard")
        assert outcome.ok
        assert outcome.skipped
        assert outcome.inserted == 0
        assert "already imported" in outcome.notes[0]

    def test_force_reruns_a_skipped_import(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        client.import_file(sample_csv, stream_key="timecard")
        outcome = client.import_file(sample_csv, stream_key="timecard", force=True)
        assert not outcome.skipped
        # Identical rows, so they land as duplicates rather than new inserts.
        assert outcome.inserted == 0
        assert outcome.duplicates == 4

    def test_bad_rows_are_rejected_without_losing_the_good_ones(
        self, repository: Repository, home: Path, malformed_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        outcome = client.import_file(malformed_csv, stream_key="timecard")
        assert outcome.ok
        assert outcome.inserted == 1
        assert outcome.rejected == 6

    def test_a_file_that_conflicts_with_stored_data_reports_an_error_not_a_crash(
        self, repository: Repository, home: Path, sample_csv: Path, tmp_path: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        client.import_file(sample_csv, stream_key="timecard")

        conflicting = tmp_path / "edited.csv"
        conflicting.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "ZZZZ,admin,2026-06-06T07:30:00-07:00,2026-06-06T08:30:00-07:00,3600\n",
            encoding="utf-8",
        )
        outcome = client.import_file(conflicting, stream_key="timecard")
        assert not outcome.ok
        assert "overlaps" in outcome.error

    def test_a_file_the_importer_does_not_recognise_reports_an_error(
        self, repository: Repository, home: Path, tmp_path: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        wrong = tmp_path / "wrong.csv"
        wrong.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        outcome = client.import_file(wrong, stream_key="timecard")
        assert not outcome.ok
        assert "not a Timecard export" in outcome.error

    def test_an_unknown_stream_key_reports_an_error(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        outcome = client.import_file(sample_csv, stream_key="fitbit")
        assert not outcome.ok
        assert "no stream 'fitbit'" in outcome.error

    def test_a_missing_file_reports_an_error(self, repository: Repository, home: Path) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        outcome = client.import_file(home / "nope.csv", stream_key="timecard")
        assert not outcome.ok
        assert "no such file" in outcome.error


class TestStreamCreation:
    def test_a_created_stream_appears_in_streams(self, repository: Repository, home: Path) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        summary = client.create_stream(
            "pickups",
            "Pickups",
            StreamKind.EVENT,
            fields=[FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="at")],
        )
        assert summary.key == "pickups"
        assert summary.kind == "event"
        assert summary.record_count == 0
        assert [s.key for s in client.streams()] == ["pickups"]

    def test_a_duplicate_key_is_refused(self, repository: Repository, home: Path) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        client.create_stream("pickups", "Pickups", StreamKind.EVENT)
        with pytest.raises(ValueError, match="already exists"):
            client.create_stream("pickups", "Pickups Again", StreamKind.EVENT)


class TestAutoFetch:
    def test_a_stream_starts_with_auto_fetch_off(self, client: MinmaxClient) -> None:
        [summary] = client.streams()
        assert summary.watch_enabled is False
        assert summary.watch_path is None

    def test_set_auto_fetch_remembers_the_path_and_turns_it_on(self, client: MinmaxClient) -> None:
        client.set_auto_fetch("timecard", enabled=True, path=Path("/data/timecard.csv"))

        [summary] = client.streams()
        assert summary.watch_enabled is True
        assert summary.watch_path == "/data/timecard.csv"

    def test_disabling_keeps_the_remembered_path(self, client: MinmaxClient) -> None:
        client.set_auto_fetch("timecard", enabled=True, path=Path("/data/timecard.csv"))
        client.set_auto_fetch("timecard", enabled=False)

        [summary] = client.streams()
        assert summary.watch_enabled is False
        assert summary.watch_path == "/data/timecard.csv"

    def test_re_enabling_without_a_path_keeps_the_old_one(self, client: MinmaxClient) -> None:
        client.set_auto_fetch("timecard", enabled=True, path=Path("/data/timecard.csv"))
        client.set_auto_fetch("timecard", enabled=False)
        client.set_auto_fetch("timecard", enabled=True)

        [summary] = client.streams()
        assert summary.watch_enabled is True
        assert summary.watch_path == "/data/timecard.csv"

    def test_an_unknown_stream_raises(self, client: MinmaxClient) -> None:
        with pytest.raises(LookupError, match="no stream"):
            client.set_auto_fetch("fitbit", enabled=True, path=Path("/data/x.csv"))

    def test_a_periodic_reimport_merges_new_rows_from_the_same_path(
        self, repository: Repository, home: Path, tmp_path: Path
    ) -> None:
        """What the GUI's once-a-minute tick actually does: call import_file
        again on the remembered path. A growing file is re-read whole each
        time, and the natural-key constraint is what makes that a merge
        rather than a duplication -- this is the guarantee the auto-fetch
        feature relies on, so it is worth pinning down here directly rather
        than only through the UI plumbing on top of it."""
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        feed = tmp_path / "feed.csv"
        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "A,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )

        first = client.import_file(feed, stream_key="timecard")
        assert first.ok
        assert first.inserted == 1

        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "A,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n"
            "B,writing,2026-06-07T10:00:00-07:00,2026-06-07T11:00:00-07:00,3600\n",
            encoding="utf-8",
        )

        second = client.import_file(feed, stream_key="timecard")
        assert second.ok
        assert second.inserted == 1  # only the new row
        assert second.duplicates == 1  # the row from the first fetch, recognised

        [summary] = client.streams()
        assert summary.record_count == 2


class TestCorrelations:
    """The statistics themselves (Pearson's r, the sample-size gates, the
    multiple-comparisons correction) are already pinned down against
    hand-computed fixtures in ``tests/test_analysis_*.py``. What matters here
    is the flattening: everything reaching a caller through this method must
    be a plain, serializable value (the same rule ``TestProtocolConformance``
    checks for every other view), and every field the analysis layer
    computes must actually survive the trip.
    """

    @pytest.fixture
    def two_correlated_streams(self, repository: Repository, home: Path) -> MinmaxClient:
        """Two interval streams whose durations are an exact linear function
        of the same day index, over enough days to clear every gate at daily
        granularity -- just enough to exercise the API without re-testing
        the statistics themselves."""
        a = repository.ensure_stream("focus", "Focus", StreamKind.INTERVAL)
        b = repository.ensure_stream("chores", "Chores", StreamKind.INTERVAL)
        assert a.id is not None
        assert b.id is not None
        run_a = repository.begin_import(
            a.id, importer="test", source_name="a.csv", source_sha256="a" * 64, archived_path=None
        )
        run_b = repository.begin_import(
            b.id, importer="test", source_name="b.csv", source_sha256="b" * 64, archived_path=None
        )

        def on(day_index: int, hour: int, minute: int) -> Instant:
            day = date(2026, 6, 1) + timedelta(days=day_index)
            return Instant.from_iso(f"{day.isoformat()}T{hour:02d}:{minute:02d}:00-07:00")

        intervals_a = [
            IntervalRecord(
                category_source_id="work",
                category_path="work",
                start=on(i, 9, 0),
                end=on(i, 9, 10 + i),  # linear in i
            )
            for i in range(10)
        ]
        intervals_b = [
            IntervalRecord(
                category_source_id="chores",
                category_path="chores",
                start=on(i, 14, 0),
                end=on(i, 14, 20 + 2 * i),  # linear in i, same slope sign
            )
            for i in range(10)
        ]
        repository.insert_intervals(a, run_a, intervals_a)
        repository.insert_intervals(b, run_b, intervals_b)
        return InProcessClient(repository, timezone=PACIFIC, home=home)

    def test_only_plain_serializable_values_cross_the_boundary(
        self, two_correlated_streams: MinmaxClient
    ) -> None:
        view = two_correlated_streams.correlations(min_periods=5, min_r=0.0, top=None)
        assert view.rows, "expected at least one row from two clearly related streams"
        for row in view.rows:
            for field in dataclasses.fields(row):
                value = getattr(row, field.name)
                if isinstance(value, tuple):
                    assert all(isinstance(item, str) for item in value)
                else:
                    assert isinstance(value, str | int | float | bool | date)

    def test_the_correlated_pair_appears_with_a_high_r(
        self, two_correlated_streams: MinmaxClient
    ) -> None:
        view = two_correlated_streams.correlations(min_periods=5, min_r=0.0, top=None)
        [row] = [
            r for r in view.rows if {r.x_key, r.y_key} == {"focus:duration", "chores:duration"}
        ]
        assert abs(row.r) > 0.9

    def test_an_empty_database_returns_an_empty_view(self, client: MinmaxClient) -> None:
        # `client` (from the module fixture) has already imported one
        # stream's worth of Timecard data with only one category, so there
        # is nothing to pair -- not an error, just nothing to report.
        view = client.correlations()
        assert view.rows == []
        assert view.is_empty

    def test_all_intervals_emits_one_row_per_tested_interval(
        self, two_correlated_streams: MinmaxClient
    ) -> None:
        best_only = two_correlated_streams.correlations(min_periods=5, min_r=0.0, top=None)
        every_interval = two_correlated_streams.correlations(
            min_periods=5, min_r=0.0, top=None, all_intervals=True
        )
        assert len(every_interval.rows) >= len(best_only.rows)

    def test_skipped_notes_are_aggregated_not_one_per_combination(
        self, two_correlated_streams: MinmaxClient
    ) -> None:
        # Only 10 days of data: a fortnight or a month cannot produce even
        # one complete period, so those show up as (few) grouped notes.
        view = two_correlated_streams.correlations(min_periods=8, min_r=0.0, top=None)
        assert view.skipped_notes
        assert len(view.skipped_notes) < 20  # grouped, not one line per pair


class TestCsvInspection:
    def test_inspecting_a_valid_csv_guesses_the_time_column(
        self, repository: Repository, home: Path, heart_rate_csv: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        preview = client.inspect_csv(heart_rate_csv)
        assert preview.ok
        assert preview.header == ("measured_at", "bpm")
        assert preview.sample_rows[0] == ("2026-06-06T08:00:00-07:00", "58")
        at_fields = [f for f in preview.suggested_fields if f.role is FieldRole.AT]
        assert [f.source_column for f in at_fields] == ["measured_at"]

    def test_inspecting_a_missing_file_reports_an_error_not_a_crash(
        self, repository: Repository, home: Path
    ) -> None:
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        preview = client.inspect_csv(home / "nope.csv")
        assert not preview.ok
        assert isinstance(preview, CsvPreview)
        assert preview.header == ()
        assert preview.suggested_fields == ()


class TestEventStreamViews:
    """The point-in-time analogues of the interval day/window/range/trends
    tests above -- same protocol methods, dispatched by the stream's kind."""

    def test_bounds_span_both_days(self, event_client: MinmaxClient) -> None:
        assert event_client.bounds("heart_rate") == (date(2026, 6, 6), date(2026, 6, 7))

    def test_day_view_returns_markers_not_blocks(self, event_client: MinmaxClient) -> None:
        view = event_client.day("heart_rate", date(2026, 6, 6))
        assert view.stream_kind is StreamKind.EVENT
        assert len(view.records) == 2
        assert all(record.is_instant for record in view.records)
        assert all(record.start_epoch == record.end_epoch for record in view.records)
        assert all(record.duration_seconds == 0 for record in view.records)
        assert view.records[0].fields == (("BPM", "60 bpm"),)
        # tracked_seconds carries the count for an event stream, documented
        # on DayView.stream_kind.
        assert view.tracked_seconds == 2
        [total] = view.totals
        assert total.record_count == 2
        assert total.seconds == 0
        assert total.share == pytest.approx(1.0)

    def test_window_view_covers_both_days(self, event_client: MinmaxClient) -> None:
        window = event_client.window("heart_rate", date(2026, 6, 6), date(2026, 6, 7))
        assert window.stream_kind is StreamKind.EVENT
        assert len(window.records) == 3
        by_day = {summary.day: summary.tracked_seconds for summary in window.days}
        assert by_day == {date(2026, 6, 6): 2, date(2026, 6, 7): 1}

    def test_range_totals_count_across_the_range(self, event_client: MinmaxClient) -> None:
        totals = event_client.range_totals("heart_rate", date(2026, 6, 6), date(2026, 6, 7))
        assert totals.stream_kind is StreamKind.EVENT
        assert totals.total_seconds == 3
        [slice_] = totals.slices
        assert slice_.record_count == 3

    def test_trends_plots_counts_per_column(self, event_client: MinmaxClient) -> None:
        from minmax.api import Granularity

        view = event_client.trends("heart_rate", interval=Granularity.DAY)
        assert view.stream_kind is StreamKind.EVENT
        assert len(view.columns) == 2
        [series] = view.series
        assert series.values == [2, 1]
        assert series.total_seconds == 3
        assert view.total_seconds == 3

    def test_a_field_less_stream_files_under_the_implicit_category(
        self, repository: Repository, home: Path
    ) -> None:
        from minmax.core.models import implicit_category_path

        stream = repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        assert stream.id is not None
        run = repository.begin_import(
            stream.id,
            importer="test",
            source_name="p.csv",
            source_sha256="p" * 64,
            archived_path=None,
        )
        repository.insert_events(
            stream,
            run,
            [
                EventRecord(
                    None,
                    implicit_category_path("pickups"),
                    Instant.from_iso("2026-06-06T09:00:00-07:00"),
                )
            ],
        )
        client = InProcessClient(repository, timezone=PACIFIC, home=home)
        view = client.day("pickups", date(2026, 6, 6))
        assert view.records[0].fields == ()
        assert view.records[0].category_path == "pickups"
