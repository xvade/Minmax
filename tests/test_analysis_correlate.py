"""Integration test for :mod:`minmax.analysis.correlate`.

A synthetic three-stream, 84-day fixture built through the real
``Repository.insert_intervals``/``insert_events`` (not hand-written rollup
rows), so the real import path -- category resolution, rollup maintenance,
everything -- is what the engine is actually exercised against.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from minmax.analysis.correlate import correlate
from minmax.core.columns import Granularity, Interval
from minmax.core.models import EventRecord, FieldRole, FieldSpec, IntervalRecord, StreamKind
from minmax.core.timeref import Instant
from minmax.storage.repository import Repository

DAY_COUNT = 84
START = date(2026, 3, 2)  # a Monday


def at_local_midnight_plus(day_index: int, hour: int, minutes: int = 0) -> Instant:
    """``hour:00`` on ``START + day_index`` days, plus ``minutes`` -- via real
    datetime arithmetic (not string-formatted clamping), so a duration that
    grows past 59 minutes correctly rolls into the next hour rather than
    silently capping."""
    day = START + timedelta(days=day_index)
    naive = datetime(day.year, day.month, day.day, hour) + timedelta(minutes=minutes)
    return Instant.from_iso(naive.strftime("%Y-%m-%dT%H:%M:%S") + "-07:00")


def build_three_stream_fixture(repository: Repository) -> None:
    """Stream A and B are exact linear functions of the same day index, so
    they are perfectly correlated at every period length. Stream C is a
    modular ("noise-like") sequence deliberately uncorrelated with either.
    """
    a = repository.ensure_stream("focus", "Focus", StreamKind.INTERVAL)
    b = repository.create_stream("commits", "Commits", StreamKind.EVENT)
    c = repository.create_stream(
        "weight",
        "Weight",
        StreamKind.EVENT,
        fields=[FieldSpec(key="kg", label="kg", role=FieldRole.NUMBER, source_column="kg")],
    )
    assert a.id is not None
    assert b.id is not None
    assert c.id is not None

    run_a = repository.begin_import(
        a.id, importer="test", source_name="a.csv", source_sha256="a" * 64, archived_path=None
    )
    run_b = repository.begin_import(
        b.id, importer="test", source_name="b.csv", source_sha256="b" * 64, archived_path=None
    )
    run_c = repository.begin_import(
        c.id, importer="test", source_name="c.csv", source_sha256="c" * 64, archived_path=None
    )

    intervals = []
    events_b = []
    events_c = []
    for i in range(DAY_COUNT):
        duration_minutes = 20 + i  # linear in i, up to ~1h43m -- comfortably same-day
        start = at_local_midnight_plus(i, 9)
        end = at_local_midnight_plus(i, 9, duration_minutes)
        intervals.append(
            IntervalRecord(category_source_id="work", category_path="work", start=start, end=end)
        )

        commit_count = 2 + i  # linear in i, same slope sign as A
        for n in range(commit_count):
            # A distinct minute per event: a field-less event's natural key
            # is (stream, category, instant, payload_hash), and a constant
            # payload_hash for events with no declared fields means two
            # events landing in the very same second would collapse into
            # one (see docs/architecture.md §6.1) -- exactly what happens if
            # this loop reuses the same hour for more than a handful of events.
            events_b.append(
                EventRecord(
                    category_source_id="repo",
                    category_path="repo",
                    at=at_local_midnight_plus(i, 10, minutes=n),
                )
            )

        noise_kg = 50.0 + (i * 37 % 23)  # deliberately uncorrelated with i
        events_c.append(
            EventRecord(
                category_source_id="me",
                category_path="me",
                at=at_local_midnight_plus(i, 8),
                values={"kg": noise_kg},
            )
        )

    repository.insert_intervals(a, run_a, intervals)
    repository.insert_events(b, run_b, events_b)
    repository.insert_events(c, run_c, events_c)


class TestSyntheticThreeStreamFixture:
    def test_the_perfectly_linear_pair_ranks_first_with_r_near_one(
        self, repository: Repository, home: Path
    ) -> None:
        build_three_stream_fixture(repository)
        report = correlate(repository, min_r=0.5, top=None)

        assert report.pairs, "expected at least the focus/commits pair to survive"
        top_pair = report.pairs[0]
        keys = {top_pair.best.x.stream_key, top_pair.best.y.stream_key}
        assert keys == {"focus", "commits"}
        assert top_pair.best.r == pytest.approx(1.0, abs=1e-6)
        assert top_pair.consistent_sign
        assert top_pair.stability == pytest.approx(1.0)

    def test_the_uncorrelated_pairs_fall_below_min_r(
        self, repository: Repository, home: Path
    ) -> None:
        build_three_stream_fixture(repository)
        report = correlate(repository, min_r=0.5, top=None)

        surviving_pairs = {
            frozenset((p.best.x.stream_key, p.best.y.stream_key)) for p in report.pairs
        }
        assert frozenset(("focus", "weight")) not in surviving_pairs
        assert frozenset(("commits", "weight")) not in surviving_pairs
        # But they were tested, not silently ignored -- they show up in the
        # denominator even though they did not clear the ranking threshold.
        assert report.tests_performed > 0

    def test_n_matches_the_number_of_complete_periods_at_each_interval(
        self, repository: Repository, home: Path
    ) -> None:
        build_three_stream_fixture(repository)
        report = correlate(repository, min_r=0.5, top=None)
        [top_pair] = [
            p
            for p in report.pairs
            if {p.best.x.stream_key, p.best.y.stream_key} == {"focus", "commits"}
        ]
        for correlation in top_pair.by_interval:
            if correlation.interval == Interval(Granularity.DAY, 1):
                assert correlation.n == DAY_COUNT
            if correlation.interval == Interval(Granularity.WEEK, 1):
                assert correlation.n == DAY_COUNT // 7

    def test_a_short_range_reports_coarse_intervals_as_skipped(
        self, repository: Repository, home: Path
    ) -> None:
        build_three_stream_fixture(repository)
        # Only the first 10 days -- nowhere near enough for a fortnight or a
        # month to produce even one complete period, let alone eight.
        report = correlate(
            repository,
            since=START,
            until=START + timedelta(days=9),
            min_r=0.5,
            top=None,
        )
        skipped_intervals = {s.interval for s in report.skipped}
        assert Interval(Granularity.WEEK, 2) in skipped_intervals
        assert Interval(Granularity.MONTH, 1) in skipped_intervals
        for skip in report.skipped:
            assert "needs" in skip.reason or "distinct" in skip.reason

    def test_q_values_are_computed_over_every_test_performed(
        self, repository: Repository, home: Path
    ) -> None:
        build_three_stream_fixture(repository)
        report = correlate(repository, min_r=0.0, max_q=None, top=None)
        all_correlations = [c for pair in report.pairs for c in pair.by_interval]
        assert all_correlations
        assert all(0.0 <= c.q_value <= 1.0 for c in all_correlations)
        assert len(all_correlations) <= report.tests_performed


class TestEmptyDatabase:
    def test_returns_an_empty_report_rather_than_raising(
        self, repository: Repository, home: Path
    ) -> None:
        report = correlate(repository)
        assert report.pairs == ()
        assert report.skipped == ()
        assert report.tests_performed == 0
        assert report.series_count == 0

    def test_a_single_stream_has_no_pairs_to_test(self, repository: Repository, home: Path) -> None:
        repository.ensure_stream("timecard", "Timecard", StreamKind.INTERVAL)
        report = correlate(repository)
        assert report.pairs == ()
        assert report.tests_performed == 0
