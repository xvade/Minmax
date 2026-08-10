"""Tests for :mod:`minmax.core.measures`.

The one invariant worth a hand-computed fixture: a multi-day mean is the
value-weighted mean of the days' own sums and counts, never the mean of the
days' own means. Getting this wrong is exactly the trap ``field_rollup``'s
schema comment warns about, and it produces a plausible-looking wrong number
rather than a crash, so it needs a test that would actually catch it.
"""

from __future__ import annotations

from datetime import date

import pytest

from minmax.core.measures import Aggregate, DailyValue, Measure, MeasureKind


def day(n: int) -> date:
    return date(2026, 6, n)


class TestAggregateExtensive:
    def test_only_sum_is_extensive(self) -> None:
        assert Aggregate.SUM.extensive
        assert not Aggregate.MEAN.extensive
        assert not Aggregate.MIN.extensive
        assert not Aggregate.MAX.extensive


class TestMeasureValidation:
    def test_a_field_measure_requires_a_field_key(self) -> None:
        with pytest.raises(ValueError, match="field_key"):
            Measure(stream_key="heart_rate", kind=MeasureKind.FIELD)

    def test_a_duration_measure_forbids_a_field_key(self) -> None:
        with pytest.raises(ValueError, match="field_key"):
            Measure(stream_key="timecard", kind=MeasureKind.DURATION, field_key="bpm")

    def test_a_count_measure_forbids_a_field_key(self) -> None:
        with pytest.raises(ValueError, match="field_key"):
            Measure(stream_key="pickups", kind=MeasureKind.COUNT, field_key="bpm")

    def test_a_duration_measure_must_use_sum(self) -> None:
        with pytest.raises(ValueError, match=r"Aggregate\.SUM"):
            Measure(stream_key="timecard", kind=MeasureKind.DURATION, aggregate=Aggregate.MEAN)

    def test_a_count_measure_must_use_sum(self) -> None:
        with pytest.raises(ValueError, match=r"Aggregate\.SUM"):
            Measure(stream_key="pickups", kind=MeasureKind.COUNT, aggregate=Aggregate.MAX)

    def test_a_field_measure_accepts_any_aggregate(self) -> None:
        for aggregate in Aggregate:
            Measure(
                stream_key="heart_rate",
                kind=MeasureKind.FIELD,
                field_key="bpm",
                aggregate=aggregate,
            )


class TestMeasureKeyAndLabel:
    def test_whole_stream_duration(self) -> None:
        measure = Measure(stream_key="timecard", kind=MeasureKind.DURATION)
        assert measure.key == "timecard:duration"
        assert measure.label == "timecard duration"

    def test_category_duration(self) -> None:
        measure = Measure(stream_key="timecard", kind=MeasureKind.DURATION, category_path="media")
        assert measure.key == "timecard:duration:media"
        assert measure.label == "media duration"

    def test_whole_stream_count(self) -> None:
        measure = Measure(stream_key="pickups", kind=MeasureKind.COUNT)
        assert measure.key == "pickups:count"
        assert measure.label == "pickups count"

    def test_field_mean(self) -> None:
        measure = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MEAN,
        )
        assert measure.key == "heart_rate:field:bpm:mean"
        assert measure.label == "heart_rate bpm mean"

    def test_field_with_a_category_puts_the_category_last(self) -> None:
        measure = Measure(
            stream_key="location",
            kind=MeasureKind.FIELD,
            field_key="speed",
            aggregate=Aggregate.SUM,
            category_path="car",
        )
        assert measure.key == "location:field:speed:sum:car"
        assert measure.label == "car speed sum"

    def test_two_streams_sharing_a_field_key_produce_different_keys(self) -> None:
        a = Measure(stream_key="heart_rate", kind=MeasureKind.FIELD, field_key="value")
        b = Measure(stream_key="weight", kind=MeasureKind.FIELD, field_key="value")
        assert a.key != b.key


class TestSummableAcrossPeriodsAndZeroWhenAbsent:
    def test_duration_and_count_are_summable_and_zero_filled(self) -> None:
        for kind in (MeasureKind.DURATION, MeasureKind.COUNT):
            measure = Measure(stream_key="s", kind=kind)
            assert measure.summable_across_periods
            assert measure.zero_when_absent

    def test_field_sum_is_summable_but_not_zero_filled(self) -> None:
        measure = Measure(
            stream_key="s", kind=MeasureKind.FIELD, field_key="v", aggregate=Aggregate.SUM
        )
        assert measure.summable_across_periods
        assert not measure.zero_when_absent

    def test_field_mean_min_max_are_neither(self) -> None:
        for aggregate in (Aggregate.MEAN, Aggregate.MIN, Aggregate.MAX):
            measure = Measure(
                stream_key="s", kind=MeasureKind.FIELD, field_key="v", aggregate=aggregate
            )
            assert not measure.summable_across_periods
            assert not measure.zero_when_absent


class TestReduce:
    def test_sum_adds_totals(self) -> None:
        measure = Measure(stream_key="timecard", kind=MeasureKind.DURATION)
        values = [DailyValue(day(1), total=3600.0), DailyValue(day(2), total=1800.0)]
        assert measure.reduce(values) == 5400.0

    def test_sum_of_no_days_is_zero(self) -> None:
        measure = Measure(stream_key="timecard", kind=MeasureKind.DURATION)
        assert measure.reduce([]) == 0.0

    def test_mean_is_value_weighted_not_a_mean_of_daily_means(self) -> None:
        """The trap this module exists to prevent, worked by hand.

        Day A: two readings, 60 and 80 (sum 140, count 2, mean 70).
        Day B: one reading, 100 (sum 100, count 1, mean 100).

        The correct two-day mean weighs by how many readings each day had:
        (140 + 100) / (2 + 1) = 240 / 3 = 80.0.

        The wrong answer -- averaging the two days' own means, (70 + 100) / 2
        = 85.0 -- is a different, larger number, so this test would catch the
        bug rather than pass by coincidence either way.
        """
        measure = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MEAN,
        )
        values = [
            DailyValue(day(1), total=140.0, weight=2.0),
            DailyValue(day(2), total=100.0, weight=1.0),
        ]
        result = measure.reduce(values)
        assert result == pytest.approx(80.0)
        assert result != pytest.approx(85.0)

    def test_mean_with_no_weight_is_none(self) -> None:
        measure = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MEAN,
        )
        assert measure.reduce([]) is None
        assert measure.reduce([DailyValue(day(1), total=0.0, weight=0.0)]) is None

    def test_min_and_max_fold_the_extremes(self) -> None:
        low = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MIN,
        )
        high = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MAX,
        )
        values = [
            DailyValue(day(1), total=140.0, weight=2.0, low=60.0, high=80.0),
            DailyValue(day(2), total=100.0, weight=1.0, low=100.0, high=100.0),
        ]
        assert low.reduce(values) == 60.0
        assert high.reduce(values) == 100.0

    def test_min_and_max_with_no_extremes_are_none(self) -> None:
        measure = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MIN,
        )
        assert measure.reduce([]) is None
        assert (
            measure.reduce([DailyValue(day(1), total=0.0, weight=0.0, low=None, high=None)]) is None
        )
