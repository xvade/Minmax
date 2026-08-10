"""The correlation engine: enumerate, load, sweep, gate, test, correct, rank.

One function, :func:`correlate`, does the whole thing -- everything above it
in this package (``catalog``, ``series``, ``sweep``, ``stats``) is a pure
building block this orchestrates. Reading it top to bottom is reading the
pipeline: candidate series and pairs (``catalog``), each measure's daily
values (``series``), bucketed into every period length being swept and
aligned pairwise (``sweep``), gated on sample size, tested with Pearson's r
(``stats``), corrected for how many tests that was, and ranked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from minmax.analysis.catalog import (
    DEFAULT_MAX_SERIES,
    CandidatePair,
    candidate_measures,
    candidate_pairs,
)
from minmax.analysis.models import Correlation, CorrelationReport, PairResult, Skipped
from minmax.analysis.series import SeriesLoader
from minmax.analysis.stats import PearsonResult, benjamini_hochberg, pearson
from minmax.analysis.sweep import DEFAULT_SWEEP, align, periodic_values
from minmax.core.columns import Granularity, Interval
from minmax.core.measures import DailyValue, Measure
from minmax.storage.repository import Repository

__all__ = ["MIN_DISTINCT_VALUES", "MIN_PERIODS", "correlate"]

#: Aligned, complete, jointly-observed periods required before a (pair,
#: interval) combination is even tested. At n=6 the two-sided 5% critical
#: |r| is 0.81; below roughly this many points a reported r is a claim about
#: very little data wearing a lab coat.
MIN_PERIODS = 8

#: Distinct values required on each side, so a two-valued (mostly-zero,
#: occasionally-something) series cannot pin r at +-1 off a handful of days.
MIN_DISTINCT_VALUES = 3

#: Below this many days per period, Pearson's assumption that observations
#: are independent is worth flagging -- daily life has weekday/weekend
#: structure a period shorter than a week cannot average away.
_INDEPENDENCE_CAVEAT_DAYS = 7


@dataclass(frozen=True, slots=True)
class _RawTest:
    pair: CandidatePair
    interval: Interval
    result: PearsonResult
    first_period: date
    last_period: date


def correlate(
    repository: Repository,
    *,
    stream_keys: Sequence[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    sweep: Sequence[Interval] = DEFAULT_SWEEP,
    min_periods: int = MIN_PERIODS,
    min_distinct: int = MIN_DISTINCT_VALUES,
    min_r: float = 0.5,
    max_q: float | None = None,
    top: int | None = 20,
    per_category: bool = False,
    include_fields: bool = True,
    field_sum: bool = True,
    max_series: int = DEFAULT_MAX_SERIES,
) -> CorrelationReport:
    """Run a full correlation sweep and return a ranked report.

    ``min_r``/``max_q``/``top`` shape what ends up in
    :attr:`~minmax.analysis.models.CorrelationReport.pairs`, not what gets
    *tested* -- every candidate pair is tested at every interval in ``sweep``
    that has enough data, and the Benjamini-Hochberg correction
    (:func:`~minmax.analysis.stats.benjamini_hochberg`) is computed over all
    of that before any filtering happens, so narrowing what is displayed
    later can never change a q-value.
    """
    measures = candidate_measures(
        repository,
        stream_keys=stream_keys,
        per_category=per_category,
        include_fields=include_fields,
        field_sum=field_sum,
        max_series=max_series,
    )
    unbounded = candidate_measures(
        repository,
        stream_keys=stream_keys,
        per_category=per_category,
        include_fields=include_fields,
        field_sum=field_sum,
        max_series=10**9,
    )
    pairs = candidate_pairs(repository, measures)

    loader = SeriesLoader(repository, since=since, until=until)
    daily_cache: dict[str, list[DailyValue]] = {}
    active_cache: dict[str, frozenset[date]] = {}
    periodic_cache: dict[tuple[str, Interval], dict[date, float]] = {}

    def daily_for(measure: Measure) -> list[DailyValue]:
        return daily_cache.setdefault(measure.key, loader.daily(measure))

    def active_for(stream_key: str) -> frozenset[date]:
        return active_cache.setdefault(stream_key, loader.active_days(stream_key))

    def periodic_for(measure: Measure, interval: Interval) -> dict[date, float]:
        key = (measure.key, interval)
        if key not in periodic_cache:
            periodic_cache[key] = periodic_values(
                measure, daily_for(measure), interval, active_days=active_for(measure.stream_key)
            )
        return periodic_cache[key]

    raw_tests: list[_RawTest] = []
    skipped: list[Skipped] = []

    for pair in pairs:
        for interval in sweep:
            x_values = periodic_for(pair.x, interval)
            y_values = periodic_for(pair.y, interval)
            xs, ys, shared = align(x_values, y_values)
            n = len(shared)

            if n < min_periods:
                skipped.append(
                    Skipped(
                        pair.x.key,
                        pair.y.key,
                        interval,
                        f"{n} complete period(s), needs {min_periods}",
                        n,
                    )
                )
                continue
            if len(set(xs)) < min_distinct or len(set(ys)) < min_distinct:
                skipped.append(
                    Skipped(pair.x.key, pair.y.key, interval, "not enough distinct values", n)
                )
                continue

            raw_tests.append(_RawTest(pair, interval, pearson(xs, ys), shared[0], shared[-1]))

    q_values = benjamini_hochberg([test.result.p_value for test in raw_tests])

    by_pair: dict[tuple[str, str], list[Correlation]] = {}
    for test, q_value in zip(raw_tests, q_values, strict=True):
        correlation = Correlation(
            x=test.pair.x,
            y=test.pair.y,
            interval=test.interval,
            n=test.result.n,
            r=test.result.r,
            p_value=test.result.p_value,
            q_value=q_value,
            first_period=test.first_period,
            last_period=test.last_period,
            caveats=test.pair.caveats + _interval_caveat(test.interval),
        )
        key = (test.pair.x.key, test.pair.y.key)
        by_pair.setdefault(key, []).append(correlation)

    results = [_pair_result(correlations, min_r) for correlations in by_pair.values()]
    results = [
        result
        for result in results
        if abs(result.best.r) >= min_r and (max_q is None or result.best.q_value <= max_q)
    ]
    results.sort(key=lambda result: (-abs(result.best.r), -result.best.n, result.best.x.key))
    if top is not None:
        results = results[:top]

    days_seen = [value.day for values in daily_cache.values() for value in values]
    first_day = min(days_seen) if days_seen else None
    last_day = max(days_seen) if days_seen else None

    return CorrelationReport(
        pairs=tuple(results),
        skipped=tuple(skipped),
        tests_performed=len(raw_tests),
        series_count=len(measures),
        truncated_series=max(0, len(unbounded) - len(measures)),
        first_day=first_day,
        last_day=last_day,
    )


def _pair_result(correlations: Sequence[Correlation], min_r: float) -> PairResult:
    by_interval = tuple(correlations)
    best = max(by_interval, key=lambda c: (abs(c.r), c.n))
    qualifying = [c for c in by_interval if abs(c.r) >= min_r]
    stability = len(qualifying) / len(by_interval) if by_interval else 0.0
    signs = {1 if c.r >= 0 else -1 for c in qualifying}
    consistent_sign = len(signs) <= 1
    return PairResult(
        best=best, by_interval=by_interval, stability=stability, consistent_sign=consistent_sign
    )


def _interval_caveat(interval: Interval) -> tuple[str, ...]:
    if interval.unit is Granularity.DAY and interval.count < _INDEPENDENCE_CAVEAT_DAYS:
        return (
            "daily-scale periods share weekday/weekend structure; Pearson's "
            "p-value assumes independent observations, so treat it as optimistic.",
        )
    return ()
