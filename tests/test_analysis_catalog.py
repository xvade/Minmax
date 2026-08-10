"""Tests for :mod:`minmax.analysis.catalog`."""

from __future__ import annotations

from pathlib import Path

import pytest

from minmax.analysis.catalog import candidate_measures, candidate_pairs
from minmax.core.measures import Aggregate, Measure, MeasureKind
from minmax.core.models import EventRecord, FieldRole, FieldSpec, IntervalRecord, StreamKind
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


@pytest.fixture
def timecard(repository: Repository, home: Path) -> None:
    stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL, exclusive=True)
    assert stream.id is not None
    run = begin(repository, stream.id, "a")
    repository.insert_intervals(
        stream,
        run,
        [
            interval("media/gaming", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00"),
            interval("work", "2026-06-06T11:00:00-07:00", "2026-06-06T12:00:00-07:00"),
        ],
    )


@pytest.fixture
def heart_rate(repository: Repository, home: Path) -> None:
    stream = repository.create_stream(
        "heart_rate",
        "Heart Rate",
        StreamKind.EVENT,
        fields=[FieldSpec(key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm")],
    )
    assert stream.id is not None
    run = begin(repository, stream.id, "b")
    repository.insert_events(
        stream, run, [event("heart_rate", "2026-06-06T09:00:00-07:00", {"bpm": 60.0})]
    )


class TestCandidateMeasures:
    def test_a_fresh_interval_stream_yields_only_the_grand_total_by_default(
        self, repository: Repository, timecard: None
    ) -> None:
        measures = candidate_measures(repository)
        assert measures == [Measure(stream_key="timecard", kind=MeasureKind.DURATION)]

    def test_per_category_adds_top_level_groups_but_not_leaves(
        self, repository: Repository, timecard: None
    ) -> None:
        measures = candidate_measures(repository, per_category=True)
        keys = {m.key for m in measures}
        assert "timecard:duration" in keys
        assert "timecard:duration:media" in keys  # top-level, not "media/gaming"
        assert "timecard:duration:work" in keys
        assert "timecard:duration:media/gaming" not in keys

    def test_event_stream_yields_a_count_measure(
        self, repository: Repository, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, include_fields=False)
        assert measures == [Measure(stream_key="heart_rate", kind=MeasureKind.COUNT)]

    def test_numeric_fields_yield_a_mean_measure_by_default(
        self, repository: Repository, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository)
        field_measures = [m for m in measures if m.kind is MeasureKind.FIELD]
        assert field_measures == [
            Measure(
                stream_key="heart_rate",
                kind=MeasureKind.FIELD,
                field_key="bpm",
                aggregate=Aggregate.MEAN,
                unit=None,
            )
        ]

    def test_field_sum_adds_a_sum_variant_alongside_the_mean(
        self, repository: Repository, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, field_sum=True)
        aggregates = {m.aggregate for m in measures if m.kind is MeasureKind.FIELD}
        assert aggregates == {Aggregate.MEAN, Aggregate.SUM}

    def test_no_fields_excludes_field_measures_entirely(
        self, repository: Repository, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, include_fields=False)
        assert not any(m.kind is MeasureKind.FIELD for m in measures)

    def test_stream_keys_restricts_enumeration(
        self, repository: Repository, timecard: None, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, stream_keys=["timecard"])
        assert all(m.stream_key == "timecard" for m in measures)

    def test_no_duplicate_measures(
        self, repository: Repository, timecard: None, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, per_category=True, field_sum=True)
        keys = [m.key for m in measures]
        assert len(keys) == len(set(keys))

    def test_truncates_to_max_series_keeping_grand_totals_first(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "c")
        repository.insert_intervals(
            stream,
            run,
            [
                interval(f"cat{i}", "2026-06-06T09:00:00-07:00", "2026-06-06T10:00:00-07:00")
                for i in range(10)
            ],
        )

        measures = candidate_measures(repository, per_category=True, max_series=3)
        assert len(measures) == 3
        assert Measure(stream_key="timecard", kind=MeasureKind.DURATION) in measures

    def test_truncation_keeps_the_most_used_categories(
        self, repository: Repository, home: Path
    ) -> None:
        stream = repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        assert stream.id is not None
        run = begin(repository, stream.id, "d")
        repository.insert_intervals(
            stream,
            run,
            [
                interval("busy", "2026-06-06T00:00:00-07:00", "2026-06-06T10:00:00-07:00"),
                interval("quiet", "2026-06-06T10:00:00-07:00", "2026-06-06T10:01:00-07:00"),
            ],
        )

        # max_series=2 leaves room for the grand total plus exactly one category.
        measures = candidate_measures(repository, per_category=True, max_series=2)
        category_measures = [m for m in measures if m.category_path is not None]
        assert category_measures == [
            Measure(stream_key="timecard", kind=MeasureKind.DURATION, category_path="busy")
        ]


class TestCandidatePairs:
    def test_a_grand_total_is_not_paired_with_its_own_category(
        self, repository: Repository, timecard: None
    ) -> None:
        measures = candidate_measures(repository, per_category=True)
        pairs = candidate_pairs(repository, measures)
        keys = {(p.x.key, p.y.key) for p in pairs}
        assert ("timecard:duration", "timecard:duration:media") not in keys
        assert ("timecard:duration:media", "timecard:duration") not in keys

    def test_the_same_field_under_two_aggregates_is_not_paired(
        self, repository: Repository, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository, field_sum=True)
        pairs = candidate_pairs(repository, measures)
        field_keys = {m.key for m in measures if m.kind is MeasureKind.FIELD}
        assert len(field_keys) == 2  # mean and sum
        for pair in pairs:
            assert not (pair.x.key in field_keys and pair.y.key in field_keys)

    def test_two_top_level_categories_in_the_same_stream_are_paired(
        self, repository: Repository, timecard: None
    ) -> None:
        measures = candidate_measures(repository, per_category=True)
        pairs = candidate_pairs(repository, measures)
        keys = {frozenset((p.x.key, p.y.key)) for p in pairs}
        assert frozenset(("timecard:duration:media", "timecard:duration:work")) in keys

    def test_cross_stream_pairs_are_kept(
        self, repository: Repository, timecard: None, heart_rate: None
    ) -> None:
        measures = candidate_measures(repository)
        pairs = candidate_pairs(repository, measures)
        keys = {frozenset((p.x.stream_key, p.y.stream_key)) for p in pairs}
        assert frozenset(("timecard", "heart_rate")) in keys

    def test_two_categories_of_an_exclusive_stream_carry_a_caveat(
        self, repository: Repository, timecard: None
    ) -> None:
        measures = candidate_measures(repository, per_category=True)
        pairs = candidate_pairs(repository, measures)
        [pair] = [p for p in pairs if {p.x.category_path, p.y.category_path} == {"media", "work"}]
        assert pair.caveats
        assert "exclusive" in pair.caveats[0]

    def test_a_grand_total_pair_across_two_non_exclusive_streams_has_no_caveat(
        self, repository: Repository, heart_rate: None, home: Path
    ) -> None:
        repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        measures = candidate_measures(repository, stream_keys=["heart_rate", "pickups"])
        pairs = candidate_pairs(repository, measures)
        assert pairs and all(pair.caveats == () for pair in pairs)
