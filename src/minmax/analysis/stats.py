"""Pearson's r, its p-value, and a false-discovery correction over many tests.

The only module in ``analysis/`` that imports scipy, so the optional
``analysis`` dependency (see ``pyproject.toml``) is isolated to one small,
easily-swapped file rather than spread through the engine. scipy itself is
imported lazily, inside :func:`pearson`, so importing this module (and
therefore ``analysis/`` generally) does not require it to be installed --
only actually computing a correlation does, and the caller that does that
(``cli.py``'s ``cmd_correlate``) is the one place that needs to handle it
being missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["PearsonResult", "benjamini_hochberg", "pearson"]


@dataclass(frozen=True, slots=True)
class PearsonResult:
    r: float
    p_value: float
    n: int


def pearson(x: Sequence[float], y: Sequence[float]) -> PearsonResult:
    """Pearson's correlation coefficient between ``x`` and ``y``, and its
    two-sided p-value under the null hypothesis that they are uncorrelated.

    Raises ``ValueError`` rather than returning a number for every input scipy
    itself only warns about: mismatched lengths, fewer than two paired values,
    or either side being constant (undefined -- there is no variance to
    correlate against, and scipy's own answer there is ``nan`` behind a
    ``ConstantInputWarning`` a caller could easily miss). The correlation
    sweep in ``analysis/correlate.py`` is expected to have already screened
    these out before calling this at all (see its minimum-sample-size and
    distinct-value gates); this function still checks them itself so that
    calling it directly -- as the tests do -- is never silently wrong.
    """
    if len(x) != len(y):
        raise ValueError(f"x and y must be the same length, got {len(x)} and {len(y)}")
    n = len(x)
    if n < 2:
        raise ValueError(f"pearson needs at least 2 paired values, got {n}")
    if len(set(x)) == 1 or len(set(y)) == 1:
        raise ValueError("pearson is undefined for a constant series")

    from scipy import stats

    result = stats.pearsonr(x, y)
    return PearsonResult(r=float(result.statistic), p_value=float(result.pvalue), n=n)


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """False-discovery-rate q-values for a batch of p-values, in the order given.

    A correlation sweep tests many (pair, period-length) combinations, and
    reporting a bare p-value from one of them ignores that ~5% of *any* batch
    of tests clears p<0.05 by chance alone. The Benjamini-Hochberg procedure
    answers a more honest question -- "of the results I call significant,
    what fraction are expected to be false leads" -- at the cost of nothing
    more exotic than sorting: rank the p-values ascending, scale each by
    ``m / rank``, then enforce that a q-value never exceeds the next-larger
    p-value's, by taking a running minimum from the largest rank down to the
    smallest (the standard way this procedure is stated, e.g. in
    ``statsmodels.stats.multitest.multipletests(method="fdr_bh")``, which
    this reimplements in a dozen lines rather than adding the dependency for).

    Q-values are capped at 1.0. An empty input returns an empty list.
    """
    m = len(p_values)
    if m == 0:
        return []

    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        index = order[rank - 1]
        candidate = min(1.0, p_values[index] * m / rank)
        running_min = min(running_min, candidate)
        adjusted[index] = running_min
    return adjusted
