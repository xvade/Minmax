"""Tests for day-timeline layout arithmetic.

These cover the logic that would otherwise hide inside a paint method, where a
mistake is invisible to every other test in the suite.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from minmax.core.timeline import (
    MAX_PIXELS_PER_HOUR,
    MIN_PIXELS_PER_HOUR,
    DayGeometry,
    anchored_scroll_offset,
    clamp_zoom,
    hour_boundaries,
    lay_out_day,
)
from minmax.core.timeref import Instant, local_day_bounds

PACIFIC = ZoneInfo("America/Los_Angeles")
HOUR = 3600


def geometry_for(day: str, pixels_per_hour: float = 60.0) -> DayGeometry:
    start, end = local_day_bounds(date.fromisoformat(day), PACIFIC)
    return DayGeometry(start, end, pixels_per_hour)


def at(text: str) -> int:
    return Instant.from_iso(text).epoch_seconds


class TestDayGeometry:
    def test_an_ordinary_day_is_twenty_four_hours_tall(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        assert geometry.seconds == 24 * HOUR
        assert geometry.height == pytest.approx(24 * 60.0)

    def test_positions_are_proportional_to_elapsed_time(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        assert geometry.y_for_epoch(geometry.day_start_epoch) == pytest.approx(0.0)
        assert geometry.y_for_epoch(at("2026-06-06T06:00:00-07:00")) == pytest.approx(360.0)
        assert geometry.y_for_epoch(geometry.day_end_epoch) == pytest.approx(geometry.height)

    def test_epoch_for_y_inverts_y_for_epoch(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=97.0)
        moment = at("2026-06-06T13:47:00-07:00")
        assert geometry.epoch_for_y(geometry.y_for_epoch(moment)) == moment

    def test_a_spring_forward_day_is_only_twenty_three_hours(self) -> None:
        # Sizing the timeline from the day's real bounds means a DST day renders
        # correctly with no special case: the short day is simply shorter.
        geometry = geometry_for("2026-03-08")
        assert geometry.seconds == 23 * HOUR

    def test_a_fall_back_day_is_twenty_five_hours(self) -> None:
        geometry = geometry_for("2026-11-01")
        assert geometry.seconds == 25 * HOUR

    def test_a_day_must_have_positive_length(self) -> None:
        with pytest.raises(ValueError, match="must end after it starts"):
            DayGeometry(1000, 1000, 60.0)

    def test_zoom_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            DayGeometry(0, HOUR, 0.0)


class TestClampZoom:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (0.001, MIN_PIXELS_PER_HOUR),
            (10_000.0, MAX_PIXELS_PER_HOUR),
            (100.0, 100.0),
        ],
    )
    def test_stays_within_usable_limits(self, requested: float, expected: float) -> None:
        assert clamp_zoom(requested) == expected

    def test_maximum_zoom_makes_a_one_minute_run_selectable(self) -> None:
        # Timecard's bar: zoom in far enough to comfortably select a ~1-minute
        # run. At the maximum, one minute must be a comfortably clickable height.
        one_minute_px = MAX_PIXELS_PER_HOUR / 60
        assert one_minute_px >= 8.0

    def test_minimum_zoom_fits_a_whole_day_on_screen(self) -> None:
        assert 24 * MIN_PIXELS_PER_HOUR <= 700


class TestLayOutDay:
    def test_a_block_is_sized_to_its_real_duration(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        [block] = lay_out_day(
            [(at("2026-06-06T09:00:00-07:00"), at("2026-06-06T11:30:00-07:00"))], geometry
        )
        assert block.top == pytest.approx(9 * 60.0)
        assert block.height == pytest.approx(2.5 * 60.0)

    def test_adjacent_runs_tile_without_gaps_or_overlap(self) -> None:
        # 325 of 337 adjacent runs in the real export share an exact boundary,
        # so any rounding here would show up as visible seams.
        geometry = geometry_for("2026-06-06", pixels_per_hour=73.0)
        blocks = lay_out_day(
            [
                (at("2026-06-06T09:00:00-07:00"), at("2026-06-06T10:17:00-07:00")),
                (at("2026-06-06T10:17:00-07:00"), at("2026-06-06T11:03:00-07:00")),
            ],
            geometry,
        )
        assert blocks[0].bottom == pytest.approx(blocks[1].top)

    def test_a_run_from_the_previous_night_is_clipped_and_flagged(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        [block] = lay_out_day(
            [(at("2026-06-05T23:00:00-07:00"), at("2026-06-06T07:00:00-07:00"))], geometry
        )
        assert block.top == pytest.approx(0.0)
        assert block.height == pytest.approx(7 * 60.0)
        assert block.continues_before and not block.continues_after

    def test_a_run_into_the_next_day_is_clipped_and_flagged(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        [block] = lay_out_day(
            [(at("2026-06-06T22:00:00-07:00"), at("2026-06-07T06:00:00-07:00"))], geometry
        )
        assert block.bottom == pytest.approx(geometry.height)
        assert block.continues_after and not block.continues_before

    def test_a_run_spanning_the_whole_day_is_flagged_both_ways(self) -> None:
        geometry = geometry_for("2026-06-06")
        [block] = lay_out_day(
            [(at("2026-06-05T20:00:00-07:00"), at("2026-06-07T04:00:00-07:00"))], geometry
        )
        assert block.continues_before and block.continues_after
        assert block.height == pytest.approx(geometry.height)

    def test_runs_outside_the_day_are_dropped(self) -> None:
        geometry = geometry_for("2026-06-06")
        assert (
            lay_out_day(
                [
                    (at("2026-06-04T09:00:00-07:00"), at("2026-06-04T10:00:00-07:00")),
                    (at("2026-06-08T09:00:00-07:00"), at("2026-06-08T10:00:00-07:00")),
                ],
                geometry,
            )
            == []
        )

    def test_a_run_abutting_the_day_boundary_does_not_appear(self) -> None:
        # Ranges are half-open: a run ending exactly at midnight belongs to the
        # previous day, not this one.
        geometry = geometry_for("2026-06-06")
        span = [(at("2026-06-05T22:00:00-07:00"), geometry.day_start_epoch)]
        assert lay_out_day(span, geometry) == []

    def test_blocks_keep_the_index_of_their_source_record(self) -> None:
        geometry = geometry_for("2026-06-06")
        blocks = lay_out_day(
            [
                (at("2026-06-01T09:00:00-07:00"), at("2026-06-01T10:00:00-07:00")),  # dropped
                (at("2026-06-06T09:00:00-07:00"), at("2026-06-06T10:00:00-07:00")),
            ],
            geometry,
        )
        assert [block.index for block in blocks] == [1]

    def test_labels_appear_only_when_a_block_is_tall_enough(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        blocks = lay_out_day(
            [
                (at("2026-06-06T09:00:00-07:00"), at("2026-06-06T09:02:00-07:00")),  # 2px
                (at("2026-06-06T10:00:00-07:00"), at("2026-06-06T10:20:00-07:00")),  # 20px
                (at("2026-06-06T11:00:00-07:00"), at("2026-06-06T12:00:00-07:00")),  # 60px
            ],
            geometry,
        )
        assert [b.show_label for b in blocks] == [False, True, True]
        # The duration joins the label only once there is room for both.
        assert [b.show_duration for b in blocks] == [False, False, True]

    def test_zooming_in_reveals_labels_on_short_runs(self) -> None:
        span = [(at("2026-06-06T09:00:00-07:00"), at("2026-06-06T09:02:00-07:00"))]
        assert not lay_out_day(span, geometry_for("2026-06-06", 60.0))[0].show_label
        assert lay_out_day(span, geometry_for("2026-06-06", MAX_PIXELS_PER_HOUR))[0].show_label


class TestHourBoundaries:
    def test_an_ordinary_day_has_one_boundary_per_hour(self) -> None:
        geometry = geometry_for("2026-06-06", pixels_per_hour=60.0)
        marks = hour_boundaries(geometry.day_start_epoch, geometry.day_end_epoch)
        assert len(marks) == 24
        assert marks[0] == geometry.day_start_epoch
        assert geometry.y_for_epoch(marks[6]) == pytest.approx(360.0)

    def test_a_fall_back_day_gets_twenty_five_boundaries(self) -> None:
        geometry = geometry_for("2026-11-01")
        assert len(hour_boundaries(geometry.day_start_epoch, geometry.day_end_epoch)) == 25

    def test_a_spring_forward_day_gets_twenty_three_boundaries(self) -> None:
        geometry = geometry_for("2026-03-08")
        assert len(hour_boundaries(geometry.day_start_epoch, geometry.day_end_epoch)) == 23

    def test_boundaries_can_be_thinned_when_zoomed_out(self) -> None:
        geometry = geometry_for("2026-06-06")
        marks = hour_boundaries(geometry.day_start_epoch, geometry.day_end_epoch, every=6)
        assert len(marks) == 4

    def test_every_must_be_positive(self) -> None:
        geometry = geometry_for("2026-06-06")
        with pytest.raises(ValueError, match="at least 1"):
            hour_boundaries(geometry.day_start_epoch, geometry.day_end_epoch, every=0)


class TestAnchoredScrollOffset:
    def test_the_instant_under_the_pointer_stays_put(self) -> None:
        # Without this the timeline appears to fling around while zooming.
        old, new = 60.0, 120.0
        offset, anchor = 300.0, 200.0

        before = DayGeometry(0, 24 * HOUR, old).epoch_for_y(offset + anchor)
        after_offset = anchored_scroll_offset(offset, anchor, old, new)
        after = DayGeometry(0, 24 * HOUR, new).epoch_for_y(after_offset + anchor)
        assert before == pytest.approx(after, abs=1)

    def test_zooming_at_the_top_of_the_viewport_keeps_the_top_fixed(self) -> None:
        assert anchored_scroll_offset(0.0, 0.0, 60.0, 240.0) == pytest.approx(0.0)

    def test_unchanged_zoom_leaves_the_offset_alone(self) -> None:
        assert anchored_scroll_offset(412.0, 88.0, 60.0, 60.0) == pytest.approx(412.0)

    def test_rejects_a_nonsensical_previous_zoom(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            anchored_scroll_offset(0.0, 0.0, 0.0, 60.0)
