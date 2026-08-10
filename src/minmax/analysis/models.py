"""Result shapes for a correlation sweep: pure data, no computation.

Kept separate from ``correlate.py`` so the shape a caller (``api/``, tests)
depends on is not entangled with how it gets built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from minmax.core.columns import Interval
from minmax.core.measures import Measure

__all__ = ["Correlation", "CorrelationReport", "PairResult", "Skipped"]


@dataclass(frozen=True, slots=True)
class Correlation:
    """One (pair, period-length) test that had enough data to run."""

    x: Measure
    y: Measure
    interval: Interval
    n: int
    """Aligned, complete, jointly-observed periods the test ran over."""
    r: float
    p_value: float
    q_value: float
    """A Benjamini-Hochberg false-discovery rate over *every* test the sweep
    performed, not just this pair -- see ``analysis/stats.py``."""
    first_period: date
    last_period: date
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PairResult:
    """Everything a sweep found for one pair, across every period length tested."""

    best: Correlation
    """The interval with the largest ``|r|`` for this pair."""
    by_interval: tuple[Correlation, ...]
    """Every interval this pair was actually tested at (passed the minimum-
    sample-size and distinct-value gates), ascending by the sweep's own order."""
    stability: float
    """Fraction of :attr:`by_interval` clearing the sweep's ``min_r`` *and*
    agreeing in sign with :attr:`best` -- a relationship that holds at
    several period lengths is a real structural one; one strong at exactly
    one length and nowhere else is what a wide sweep finds by construction."""
    consistent_sign: bool
    """Whether every interval clearing ``min_r`` agrees in sign with the others."""


@dataclass(frozen=True, slots=True)
class Skipped:
    """A (pair, interval) combination that never got tested, and why."""

    x_key: str
    y_key: str
    interval: Interval
    reason: str
    n: int
    """Aligned periods that *were* available -- short of what was needed."""


@dataclass(frozen=True, slots=True)
class CorrelationReport:
    """A complete sweep's results, ranked and ready to present."""

    pairs: tuple[PairResult, ...]
    """Ranked by ``|best.r|`` descending, already filtered to whatever
    ``min_r``/``max_q``/``top`` the sweep was run with."""
    skipped: tuple[Skipped, ...]
    tests_performed: int
    """Every (pair, interval) combination that passed the sample-size gate
    and was actually handed to Pearson's r -- the denominator the report's
    Benjamini-Hochberg correction is computed over."""
    series_count: int
    truncated_series: int = 0
    """How many candidate series ``--max-series`` left out, if any."""
    first_day: date | None = None
    last_day: date | None = None
