"""Tests for the stacked-area arithmetic and the trends query."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from minmax.api import Granularity, InProcessClient, Interval, MinmaxClient
from minmax.core import palette
from minmax.core.trends import (
    MAX_PIXELS_PER_COLUMN,
    MIN_PIXELS_PER_COLUMN,
    TrendsGeometry,
    clamp_column_zoom,
    nice_count_ceiling,
    nice_seconds_ceiling,
    stack_bands,
    tick_interval_columns,
    visible_column_range,
    window_totals,
)
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository

PACIFIC = ZoneInfo("America/Los_Angeles")
HOUR = 3600


@pytest.fixture
def client(repository: Repository, home: Path, sample_csv: Path) -> MinmaxClient:
    run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
    return InProcessClient(repository, timezone=PACIFIC)


class TestGeometry:
    def test_days_map_to_columns_left_to_right(self) -> None:
        geometry = TrendsGeometry(
            column_count=10, pixels_per_column=20.0, plot_height=100.0, max_value=100
        )
        assert geometry.x_for_column(0) == 0.0
        assert geometry.x_for_column(3) == 60.0
        assert geometry.content_width == 200.0

    def test_values_map_upward_from_the_baseline(self) -> None:
        geometry = TrendsGeometry(
            column_count=1, pixels_per_column=20.0, plot_height=200.0, max_value=100
        )
        assert geometry.y_for_value(0) == 200.0  # baseline at the bottom
        assert geometry.y_for_value(100) == 0.0  # ceiling at the top
        assert geometry.y_for_value(50) == 100.0

    def test_day_at_x_clamps_to_the_range(self) -> None:
        geometry = TrendsGeometry(
            column_count=5, pixels_per_column=10.0, plot_height=100.0, max_value=1
        )
        assert geometry.column_at_x(-50) == 0
        assert geometry.column_at_x(25) == 2
        assert geometry.column_at_x(9999) == 4

    def test_degenerate_geometry_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="pixels_per_column must be positive"):
            TrendsGeometry(column_count=1, pixels_per_column=0.0, plot_height=10.0, max_value=1)
        with pytest.raises(ValueError, match="max_value must be positive"):
            TrendsGeometry(column_count=1, pixels_per_column=1.0, plot_height=10.0, max_value=0)


class TestZoom:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [(0.01, MIN_PIXELS_PER_COLUMN), (10_000.0, MAX_PIXELS_PER_COLUMN), (30.0, 30.0)],
    )
    def test_zoom_stays_within_usable_limits(self, requested: float, expected: float) -> None:
        assert clamp_column_zoom(requested) == expected

    def test_minimum_zoom_shows_more_than_a_year(self) -> None:
        # The whole point of a trends screen: long spans must fit.
        assert 1000 / MIN_PIXELS_PER_COLUMN > 365


class TestVisibleDayRange:
    def test_reports_the_columns_actually_on_screen(self) -> None:
        geometry = TrendsGeometry(
            column_count=100, pixels_per_column=10.0, plot_height=100.0, max_value=1
        )
        assert visible_column_range(geometry, scroll_offset=0.0, viewport_width=200.0) == (0, 19)
        assert visible_column_range(geometry, scroll_offset=300.0, viewport_width=200.0) == (30, 49)

    def test_a_partially_visible_column_counts_as_visible(self) -> None:
        # It is drawn, so the labels and percentages must describe it.
        geometry = TrendsGeometry(
            column_count=100, pixels_per_column=10.0, plot_height=100.0, max_value=1
        )
        assert visible_column_range(geometry, scroll_offset=5.0, viewport_width=100.0) == (0, 10)

    def test_scrolling_past_the_end_clamps(self) -> None:
        geometry = TrendsGeometry(
            column_count=10, pixels_per_column=10.0, plot_height=100.0, max_value=1
        )
        assert visible_column_range(geometry, scroll_offset=9999.0, viewport_width=200.0) == (9, 9)


class TestStackBands:
    def test_bands_stack_bottom_to_top_in_order(self) -> None:
        bands = stack_bands([[10, 20], [5, 5], [1, 0]])
        assert bands[0] == [(0.0, 10.0), (0.0, 20.0)]
        assert bands[1] == [(10.0, 15.0), (20.0, 25.0)]
        assert bands[2] == [(15.0, 16.0), (25.0, 25.0)]

    def test_a_band_of_zero_has_no_height(self) -> None:
        bands = stack_bands([[0, 5]])
        assert bands[0][0] == (0.0, 0.0)

    def test_normalising_makes_each_day_sum_to_one(self) -> None:
        bands = stack_bands([[30, 10], [10, 30]], normalise=True)
        assert bands[0][0] == pytest.approx((0.0, 0.75))
        assert bands[1][0] == pytest.approx((0.75, 1.0))
        assert bands[1][1][1] == pytest.approx(1.0)

    def test_an_empty_day_normalises_to_nothing_not_to_full_height(self) -> None:
        # Stretching a day with no data to fill the plot would invent a shape.
        bands = stack_bands([[0, 10], [0, 10]], normalise=True)
        assert bands[0][0] == (0.0, 0.0)
        assert bands[1][0] == (0.0, 0.0)

    def test_ragged_series_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="same number of columns"):
            stack_bands([[1, 2], [3]])

    def test_no_series_yields_no_bands(self) -> None:
        assert stack_bands([]) == []


class TestWindowTotals:
    def test_sums_only_the_visible_days(self) -> None:
        assert window_totals([[1, 2, 4, 8], [10, 20, 40, 80]], 1, 2) == [6, 60]

    def test_the_window_is_inclusive_at_both_ends(self) -> None:
        assert window_totals([[1, 2, 4]], 0, 2) == [7]

    def test_out_of_range_windows_clamp(self) -> None:
        assert window_totals([[1, 2, 4]], -5, 99) == [7]


class TestAxisHelpers:
    @pytest.mark.parametrize(
        ("peak", "expected"),
        [(1, HOUR), (HOUR + 1, 2 * HOUR), (7 * HOUR, 8 * HOUR), (23 * HOUR, 24 * HOUR)],
    )
    def test_ceilings_snap_to_readable_hour_counts(self, peak: int, expected: int) -> None:
        assert nice_seconds_ceiling(peak) == expected

    def test_a_full_day_does_not_overflow_the_ramp(self) -> None:
        assert nice_seconds_ceiling(86_400) == 24 * HOUR
        assert nice_seconds_ceiling(999_999) == 24 * HOUR

    def test_tick_spacing_widens_as_the_chart_zooms_out(self) -> None:
        assert tick_interval_columns(80.0) == 1
        assert tick_interval_columns(10.0) == 7
        assert tick_interval_columns(2.0) == 56

    def test_tick_spacing_lands_on_meaningful_intervals(self) -> None:
        # A reader is better served by weekly ticks than by "every 5 days".
        for zoom in (2.0, 5.0, 12.0, 40.0, 80.0):
            assert tick_interval_columns(zoom) in (1, 2, 3, 7, 14, 28, 56, 91, 182, 364)


class TestCountCeiling:
    """The count-based analogue of :func:`nice_seconds_ceiling`, for an
    event stream's trends chart -- same idea, but with no 24h-style cap,
    since a count of events per day has no natural maximum."""

    @pytest.mark.parametrize(
        ("peak", "expected"),
        [(0, 1), (1, 1), (2, 2), (3, 5), (7, 10), (12, 20), (37, 50), (80, 100)],
    )
    def test_ceilings_snap_to_a_one_two_five_progression(self, peak: float, expected: int) -> None:
        assert nice_count_ceiling(peak) == expected

    def test_unlike_the_duration_ceiling_there_is_no_overflow_cap(self) -> None:
        # nice_seconds_ceiling clamps at 24h because a day cannot hold more;
        # a count has no such bound and must keep climbing past it.
        assert nice_count_ceiling(200) == 200
        assert nice_count_ceiling(9_999) == 10_000


class TestTrendsQuery:
    def test_returns_one_column_per_day_in_the_range(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert [column.start for column in view.columns] == [date(2026, 6, 5), date(2026, 6, 6)]
        assert all(len(series.values) == len(view.columns) for series in view.series)
        assert view.interval.unit is Granularity.DAY

    def test_groups_by_top_level_not_by_leaf(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert {series.label for series in view.series} == {"sleep", "social", "media"}

    def test_values_match_the_daily_rollups(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        by_label = {series.label: series.values for series in view.series}
        # The overnight sleep run: one hour on the 5th, seven on the 6th.
        assert by_label["sleep"] == [HOUR, 7 * HOUR]
        assert by_label["social"] == [0, 679 + 3600]
        assert view.total_seconds == 36_000

    def test_empty_days_inside_the_range_are_kept(self, client: MinmaxClient) -> None:
        # A gap in tracking is information; compressing it away would claim the
        # surrounding days were adjacent.
        view = client.trends("timecard", since=date(2026, 6, 4), until=date(2026, 6, 8))
        assert len(view.columns) == 5
        sleep = next(s for s in view.series if s.label == "sleep")
        assert sleep.values == [0, HOUR, 7 * HOUR, 0, 0]

    def test_peak_day_is_reported_for_the_axis(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert view.peak_daily_seconds == 32_400

    def test_series_are_ordered_largest_first(self, client: MinmaxClient) -> None:
        totals = [series.total_seconds for series in client.trends("timecard").series]
        assert totals == sorted(totals, reverse=True)

    def test_an_empty_stream_yields_an_empty_view(self, repository: Repository) -> None:
        from minmax.core.models import StreamKind

        repository.ensure_stream("empty", "Empty", StreamKind.INTERVAL)
        view = InProcessClient(repository, timezone=PACIFIC).trends("empty")
        assert view.is_empty

    def test_an_inverted_range_is_rejected(self, client: MinmaxClient) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            client.trends("timecard", since=date(2026, 6, 6), until=date(2026, 6, 5))


class TestSeriesCapping:
    def test_the_tail_folds_into_a_neutral_other(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", max_series=2)
        assert len(view.series) == 3
        other = view.series[-1]
        assert other.is_other
        assert other.swatch == palette.OTHER
        assert other.label.startswith("Other")

    def test_folding_preserves_the_total(self, client: MinmaxClient) -> None:
        capped = client.trends("timecard", max_series=1)
        full = client.trends("timecard")
        assert capped.total_seconds == full.total_seconds

    def test_never_more_than_eight_hues_on_screen(self, client: MinmaxClient) -> None:
        # Past eight, adjacent categorical hues stop being distinguishable under
        # colour-vision deficiency.
        view = client.trends("timecard")
        hues = [s for s in view.series if not s.is_other]
        assert len(hues) <= 8

    def test_colours_do_not_shift_when_the_series_count_changes(self, client: MinmaxClient) -> None:
        # "Colour follows the entity, never its rank."
        full = {s.label: s.swatch for s in client.trends("timecard").series}
        capped = {
            s.label: s.swatch
            for s in client.trends("timecard", max_series=2).series
            if not s.is_other
        }
        for label, swatch in capped.items():
            assert full[label] == swatch


class TestDrillDown:
    def test_a_prefix_splits_a_group_into_its_children(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", prefix="media")
        assert [series.label for series in view.series] == ["media/gaming"]

    def test_drillable_groups_are_flagged(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        by_label = {series.label: series.can_drill for series in view.series}
        assert by_label["media"] is True

    def test_a_focus_only_counts_its_own_subtree(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", prefix="media")
        assert view.total_seconds == 2921

    def test_focus_colours_are_stable_regardless_of_ranking(self, client: MinmaxClient) -> None:
        first = {s.label: s.swatch for s in client.trends("timecard", prefix="media").series}
        second = {s.label: s.swatch for s in client.trends("timecard", prefix="media").series}
        assert first == second


class TestGranularity:
    def test_a_short_range_stays_daily_under_auto(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert view.interval.unit is Granularity.DAY
        assert view.requested_interval.unit is Granularity.AUTO

    def test_a_long_range_becomes_weekly_under_auto(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", since=date(2026, 1, 1), until=date(2026, 12, 31))
        assert view.interval.unit is Granularity.WEEK
        assert len(view.columns) == 53

    def test_a_very_long_range_becomes_monthly_under_auto(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", since=date(2020, 1, 1), until=date(2026, 12, 31))
        assert view.interval.unit is Granularity.MONTH
        assert len(view.columns) == 84

    def test_an_explicit_granularity_overrides_the_span(self, client: MinmaxClient) -> None:
        view = client.trends(
            "timecard",
            since=date(2020, 1, 1),
            until=date(2026, 12, 31),
            interval=Granularity.WEEK,
        )
        assert view.interval.unit is Granularity.WEEK

    def test_weekly_columns_sum_the_days_they_cover(self, client: MinmaxClient) -> None:
        # Both sample days fall in the same ISO week, so the weekly view must
        # hold exactly what the daily one held, in one column.
        daily = client.trends("timecard", interval=Granularity.DAY)
        weekly = client.trends("timecard", interval=Granularity.WEEK)

        assert len(weekly.columns) == 1
        assert weekly.total_seconds == daily.total_seconds
        by_label = {series.label: series for series in weekly.series}
        assert by_label["sleep"].values == [8 * HOUR]

    def test_aggregating_never_changes_the_grand_total(self, client: MinmaxClient) -> None:
        totals = {
            granularity: client.trends(
                "timecard",
                since=date(2026, 6, 1),
                until=date(2026, 8, 31),
                interval=granularity,
            ).total_seconds
            for granularity in (Granularity.DAY, Granularity.WEEK, Granularity.MONTH)
        }
        assert len(set(totals.values())) == 1


class TestDailyAverages:
    def test_the_axis_ceiling_is_a_daily_average_not_a_column_total(
        self, client: MinmaxClient
    ) -> None:
        # Plotting weekly totals would rescale the y axis by seven and make the
        # same data look like a different life.
        daily = client.trends("timecard", interval=Granularity.DAY)
        # A whole calendar week, so the column is not clipped to the data.
        weekly = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 7),
            interval=Granularity.WEEK,
        )

        assert daily.peak_daily_seconds == 32_400  # the 6th, 9 hours
        # One week holding 10h over 7 days averages well under any single day.
        assert weekly.columns[0].day_count == 7
        assert weekly.peak_daily_seconds == round(36_000 / 7)

    def test_daily_average_divides_by_the_days_the_column_covers(
        self, client: MinmaxClient
    ) -> None:
        weekly = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 7),
            interval=Granularity.WEEK,
        )
        sleep = next(i for i, s in enumerate(weekly.series) if s.label == "sleep")
        assert weekly.daily_average(sleep, 0) == pytest.approx(8 * HOUR / 7)

    def test_a_clipped_column_averages_over_its_real_length(self, client: MinmaxClient) -> None:
        # A half-week at the edge of a range must not read as a collapse in
        # activity just because it holds fewer days.
        weekly = client.trends(
            "timecard",
            since=date(2026, 6, 5),
            until=date(2026, 6, 6),
            interval=Granularity.WEEK,
        )
        assert weekly.columns[0].day_count == 2
        assert weekly.columns[0].clipped
        assert weekly.peak_daily_seconds == round(36_000 / 2)


class TestColumnLabels:
    def test_a_daily_column_names_its_weekday(self, client: MinmaxClient) -> None:
        column = client.trends("timecard", interval=Granularity.DAY).columns[0]
        assert column.axis_label == "5 Jun"
        assert column.detail_label == "Friday 5 Jun 2026"

    def test_a_weekly_column_names_its_span(self, client: MinmaxClient) -> None:
        column = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 14),
            interval=Granularity.WEEK,
        ).columns[0]
        assert column.axis_label == "1 Jun"
        # The month is not repeated when both ends share it: saying June
        # twice in one label serves no purpose.
        assert column.detail_label == "1 – 7 Jun 2026"  # noqa: RUF001

    def test_a_monthly_column_carries_its_year(self, client: MinmaxClient) -> None:
        # "Jun" alone is ambiguous once the chart spans more than a year.
        column = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 30),
            interval=Granularity.MONTH,
        ).columns[0]
        assert column.axis_label == "Jun 26"
        assert column.detail_label == "June 2026"

    def test_a_multi_day_column_names_the_span_it_covers(self, client: MinmaxClient) -> None:
        column = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 30),
            interval=Interval(Granularity.DAY, 3),
        ).columns[1]
        # Anchored, not counted from the range: 4 Jun is a whole number of
        # three-day periods from the epoch, so that is where the column starts.
        assert column.detail_label == "4 – 6 Jun 2026"  # noqa: RUF001

    def test_a_span_crossing_a_month_repeats_the_month(self, client: MinmaxClient) -> None:
        column = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 8, 31),
            interval=Interval(Granularity.WEEK, 2),
        ).columns[2]
        assert column.detail_label == "22 Jun – 5 Jul 2026"  # noqa: RUF001

    def test_a_quarter_names_its_months(self, client: MinmaxClient) -> None:
        column = client.trends(
            "timecard",
            since=date(2026, 1, 1),
            until=date(2026, 12, 31),
            interval=Interval(Granularity.MONTH, 3),
        ).columns[0]
        assert column.detail_label == "January – March 2026"  # noqa: RUF001
        # Not "Jan 26": a month name reads as a complete answer, so a quarter
        # labelled with only its first month claims to be one month long.
        assert column.axis_label == "Jan–Mar 26"  # noqa: RUF001  # noqa: RUF001

    def test_a_span_clipped_to_one_day_is_named_as_that_day(self, client: MinmaxClient) -> None:
        # Naming a single clipped day as a span would be absurd, and naming it
        # after the week it came from would misstate what is plotted.
        column = client.trends(
            "timecard",
            since=date(2026, 6, 7),
            until=date(2026, 6, 30),
            interval=Granularity.WEEK,
        ).columns[0]
        assert column.day_count == 1
        assert column.detail_label == "Sunday 7 Jun 2026"


class TestCustomIntervals:
    def test_a_fortnight_sums_the_days_it_covers(self, client: MinmaxClient) -> None:
        daily = client.trends("timecard", interval=Granularity.DAY)
        fortnightly = client.trends("timecard", interval=Interval(Granularity.WEEK, 2))
        assert fortnightly.total_seconds == daily.total_seconds
        assert len(fortnightly.columns) < len(daily.columns)

    def test_the_grand_total_survives_any_count(self, client: MinmaxClient) -> None:
        totals = {
            count: client.trends(
                "timecard",
                since=date(2026, 6, 1),
                until=date(2026, 8, 31),
                interval=Interval(Granularity.DAY, count),
            ).total_seconds
            for count in (1, 2, 3, 5, 12)
        }
        assert len(set(totals.values())) == 1

    def test_the_average_divides_by_the_real_length(self, client: MinmaxClient) -> None:
        # The plotted height must stay in hours-per-day whatever the count, or
        # widening the interval would look like the data changed.
        view = client.trends(
            "timecard",
            since=date(2026, 6, 1),
            until=date(2026, 6, 28),
            interval=Interval(Granularity.WEEK, 2),
        )
        column = view.columns[0]
        assert view.daily_average(0, 0) == view.series[0].values[0] / column.day_count

    def test_the_view_reports_what_was_asked_for(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", interval=Interval(Granularity.DAY, 3))
        assert view.requested_interval == Interval(Granularity.DAY, 3)
        assert view.interval == Interval(Granularity.DAY, 3)

    def test_auto_still_resolves_to_a_count_of_one(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert view.requested_interval.is_auto
        assert view.interval.count == 1


class TestPinnedRange:
    def test_since_and_until_restrict_the_columns(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", since=date(2026, 6, 6), until=date(2026, 6, 6))
        assert len(view.columns) == 1
        assert view.since == date(2026, 6, 6)
        assert view.until == date(2026, 6, 6)

    def test_a_pinned_range_excludes_data_outside_it(self, client: MinmaxClient) -> None:
        view = client.trends("timecard", since=date(2026, 6, 6), until=date(2026, 6, 6))
        # The 5th's hour of sleep is outside the range and must not be counted.
        assert view.total_seconds == 9 * HOUR

    def test_the_default_range_is_everything(self, client: MinmaxClient) -> None:
        view = client.trends("timecard")
        assert view.since == date(2026, 6, 5)
        assert view.until == date(2026, 6, 6)

    def test_an_inverted_range_is_rejected(self, client: MinmaxClient) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            client.trends("timecard", since=date(2026, 6, 8), until=date(2026, 6, 1))
