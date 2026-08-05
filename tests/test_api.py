"""Tests for the client facade.

This is also the contract a future ``HttpClient`` must satisfy, so the tests are
written against the :class:`MinmaxClient` protocol rather than against
``InProcessClient``'s internals.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from minmax.api import DayView, InProcessClient, MinmaxClient
from minmax.core import palette
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository

PACIFIC = ZoneInfo("America/Los_Angeles")
HOUR = 3600


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
