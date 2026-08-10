"""Tests for :mod:`minmax.analysis.series`.

One case per measure kind against hand-written rollup rows, plus the trap
this module exists to prevent: an interval stream's ``record_count`` (which
double-counts a run split across buckets) must never reach a duration
measure, and no ``COUNT`` measure can even be constructed against an interval
stream in the first place.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from minmax.analysis.series import SeriesLoader
from minmax.core.measures import Aggregate, Measure, MeasureKind
from minmax.core.models import EventRecord, FieldRole, FieldSpec, IntervalRecord, Stream, StreamKind
from minmax.core.timeref import Instant
from minmax.storage.repository import Repository


def at(text: str) -> Instant:
    return Instant.from_iso(text)


def interval(path: str, start: str, end: str) -> IntervalRecord:
    return IntervalRecord(category_source_id=path, category_path=path, start=at(start), end=at(end))


def event(path: str, when: str, values: dict[str, float | str] | None = None) -> EventRecord:
    return EventRecord(
        category_source_id=path, category_path=path, at=at(when), values=values or {}
    )


def begin(repository: Repository, stream_id: int, salt: str) -> int:
    return repository.begin_import(
        stream_id, importer="test", source_name="t.csv", source_sha256=salt * 64, archived_path=None
    )


class TestDurationMeasure:
    def test_reads_seconds_per_day(self, repository: Repository, home: Path) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "a")
        repository.insert_intervals(
            stream,
            run,
            [
                interval("media", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                interval("media", "2026-06-07T09:00:00-07:00", "2026-06-07T09:30:00-07:00"),
            ],
        )

        loader = SeriesLoader(repository)
        values = loader.daily(Measure(stream_key="timecard", kind=MeasureKind.DURATION))
        by_day = {v.day: v.total for v in values}
        assert by_day == {date(2026, 6, 6): 3600.0, date(2026, 6, 7): 1800.0}

    def test_category_path_sums_across_subcategories_only(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "b")
        repository.insert_intervals(
            stream,
            run,
            [
                interval("media/gaming", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                interval("media/reading", "2026-06-06T10:00:00-07:00", "2026-06-06T10:30:00-07:00"),
                interval("work", "2026-06-06T11:00:00-07:00", "2026-06-06T12:00:00-07:00"),
            ],
        )

        loader = SeriesLoader(repository)
        media = loader.daily(
            Measure(stream_key="timecard", kind=MeasureKind.DURATION, category_path="media")
        )
        assert media == [
            v for v in media if v.day == date(2026, 6, 6)
        ]  # sanity: only one day exists
        assert media[0].total == 5400.0  # 3600 + 1800, "work" excluded

    def test_omits_days_with_nothing_in_that_category(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "c")
        repository.insert_intervals(
            stream,
            run,
            [
                interval("media", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                interval("work", "2026-06-07T09:00:00-07:00", "2026-06-07T10:00:00-07:00"),
            ],
        )

        loader = SeriesLoader(repository)
        media = loader.daily(
            Measure(stream_key="timecard", kind=MeasureKind.DURATION, category_path="media")
        )
        assert [v.day for v in media] == [date(2026, 6, 6)]


class TestCountMeasure:
    def test_reads_record_count_per_day(self, repository: Repository, home: Path) -> None:
        stream = repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        assert stream.id is not None
        run = begin(repository, stream.id, "d")
        repository.insert_events(
            stream,
            run,
            [
                event("pickups", "2026-06-06T09:00:00-07:00"),
                event("pickups", "2026-06-06T10:00:00-07:00"),
                event("pickups", "2026-06-07T09:00:00-07:00"),
            ],
        )

        loader = SeriesLoader(repository)
        values = loader.daily(Measure(stream_key="pickups", kind=MeasureKind.COUNT))
        by_day = {v.day: v.total for v in values}
        assert by_day == {date(2026, 6, 6): 2.0, date(2026, 6, 7): 1.0}


class TestFieldMeasure:
    def _heart_rate_stream(self, repository: Repository) -> Stream:
        stream = repository.create_stream(
            "heart_rate",
            "Heart Rate",
            StreamKind.EVENT,
            fields=[FieldSpec(key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm")],
        )
        assert stream.id is not None
        return stream

    def test_mean_is_value_weighted_across_readings(
        self, repository: Repository, home: Path
    ) -> None:
        stream = self._heart_rate_stream(repository)
        assert stream.id is not None
        run = begin(repository, stream.id, "e")
        repository.insert_events(
            stream,
            run,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", {"bpm": 60.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", {"bpm": 80.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", {"bpm": 100.0}),
            ],
        )

        loader = SeriesLoader(repository)
        measure = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.MEAN,
        )
        values = {v.day: measure.reduce([v]) for v in loader.daily(measure)}
        assert values == {date(2026, 6, 6): 70.0, date(2026, 6, 7): 100.0}
        # And the two-day value-weighted mean, not the mean of the daily means:
        assert measure.reduce(loader.daily(measure)) == pytest.approx(240.0 / 3)

    def test_sum_and_min_max(self, repository: Repository, home: Path) -> None:
        stream = self._heart_rate_stream(repository)
        assert stream.id is not None
        run = begin(repository, stream.id, "f")
        repository.insert_events(
            stream,
            run,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", {"bpm": 60.0}),
                event("heart_rate", "2026-06-06T10:00:00-07:00", {"bpm": 80.0}),
            ],
        )

        loader = SeriesLoader(repository)
        total = Measure(
            stream_key="heart_rate",
            kind=MeasureKind.FIELD,
            field_key="bpm",
            aggregate=Aggregate.SUM,
        )
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
        [day_values] = loader.daily(total)
        assert day_values.total == 140.0
        assert day_values.low == 60.0
        assert day_values.high == 80.0
        assert total.reduce([day_values]) == 140.0
        assert low.reduce([day_values]) == 60.0
        assert high.reduce([day_values]) == 80.0

    def test_omits_days_with_no_readings_rather_than_reporting_zero(
        self, repository: Repository, home: Path
    ) -> None:
        stream = self._heart_rate_stream(repository)
        assert stream.id is not None
        run = begin(repository, stream.id, "0")
        repository.insert_events(
            stream,
            run,
            [event("heart_rate", "2026-06-06T09:00:00-07:00", {"bpm": 60.0})],
        )

        loader = SeriesLoader(repository)
        measure = Measure(stream_key="heart_rate", kind=MeasureKind.FIELD, field_key="bpm")
        assert [v.day for v in loader.daily(measure)] == [date(2026, 6, 6)]


class TestActiveDays:
    def test_covers_every_day_the_stream_recorded_anything(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "1")
        repository.insert_intervals(
            stream,
            run,
            [
                interval("media", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                interval("work", "2026-06-08T09:00:00-07:00", "2026-06-08T10:00:00-07:00"),
            ],
        )

        loader = SeriesLoader(repository)
        assert loader.active_days("timecard") == {date(2026, 6, 6), date(2026, 6, 8)}


class TestErrors:
    def test_unknown_stream_raises(self, repository: Repository, home: Path) -> None:
        loader = SeriesLoader(repository)
        with pytest.raises(LookupError, match="no stream"):
            loader.daily(Measure(stream_key="nope", kind=MeasureKind.DURATION))
        with pytest.raises(LookupError, match="no stream"):
            loader.active_days("nope")

    def test_a_count_measure_against_an_interval_stream_is_rejected(
        self, repository: Repository, home: Path
    ) -> None:
        repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        loader = SeriesLoader(repository)
        with pytest.raises(ValueError, match="an event stream"):
            loader.daily(Measure(stream_key="timecard", kind=MeasureKind.COUNT))

    def test_a_duration_measure_against_an_event_stream_is_rejected(
        self, repository: Repository, home: Path
    ) -> None:
        repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        loader = SeriesLoader(repository)
        with pytest.raises(ValueError, match="an interval stream"):
            loader.daily(Measure(stream_key="pickups", kind=MeasureKind.DURATION))


class TestMidnightCrossingTrap:
    """The reason `COUNT` is refused for interval streams at all: a single
    run spanning two local days touches two rollup buckets, each with
    ``record_count = 1`` -- summing that across days would report "2" for a
    single run, which is exactly the trap `analysis/` must never fall into.
    """

    def test_duration_reflects_the_true_split_not_an_inflated_count(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "2")
        # One run, 23:00 to 01:00: one hour in each of two local days.
        repository.insert_intervals(
            stream,
            run,
            [interval("sleep", "2026-06-06T23:00:00-07:00", "2026-06-07T01:00:00-07:00")],
        )

        loader = SeriesLoader(repository)
        values = loader.daily(Measure(stream_key="timecard", kind=MeasureKind.DURATION))
        total_seconds = sum(v.total for v in values)
        assert total_seconds == 7200.0  # exactly the run's true duration, not doubled

        # And COUNT is not even constructible against this stream, so there
        # is no path by which the inflated per-bucket record_count (1 + 1)
        # could reach a caller as if it meant "two events".
        with pytest.raises(ValueError, match="an event stream"):
            loader.daily(Measure(stream_key="timecard", kind=MeasureKind.COUNT))


class TestQueryCount:
    def test_repeated_measures_over_one_stream_reuse_the_cached_load(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.create_stream(
            "heart_rate",
            "Heart Rate",
            StreamKind.EVENT,
            fields=[FieldSpec(key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm")],
        )
        assert stream.id is not None
        run = begin(repository, stream.id, "3")
        repository.insert_events(
            stream,
            run,
            [
                event("heart_rate", "2026-06-06T09:00:00-07:00", {"bpm": 60.0}),
                event("heart_rate", "2026-06-07T09:00:00-07:00", {"bpm": 70.0}),
            ],
        )

        queries: list[str] = []
        repository.connection.set_trace_callback(lambda sql: queries.append(sql))
        try:
            loader = SeriesLoader(repository)
            loader.daily(
                Measure(
                    stream_key="heart_rate",
                    kind=MeasureKind.FIELD,
                    field_key="bpm",
                    aggregate=Aggregate.MEAN,
                )
            )
            after_first_measure = len(queries)
            assert after_first_measure <= 3  # get_stream + 2 rollup reads, at most

            loader.daily(
                Measure(
                    stream_key="heart_rate",
                    kind=MeasureKind.FIELD,
                    field_key="bpm",
                    aggregate=Aggregate.SUM,
                )
            )
            loader.active_days("heart_rate")
            assert len(queries) == after_first_measure  # the second and third cost nothing new
        finally:
            repository.connection.set_trace_callback(None)
