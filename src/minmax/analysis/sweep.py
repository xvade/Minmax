"""Bucketing a measure's daily values into periods, and lining two of them up.

Reuses ``core/columns.py`` unchanged: its anchoring to a fixed epoch is what
makes two series bucketed independently at the same :class:`Interval` land on
the same period boundaries, which is the entire alignment mechanism a
correlation needs -- pairing is a plain key intersection (:func:`align`), not
a range-intersection problem.

Two decisions worth knowing before reading the code:

**Clipped columns are dropped, not rescaled.** A 3-day fragment of a "week" at
the edge of a series' data is not comparable to a full week for a sum, a
mean, or an extreme -- rescaling only works for a sum, and does nothing at
all for the other three aggregates. Requiring complete periods is the one
rule that is correct for all of them.

**A period where the stream recorded nothing at all is missing, not zero.**
This deliberately differs from how the desktop trends chart treats the same
kind of gap -- it keeps an empty column and draws it as zero, because for a
*display* that is the honest answer ("I stopped tracking" would otherwise be
compressed into "these days were adjacent", which is a different, false
claim -- see ``TrendsView``'s docstring). A *correlation* has a different
failure mode: two unrelated streams that both happen to be switched off at
the same time would otherwise look like they moved together, when what
actually happened is that neither was observed. Dropping the period avoids
manufacturing that relationship. An interior single-day gap within an
otherwise-tracked period still reads as zero for duration/count measures (no
rollup row that day genuinely means nothing happened) -- this is a known,
documented limitation, not silently smoothed over.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from minmax.core.columns import Granularity, Interval, columns_for
from minmax.core.measures import DailyValue, Measure

__all__ = ["DEFAULT_SWEEP", "align", "deduplicate_intervals", "periodic_values"]

#: A range wide enough to distinguish any two intervals this module ever
#: sweeps by their period boundaries alone -- see :func:`deduplicate_intervals`.
_FINGERPRINT_RANGE = (date(2020, 1, 1), date(2023, 12, 31))


def deduplicate_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    """Drop any interval whose period boundaries exactly match an earlier
    one already kept.

    ``Interval(Granularity.DAY, 7)`` and ``Interval(Granularity.WEEK, 1)``
    are the case this exists for: both anchor to the same Monday
    (``core/columns.py``'s ``_ANCHOR``), so they partition any range
    identically. Testing a sweep at both would silently count one period
    length twice -- once under each name -- which would show up as a
    duplicated row in output and an inflated test count for multiple-
    comparisons correction. Comparing the actual boundaries (rather than
    special-casing that one pair) also catches it for whatever a caller's
    own ``--interval`` list happens to contain.
    """
    seen: set[tuple[date, ...]] = set()
    kept: list[Interval] = []
    for interval in intervals:
        fingerprint = tuple(
            column.start
            for column in columns_for(_FINGERPRINT_RANGE[0], _FINGERPRINT_RANGE[1], interval)
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(interval)
    return kept


def _default_sweep() -> tuple[Interval, ...]:
    return tuple(
        deduplicate_intervals(
            [
                Interval(Granularity.DAY, 1),
                Interval(Granularity.DAY, 2),
                Interval(Granularity.DAY, 3),
                Interval(Granularity.DAY, 5),
                Interval(Granularity.WEEK, 1),
                Interval(Granularity.DAY, 10),
                Interval(Granularity.WEEK, 2),
                Interval(Granularity.WEEK, 3),
                Interval(Granularity.MONTH, 1),
            ]
        )
    )


#: Period lengths tried per pair, ascending, with :func:`deduplicate_intervals`
#: already applied -- so this is guaranteed not to test the same period
#: length twice under two different names, rather than merely curated by hand
#: to avoid it.
DEFAULT_SWEEP: tuple[Interval, ...] = _default_sweep()


def periodic_values(
    measure: Measure,
    daily: Sequence[DailyValue],
    interval: Interval,
    *,
    active_days: frozenset[date],
) -> dict[date, float]:
    """``measure``'s value for every complete, non-blackout period of
    ``interval`` within its stream's active range, keyed by each period's
    anchored start.

    ``daily`` is normally :meth:`~minmax.analysis.series.SeriesLoader.daily`'s
    result for this exact measure; ``active_days`` is
    :meth:`~minmax.analysis.series.SeriesLoader.active_days` for its stream.
    The range swept is ``active_days``' own span, not just the days ``daily``
    happens to cover -- a sparse field measure (or a rarely-used category)
    still has an opinion about every period the *stream* was active in,
    namely "no reading", and computing columns from its own sparse dates
    instead would incorrectly clip a period that a busier measure over the
    same stream would see as complete.
    """
    if interval.is_auto:
        raise ValueError("a correlation sweep needs a resolved interval, not AUTO")
    if not active_days:
        return {}

    by_day = {value.day: value for value in daily}
    first = min(active_days)
    last = max(active_days)

    result: dict[date, float] = {}
    for column in columns_for(first, last, interval):
        if column.clipped:
            continue
        days = column.days()
        if not any(day in active_days for day in days):
            continue  # the stream recorded nothing at all in this stretch

        values: list[DailyValue] = []
        for day in days:
            if day in by_day:
                values.append(by_day[day])
            elif measure.zero_when_absent:
                values.append(DailyValue(day=day, total=0.0))
            # else: no reading that day contributes nothing at all (not even
            # a zero-weighted DailyValue) -- which is what lets the empty-
            # input check below (and Measure.reduce, for MEAN/MIN/MAX)
            # correctly treat a period with no readings as missing rather
            # than as a reading of zero.

        if not values:
            continue  # a FIELD measure with nothing recorded all period
        value = measure.reduce(values)
        if value is not None:
            result[column.start] = value

    return result


def align(
    x: Mapping[date, float], y: Mapping[date, float]
) -> tuple[list[float], list[float], list[date]]:
    """The two series' values over their shared period starts, ascending.

    This is the entire alignment mechanism: because both series were bucketed
    with the same anchored :class:`Interval` (possibly over different date
    ranges -- one measure's data can start or end before the other's), their
    period-start keys already agree wherever both have data, so pairing is a
    plain key intersection rather than anything that has to reason about
    calendars.
    """
    shared = sorted(x.keys() & y.keys())
    return [x[day] for day in shared], [y[day] for day in shared], shared
