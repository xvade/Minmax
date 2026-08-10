"""What "how much" means for a stream, and how to combine it across days.

An interval stream's "how much" is unambiguous: a duration, summed. A
point-in-time stream's is not -- it might be how often something happened
(a count, also summed), or a declared numeric field's sum, mean, minimum, or
maximum (a heart-rate reading, say). ``docs/architecture.md``'s decision D6
sketched exactly this abstraction and deliberately left it unbuilt, since
nothing needed it yet: the desktop trends chart gets away with reusing its
seconds-shaped machinery for a count, because a count sums the same way a
duration does. A mean does not -- so the first caller that needs one (the
correlation engine, in ``analysis/``) is what this module is built for.

The one invariant this module exists to protect: **a multi-day mean is never
the mean of daily means.** Two readings averaging 70 one day and one reading
of 100 the next average to 80 overall (weighted by how many readings each day
had), not 85 (the mean of 70 and 100). ``field_rollup``'s own schema comment
already makes this rule for storage; :meth:`Measure.reduce` is what makes it
true for every caller that turns stored sums and counts back into a value,
so there is exactly one place this arithmetic can be gotten wrong instead of
one per caller.

Pure and Qt/SQL-free, like ``core/trends.py`` and ``core/columns.py``: turning
a :class:`Measure` into actual numbers means reading ``rollup``/``field_rollup``
through the repository, which is ``analysis/series.py``'s job, not this
module's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

__all__ = ["Aggregate", "DailyValue", "Measure", "MeasureKind"]


class MeasureKind(StrEnum):
    """Which stored aggregate a measure reads."""

    DURATION = "duration"
    """``rollup.seconds`` -- an interval stream's tracked time."""

    COUNT = "count"
    """``rollup.record_count`` -- an event stream's observation count.

    Interval streams also have a ``record_count`` column, but it counts
    observations *touching* a bucket and is only meaningful within that one
    bucket -- summing it across days double-counts a run that spans several.
    An event's count has no such trap and sums cleanly, which is exactly why
    this kind is restricted to event streams (enforced by whoever builds a
    ``Measure``, e.g. ``analysis/catalog.py`` -- a bare ``Measure`` does not
    know a stream's kind, so it cannot check this itself).
    """

    FIELD = "field"
    """``field_rollup`` -- a declared numeric field on an event stream."""


class Aggregate(StrEnum):
    """How several days combine into one wider value."""

    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"

    @property
    def extensive(self) -> bool:
        """Whether combining periods is addition.

        True only for :attr:`SUM` -- a duration, a count, or a field total is
        the sum of its days. A mean, minimum, or maximum is not: they must be
        *re-derived* from the days' own totals/counts/extremes, never averaged
        or min/maxed a second time over already-combined values (that would
        be the mean-of-means bug for :attr:`MEAN`, and the wrong answer for
        :attr:`MIN`/:attr:`MAX` too whenever a period's extreme did not fall
        on a period boundary).
        """
        return self is Aggregate.SUM


@dataclass(frozen=True, slots=True)
class DailyValue:
    """One day's contribution to a measure, in the shape :meth:`Measure.reduce`
    needs to combine several of them correctly.

    ``total`` is ``rollup.seconds``, ``rollup.record_count``, or
    ``field_rollup.value_sum`` depending on the measure; ``weight`` is
    ``field_rollup.value_count`` for a field measure (how many readings made
    up ``total``) and ``1.0`` otherwise, where each day counts once towards
    its own average. ``low``/``high`` carry ``field_rollup.value_min``/
    ``value_max`` and are ``None`` for duration/count measures, which have no
    such thing.
    """

    day: date
    total: float
    weight: float = 1.0
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True, slots=True)
class Measure:
    """One named quantity to track over time: a stream's duration or event
    count, or a declared numeric field's sum/mean/min/max.

    ``category_path`` of ``None`` means the whole stream (every category
    combined); a path restricts to one category and everything under it, the
    same prefix rule ``storage.repository``'s ``path_prefix`` queries use.
    """

    stream_key: str
    kind: MeasureKind
    aggregate: Aggregate = Aggregate.SUM
    category_path: str | None = None
    field_key: str | None = None
    """Which declared field, for a :attr:`MeasureKind.FIELD` measure. Must be
    ``None`` for any other kind -- a duration or a count has no field to name."""
    unit: str | None = None
    """Presentation only (e.g. ``'bpm'``); never affects the arithmetic."""

    def __post_init__(self) -> None:
        if self.kind is MeasureKind.FIELD:
            if self.field_key is None:
                raise ValueError("a FIELD measure must declare field_key")
        else:
            if self.field_key is not None:
                raise ValueError(f"a {self.kind} measure cannot declare field_key")
            if self.aggregate is not Aggregate.SUM:
                raise ValueError(
                    f"a {self.kind} measure must use Aggregate.SUM, got {self.aggregate}"
                )

    @property
    def key(self) -> str:
        """A stable id: ``'timecard:duration:media'``, ``'pickups:count'``,
        ``'heart_rate:field:bpm:mean'``. Disambiguated by stream as well as
        field, since two different streams may each declare a field with the
        same key (see ``core/models.py``'s ``StreamField.key`` docstring,
        which anticipates this naming)."""
        parts = [self.stream_key, self.kind.value]
        if self.kind is MeasureKind.FIELD:
            assert self.field_key is not None  # guaranteed by __post_init__
            parts.append(self.field_key)
            parts.append(self.aggregate.value)
        if self.category_path is not None:
            parts.append(self.category_path)
        return ":".join(parts)

    @property
    def label(self) -> str:
        """A short human-readable description. Callers with richer context
        (a stream's display name, a field's declared label) may build a nicer
        one -- this is the fallback every measure can produce on its own."""
        where = self.category_path or self.stream_key
        if self.kind is MeasureKind.DURATION:
            return f"{where} duration"
        if self.kind is MeasureKind.COUNT:
            return f"{where} count"
        assert self.field_key is not None
        return f"{where} {self.field_key} {self.aggregate.value}"

    @property
    def summable_across_periods(self) -> bool:
        """Whether a wider period's value is the sum of its days' values.

        True for duration, count, and a field's sum; false for mean/min/max,
        which must be re-derived from the days' own components via
        :meth:`reduce` rather than combined a second time.
        """
        return self.aggregate.extensive

    @property
    def zero_when_absent(self) -> bool:
        """Whether a day with no stored row is a genuine zero.

        True for duration/count: no activity that day really is zero
        activity. False for a field: no reading that day is not the same as
        a reading of zero, so an absent day must be left out of the
        combination entirely rather than folded in as ``0.0``.
        """
        return self.kind is not MeasureKind.FIELD

    def reduce(self, values: Sequence[DailyValue]) -> float | None:
        """Combine several days' values into one, the only place this ever
        happens -- so "one day" and "many days" are the same code path.

        Returns ``None`` when there is nothing to report: a :attr:`Aggregate.MEAN`
        whose days carry no weight (no readings at all), or a
        :attr:`Aggregate.MIN`/:attr:`Aggregate.MAX` with no extremes recorded.
        A :attr:`Aggregate.SUM` of no days is ``0.0`` -- summing nothing is a
        real, well-defined answer, unlike averaging or bounding nothing.
        """
        if self.aggregate is Aggregate.SUM:
            return sum(value.total for value in values)
        if self.aggregate is Aggregate.MEAN:
            weight = sum(value.weight for value in values)
            if weight <= 0:
                return None
            return sum(value.total for value in values) / weight
        if self.aggregate is Aggregate.MIN:
            lows = [value.low for value in values if value.low is not None]
            return min(lows) if lows else None
        if self.aggregate is Aggregate.MAX:
            highs = [value.high for value in values if value.high is not None]
            return max(highs) if highs else None
        raise AssertionError(f"unhandled aggregate: {self.aggregate}")
