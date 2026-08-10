"""Tests for grouping a date range into calendar-aligned chart columns.

The properties that matter are coverage and alignment: every day in the range
must land in exactly one column, no column may reach outside the range, and the
same date must always fall in the same week or month regardless of where the
range happens to start. A chart built on a grouping that fails any of these is
wrong in a way no amount of correct painting can rescue.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from minmax.core.columns import (
    Column,
    Granularity,
    Interval,
    choose_granularity,
    columns_for,
)


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


class TestDailyColumns:
    def test_one_column_per_day(self) -> None:
        columns = columns_for(date(2026, 6, 5), date(2026, 6, 8), Granularity.DAY)
        assert [column.start for column in columns] == days_between(
            date(2026, 6, 5), date(2026, 6, 8)
        )
        assert all(column.day_count == 1 for column in columns)
        assert not any(column.clipped for column in columns)

    def test_a_single_day_is_a_single_column(self) -> None:
        columns = columns_for(date(2026, 6, 5), date(2026, 6, 5), Granularity.DAY)
        assert len(columns) == 1
        assert columns[0].start == columns[0].end == date(2026, 6, 5)


class TestWeeklyColumns:
    def test_weeks_begin_on_monday(self) -> None:
        # 2026-06-05 is a Friday; the week it belongs to began on the 1st.
        columns = columns_for(date(2026, 6, 1), date(2026, 6, 21), Granularity.WEEK)
        assert [column.start for column in columns] == [
            date(2026, 6, 1),
            date(2026, 6, 8),
            date(2026, 6, 15),
        ]
        assert all(column.day_count == 7 for column in columns)

    def test_the_same_day_lands_in_the_same_week_whatever_the_range(self) -> None:
        # Alignment is the whole point: chopping from the range's first day
        # would shift every column whenever the range moved. Checked on the
        # column's *end*, since a range beginning mid-week legitimately clips
        # the start of the first column but must not move the boundary after it.
        target = date(2026, 6, 10)
        for start in (date(2026, 5, 3), date(2026, 6, 1), date(2026, 6, 9)):
            columns = columns_for(start, date(2026, 6, 30), Granularity.WEEK)
            owner = next(column for column in columns if column.contains(target))
            assert owner.end == date(2026, 6, 14)  # the Sunday, every time

    def test_partial_weeks_at_the_edges_are_clipped_and_flagged(self) -> None:
        # A column may not report days the reader did not ask to see.
        columns = columns_for(date(2026, 6, 3), date(2026, 6, 17), Granularity.WEEK)
        assert columns[0].start == date(2026, 6, 3)  # not the Monday, the 1st
        assert columns[0].clipped
        assert columns[-1].end == date(2026, 6, 17)
        assert columns[-1].clipped
        assert not columns[1].clipped


class TestMonthlyColumns:
    def test_months_begin_on_the_first(self) -> None:
        columns = columns_for(date(2026, 1, 1), date(2026, 3, 31), Granularity.MONTH)
        assert [column.start for column in columns] == [
            date(2026, 1, 1),
            date(2026, 2, 1),
            date(2026, 3, 1),
        ]
        assert [column.day_count for column in columns] == [31, 28, 31]

    def test_december_rolls_into_january(self) -> None:
        columns = columns_for(date(2026, 12, 1), date(2027, 1, 31), Granularity.MONTH)
        assert [column.start for column in columns] == [date(2026, 12, 1), date(2027, 1, 1)]

    def test_a_leap_february_is_twenty_nine_days(self) -> None:
        columns = columns_for(date(2028, 2, 1), date(2028, 2, 29), Granularity.MONTH)
        assert columns[0].day_count == 29


class TestCoverage:
    @pytest.mark.parametrize("granularity", [Granularity.DAY, Granularity.WEEK, Granularity.MONTH])
    def test_every_day_lands_in_exactly_one_column(self, granularity: Granularity) -> None:
        first, last = date(2026, 1, 15), date(2026, 7, 4)
        columns = columns_for(first, last, granularity)

        seen: list[date] = []
        for column in columns:
            seen.extend(column.days())
        assert seen == days_between(first, last)

    @pytest.mark.parametrize("granularity", [Granularity.DAY, Granularity.WEEK, Granularity.MONTH])
    def test_no_column_reaches_outside_the_range(self, granularity: Granularity) -> None:
        first, last = date(2026, 3, 12), date(2026, 9, 3)
        for column in columns_for(first, last, granularity):
            assert first <= column.start <= column.end <= last


class TestAutoGranularity:
    def test_short_spans_stay_daily(self) -> None:
        assert choose_granularity(1) is Granularity.DAY
        assert choose_granularity(120) is Granularity.DAY

    def test_medium_spans_become_weekly(self) -> None:
        assert choose_granularity(121) is Granularity.WEEK
        assert choose_granularity(800) is Granularity.WEEK

    def test_long_spans_become_monthly(self) -> None:
        assert choose_granularity(801) is Granularity.MONTH
        assert choose_granularity(10_000) is Granularity.MONTH

    def test_auto_resolves_before_building_columns(self) -> None:
        columns = columns_for(date(2020, 1, 1), date(2026, 1, 1), Granularity.AUTO)
        assert all(column.granularity is Granularity.MONTH for column in columns)
        assert len(columns) == 73  # six years and a day

    def test_a_resolved_granularity_is_always_reported(self) -> None:
        for span in (10, 400, 4000):
            columns = columns_for(
                date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=span - 1), Granularity.AUTO
            )
            assert columns[0].granularity is not Granularity.AUTO


class TestMultiUnitIntervals:
    """A count above one keeps every property a count of one has, plus anchoring."""

    def test_a_count_of_days_gives_columns_of_that_length(self) -> None:
        columns = columns_for(date(2026, 6, 1), date(2026, 6, 15), Interval(Granularity.DAY, 5))
        assert [column.day_count for column in columns] == [4, 5, 5, 1]

    def test_seven_days_lands_where_one_week_does(self) -> None:
        # The anchor is a Monday precisely so these agree. If they ever diverge,
        # a reader switching between them would see the data shift sideways.
        first, last = date(2026, 1, 1), date(2026, 12, 31)
        weekly = columns_for(first, last, Interval(Granularity.WEEK))
        seven_days = columns_for(first, last, Interval(Granularity.DAY, 7))
        assert [(c.start, c.end) for c in weekly] == [(c.start, c.end) for c in seven_days]

    def test_three_months_are_calendar_quarters(self) -> None:
        columns = columns_for(date(2026, 1, 1), date(2026, 12, 31), Interval(Granularity.MONTH, 3))
        assert [column.start for column in columns] == [
            date(2026, 1, 1),
            date(2026, 4, 1),
            date(2026, 7, 1),
            date(2026, 10, 1),
        ]
        assert columns[0].end == date(2026, 3, 31)

    def test_twelve_months_are_calendar_years(self) -> None:
        columns = columns_for(date(2024, 1, 1), date(2026, 12, 31), Interval(Granularity.MONTH, 12))
        assert [(c.start, c.end) for c in columns] == [
            (date(2024, 1, 1), date(2024, 12, 31)),
            (date(2025, 1, 1), date(2025, 12, 31)),
            (date(2026, 1, 1), date(2026, 12, 31)),
        ]

    @pytest.mark.parametrize("unit", [Granularity.DAY, Granularity.WEEK, Granularity.MONTH])
    def test_a_day_lands_in_the_same_column_whatever_the_range(self, unit: Granularity) -> None:
        # The whole point of anchoring: a fortnight column that started wherever
        # the range started would be a different fortnight on every chart.
        day = date(2026, 6, 17)
        spans = [
            columns_for(start, date(2026, 12, 31), Interval(unit, 2))
            for start in (date(2026, 1, 1), date(2026, 2, 14), date(2026, 3, 9))
        ]
        ends = {next(c for c in columns if c.contains(day)).end for columns in spans}
        assert len(ends) == 1

    @pytest.mark.parametrize("unit", [Granularity.DAY, Granularity.WEEK, Granularity.MONTH])
    @pytest.mark.parametrize("count", [1, 2, 3, 5, 12])
    def test_every_day_still_lands_in_exactly_one_column(
        self, unit: Granularity, count: int
    ) -> None:
        first, last = date(2025, 11, 3), date(2027, 2, 8)
        columns = columns_for(first, last, Interval(unit, count))
        covered = [day for column in columns for day in column.days()]
        assert covered == days_between(first, last)

    @pytest.mark.parametrize("unit", [Granularity.DAY, Granularity.WEEK, Granularity.MONTH])
    @pytest.mark.parametrize("count", [1, 2, 3, 5, 12])
    def test_no_column_reaches_outside_the_range(self, unit: Granularity, count: int) -> None:
        first, last = date(2025, 11, 3), date(2027, 2, 8)
        for column in columns_for(first, last, Interval(unit, count)):
            assert first <= column.start <= column.end <= last

    def test_dates_before_the_anchor_still_align(self) -> None:
        # Floor division has to round toward the past here, or a pre-1970 day
        # would land in the period that starts after it.
        columns = columns_for(date(1965, 3, 1), date(1965, 3, 31), Interval(Granularity.DAY, 3))
        for column in columns:
            assert column.start <= column.end


class TestIntervalRules:
    def test_a_bare_granularity_means_a_count_of_one(self) -> None:
        assert Interval.coerce(Granularity.WEEK) == Interval(Granularity.WEEK, 1)
        assert Interval.coerce(Interval(Granularity.WEEK, 3)).count == 3

    def test_the_label_names_the_span(self) -> None:
        assert Interval(Granularity.WEEK).label == "week"
        assert Interval(Granularity.WEEK, 2).label == "2 weeks"
        assert Interval(Granularity.DAY, 5).label == "5 days"

    def test_auto_resolves_against_the_span(self) -> None:
        assert Interval(Granularity.AUTO).resolved(30).unit is Granularity.DAY
        assert Interval(Granularity.AUTO).resolved(10_000).unit is Granularity.MONTH

    def test_a_resolved_interval_is_left_alone(self) -> None:
        fortnight = Interval(Granularity.WEEK, 2)
        assert fortnight.resolved(10_000) is fortnight

    def test_a_count_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            Interval(Granularity.DAY, 0)

    def test_an_absurd_count_is_refused(self) -> None:
        # A slipped keystroke should not produce a column measured in centuries.
        with pytest.raises(ValueError, match="above 12"):
            Interval(Granularity.MONTH, 500)

    def test_auto_cannot_be_multiplied(self) -> None:
        with pytest.raises(ValueError, match="cannot be multiplied"):
            Interval(Granularity.AUTO, 2)


class TestRejections:
    def test_a_backwards_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            columns_for(date(2026, 6, 8), date(2026, 6, 5), Granularity.DAY)

    def test_a_backwards_column_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends before it starts"):
            Column(start=date(2026, 6, 8), end=date(2026, 6, 5), interval=Interval(Granularity.DAY))

    def test_a_column_cannot_hold_an_unresolved_granularity(self) -> None:
        # AUTO is a request, not a fact about a column, and letting one through
        # would leave the view with no way to label it.
        with pytest.raises(ValueError, match="resolved granularity"):
            Column(
                start=date(2026, 6, 5), end=date(2026, 6, 5), interval=Interval(Granularity.AUTO)
            )
