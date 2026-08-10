"""Tests for :mod:`minmax.analysis.sweep`.

The anchoring/alignment guarantee this module leans on entirely -- that two
series bucketed independently at the same interval land on the same period
boundaries -- is the single most load-bearing fact in the whole correlation
engine, so it gets the first and most direct test here.
"""

from __future__ import annotations

from datetime import date

import pytest

from minmax.analysis.sweep import DEFAULT_SWEEP, align, deduplicate_intervals, periodic_values
from minmax.core.columns import Granularity, Interval
from minmax.core.measures import Aggregate, DailyValue, Measure, MeasureKind


def days(start: date, count: int) -> list[date]:
    from datetime import timedelta

    return [start + timedelta(days=n) for n in range(count)]


def duration_measure(category_path: str | None = None) -> Measure:
    return Measure(stream_key="timecard", kind=MeasureKind.DURATION, category_path=category_path)


def field_mean_measure() -> Measure:
    return Measure(
        stream_key="heart_rate", kind=MeasureKind.FIELD, field_key="bpm", aggregate=Aggregate.MEAN
    )


class TestDeduplicateIntervals:
    def test_day_seven_and_week_one_are_the_same_period(self) -> None:
        kept = deduplicate_intervals([Interval(Granularity.DAY, 7), Interval(Granularity.WEEK, 1)])
        assert kept == [Interval(Granularity.DAY, 7)]

    def test_distinct_intervals_all_survive(self) -> None:
        intervals = [
            Interval(Granularity.DAY, 1),
            Interval(Granularity.DAY, 3),
            Interval(Granularity.WEEK, 2),
        ]
        assert deduplicate_intervals(intervals) == intervals

    def test_the_default_sweep_has_no_duplicates(self) -> None:
        assert deduplicate_intervals(list(DEFAULT_SWEEP)) == list(DEFAULT_SWEEP)


class TestAnchoringAndAlignment:
    def test_two_series_over_offset_ranges_still_agree_on_shared_boundaries(self) -> None:
        # Series A: 2026-06-03 (a Wednesday) through 2026-07-20.
        # Series B: 2026-06-17 through 2026-08-09 -- a different start and end.
        # Both bucketed at Interval(WEEK, 1): the weeks they share must be the
        # exact same Mondays, since anchoring does not depend on either
        # series' own range.
        measure = duration_measure()
        active_a = frozenset(days(date(2026, 6, 3), 48))
        daily_a = [DailyValue(day=d, total=3600.0) for d in active_a]
        active_b = frozenset(days(date(2026, 6, 17), 54))
        daily_b = [DailyValue(day=d, total=1800.0) for d in active_b]

        values_a = periodic_values(
            measure, daily_a, Interval(Granularity.WEEK), active_days=active_a
        )
        values_b = periodic_values(
            measure, daily_b, Interval(Granularity.WEEK), active_days=active_b
        )

        assert all(start.weekday() == 0 for start in values_a)  # every period starts on a Monday
        assert all(start.weekday() == 0 for start in values_b)

        _, _, shared = align(values_a, values_b)
        assert shared, "the two ranges overlap enough to share at least one full week"
        assert all(start.weekday() == 0 for start in shared)

    def test_align_returns_only_the_intersection_in_order(self) -> None:
        x = {date(2026, 6, 1): 1.0, date(2026, 6, 8): 2.0, date(2026, 6, 15): 3.0}
        y = {date(2026, 6, 8): 20.0, date(2026, 6, 15): 30.0, date(2026, 6, 22): 40.0}
        xs, ys, shared = align(x, y)
        assert shared == [date(2026, 6, 8), date(2026, 6, 15)]
        assert xs == [2.0, 3.0]
        assert ys == [20.0, 30.0]


class TestClippedColumns:
    def test_a_partial_edge_period_is_dropped(self) -> None:
        # Wednesday through the following Sunday's week-plus is 12 days,
        # which is one clipped week at the start, one full week, and one
        # clipped week at the end.
        measure = duration_measure()
        start = date(2026, 6, 3)  # a Wednesday
        active = frozenset(days(start, 12))
        daily = [DailyValue(day=d, total=3600.0) for d in active]

        values = periodic_values(measure, daily, Interval(Granularity.WEEK), active_days=active)
        # Only the one full Monday-to-Sunday week survives.
        assert len(values) == 1
        [(period_start, total)] = values.items()
        assert period_start.weekday() == 0
        assert total == 7 * 3600.0


class TestMissingVersusZero:
    def test_an_interior_gap_reads_as_zero_for_a_duration_measure(self) -> None:
        measure = duration_measure()
        start = date(2026, 6, 1)  # a Monday
        active = frozenset(days(start, 7)) - {date(2026, 6, 3)}
        daily = [DailyValue(day=d, total=3600.0) for d in active]
        # The stream is still "active" that day for some other reason (e.g. a
        # different category recorded something) -- only this measure has a gap.
        stream_active = frozenset(days(start, 7))

        values = periodic_values(
            measure, daily, Interval(Granularity.WEEK), active_days=stream_active
        )
        assert values[start] == 6 * 3600.0  # 6 days tracked, the 7th counted as zero

    def test_a_field_measure_ignores_days_with_no_reading_rather_than_zero(self) -> None:
        measure = field_mean_measure()
        start = date(2026, 6, 1)
        readings = {date(2026, 6, 1), date(2026, 6, 3)}
        daily = [DailyValue(day=d, total=100.0, weight=1.0) for d in readings]
        active = frozenset(days(start, 7))

        values = periodic_values(measure, daily, Interval(Granularity.WEEK), active_days=active)
        # Two readings of 100, no zero-filled days dragging the mean down.
        assert values[start] == 100.0

    def test_a_whole_column_blackout_is_missing_not_zero(self) -> None:
        measure = duration_measure()
        # The stream is only active in week 1; week 2 is a total blackout,
        # not merely quiet -- it must not appear as a reported zero.
        week1 = date(2026, 6, 1)
        week2 = date(2026, 6, 8)
        active = frozenset(days(week1, 7))
        daily = [DailyValue(day=d, total=3600.0) for d in active]
        # active_days spans both weeks in the caller's window, but the stream
        # itself only ever recorded anything in week 1.
        values = periodic_values(measure, daily, Interval(Granularity.WEEK), active_days=active)
        assert week1 in values
        assert week2 not in values

    def test_a_field_measure_with_no_readings_anywhere_in_a_period_is_missing(self) -> None:
        measure = field_mean_measure()
        start = date(2026, 6, 1)
        daily = [DailyValue(day=start, total=100.0, weight=1.0)]
        active = frozenset(days(start, 14))  # active for two weeks, but only one reading

        values = periodic_values(measure, daily, Interval(Granularity.WEEK), active_days=active)
        assert list(values) == [start]  # the second week has no reading at all


class TestValueWeightedMeanAcrossAWiderPeriod:
    def test_a_week_long_mean_is_re_derived_not_averaged_a_second_time(self) -> None:
        # Day 1: two readings averaging 70 (sum 140, count 2).
        # Day 2: one reading of 100 (sum 100, count 1).
        # Correct week value: (140+100)/(2+1) = 80, not (70+100)/2 = 85.
        measure = field_mean_measure()
        start = date(2026, 6, 1)
        daily = [
            DailyValue(day=start, total=140.0, weight=2.0),
            DailyValue(day=start.replace(day=2), total=100.0, weight=1.0),
        ]
        active = frozenset(days(start, 7))
        values = periodic_values(measure, daily, Interval(Granularity.WEEK), active_days=active)
        assert values[start] == 80.0


class TestRejectsAuto:
    def test_auto_interval_is_rejected(self) -> None:
        measure = duration_measure()
        with pytest.raises(ValueError, match="AUTO"):
            periodic_values(measure, [], Interval(Granularity.AUTO), active_days=frozenset())


class TestEmptyInput:
    def test_no_daily_values_means_no_periods(self) -> None:
        measure = duration_measure()
        assert (
            periodic_values(measure, [], Interval(Granularity.WEEK), active_days=frozenset()) == {}
        )
