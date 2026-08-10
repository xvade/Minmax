"""Which measures a correlation sweep compares, and which pairs are worth it.

Candidates are enumerated at **grand total + top-level category** only, per
stream -- not every leaf category. This follows an existing precedent
(``category.top_level`` is already the codebase's grouping unit:
``group_color`` keys colour by it, and it is the desktop trends chart's
default depth too) and a statistical one: over the ~60 days of real data
Minmax has today, a leaf category is mostly zeros, and correlating two
mostly-zero vectors produces an r driven by a handful of coincident days
rather than a real pattern. ``--per-category`` in a future version can drill
one level deeper; this module does not attempt it yet.

Three kinds of pair are excluded before any statistics ever run on them,
because each is either arithmetically guaranteed or not an independent
finding:

- A measure against its own stream's grand total (or vice versa) -- the
  total *contains* the part, so they correlate by construction.
- The same field, on the same stream and category, under two different
  aggregates (mean vs. sum) -- not two things, one thing measured twice.
- A measure against itself.

A pair that survives is not necessarily an unconfounded one: two categories
from the same *exclusive* stream (``Stream.exclusive`` -- Timecard, where at
most one timer runs at a time) mechanically compete for one day's time
budget, which pushes any two of them toward anti-correlation. That is a real
caveat about the reading, not a reason to hide the pair, so it is attached
rather than filtered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from minmax.core import paths
from minmax.core.measures import Aggregate, Measure, MeasureKind
from minmax.core.models import FieldRole, StreamField, StreamKind
from minmax.storage.repository import Repository

__all__ = ["DEFAULT_MAX_SERIES", "CandidatePair", "candidate_measures", "candidate_pairs"]

#: A guard against a combinatorial explosion of pairs, not a performance
#: necessity (a sweep over even a few hundred series is still fast) -- past
#: this many candidate series, the output itself stops being readable.
DEFAULT_MAX_SERIES = 40


@dataclass(frozen=True, slots=True)
class CandidatePair:
    x: Measure
    y: Measure
    caveats: tuple[str, ...] = ()


def candidate_measures(
    repository: Repository,
    *,
    stream_keys: Sequence[str] | None = None,
    per_category: bool = False,
    include_fields: bool = True,
    field_sum: bool = True,
    max_series: int = DEFAULT_MAX_SERIES,
) -> list[Measure]:
    """Every measure worth correlating, across the requested streams.

    ``stream_keys=None`` means every declared stream. ``per_category`` adds
    one measure per top-level category alongside each stream's grand total;
    ``include_fields`` (on by default) adds a mean for each declared numeric
    field on an event stream, and ``field_sum`` (also on by default) adds
    that field's sum too -- the same trio the desktop trends chart now offers
    per field: count, total, average. Truncated to ``max_series`` if it would
    otherwise exceed it -- see :func:`_truncate`.
    """
    streams = [
        stream
        for stream in repository.list_streams()
        if stream_keys is None or stream.key in stream_keys
    ]
    measures: list[Measure] = []
    for stream in streams:
        assert stream.id is not None  # every stream from list_streams() is persisted
        measures.extend(
            _measures_for_stream(
                repository,
                stream_key=stream.key,
                stream_id=stream.id,
                kind=stream.kind,
                per_category=per_category,
                include_fields=include_fields,
                field_sum=field_sum,
            )
        )
    return _truncate(repository, measures, max_series)


def candidate_pairs(repository: Repository, measures: Sequence[Measure]) -> list[CandidatePair]:
    """Every pair of ``measures`` worth testing, with the pairs excluded for
    being arithmetically guaranteed already removed, and a caveat attached
    where one applies (see the module docstring)."""
    exclusive_streams = {stream.key for stream in repository.list_streams() if stream.exclusive}

    pairs: list[CandidatePair] = []
    for x, y in combinations(measures, 2):
        if _is_ancestor_or_self(x, y) or _is_aggregate_variant(x, y):
            continue
        pairs.append(CandidatePair(x=x, y=y, caveats=_caveats(x, y, exclusive_streams)))
    return pairs


# -- internals --------------------------------------------------------------


def _measures_for_stream(
    repository: Repository,
    *,
    stream_key: str,
    stream_id: int,
    kind: StreamKind,
    per_category: bool,
    include_fields: bool,
    field_sum: bool,
) -> list[Measure]:
    categories = repository.list_categories(stream_id)
    top_levels = sorted({paths.top_level(category.path) for category in categories})

    measures: list[Measure] = []
    if kind is StreamKind.INTERVAL:
        measures.append(Measure(stream_key=stream_key, kind=MeasureKind.DURATION))
        if per_category:
            measures.extend(
                Measure(stream_key=stream_key, kind=MeasureKind.DURATION, category_path=top)
                for top in top_levels
            )
    elif kind is StreamKind.EVENT:
        measures.append(Measure(stream_key=stream_key, kind=MeasureKind.COUNT))
        if per_category:
            measures.extend(
                Measure(stream_key=stream_key, kind=MeasureKind.COUNT, category_path=top)
                for top in top_levels
            )
        if include_fields:
            declared_fields = repository.stream_fields(stream_id)
            measures.extend(
                _field_measures(stream_key, declared_fields, top_levels, per_category, field_sum)
            )
    # StreamKind.SAMPLE: no storage exists yet (see TODO.md), so nothing to enumerate.
    return measures


def _field_measures(
    stream_key: str,
    fields: Sequence[StreamField],
    top_levels: Sequence[str],
    per_category: bool,
    field_sum: bool,
) -> list[Measure]:
    measures: list[Measure] = []
    for declared in fields:
        if declared.role is not FieldRole.NUMBER:
            continue
        aggregates = (Aggregate.MEAN, Aggregate.SUM) if field_sum else (Aggregate.MEAN,)
        for aggregate in aggregates:
            measures.append(
                Measure(
                    stream_key=stream_key,
                    kind=MeasureKind.FIELD,
                    field_key=declared.key,
                    aggregate=aggregate,
                    unit=declared.unit,
                )
            )
            if per_category:
                measures.extend(
                    Measure(
                        stream_key=stream_key,
                        kind=MeasureKind.FIELD,
                        field_key=declared.key,
                        aggregate=aggregate,
                        category_path=top,
                        unit=declared.unit,
                    )
                    for top in top_levels
                )
    return measures


def _is_ancestor_or_self(x: Measure, y: Measure) -> bool:
    """A grand total contains its own category's total (or field measure),
    so pairing the two is a mechanical fact about the data, not a finding."""
    if (
        x.stream_key != y.stream_key
        or x.kind != y.kind
        or x.field_key != y.field_key
        or x.aggregate != y.aggregate
    ):
        return False
    return x.category_path is None or y.category_path is None


def _is_aggregate_variant(x: Measure, y: Measure) -> bool:
    """The same field, same category, under two different aggregates -- one
    quantity measured twice, not two quantities."""
    if x.kind is not MeasureKind.FIELD or y.kind is not MeasureKind.FIELD:
        return False
    return (
        x.stream_key == y.stream_key
        and x.field_key == y.field_key
        and x.category_path == y.category_path
        and x.aggregate != y.aggregate
    )


def _caveats(x: Measure, y: Measure, exclusive_streams: set[str]) -> tuple[str, ...]:
    caveats: list[str] = []

    same_exclusive_stream = (
        x.stream_key == y.stream_key
        and x.stream_key in exclusive_streams
        and x.category_path is not None
        and y.category_path is not None
    )
    if same_exclusive_stream:
        caveats.append(
            f"{x.stream_key!r} is an exclusive stream (at most one category is ever active at "
            "once), so these two categories' durations mechanically compete for one shared "
            "time budget."
        )

    field = _paired_with_own_sum(x, y)
    if field is not None:
        caveats.append(
            f"{field.stream_key!r}'s count and its {field.field_key!r} total are mechanically "
            "linked -- a sum is the count times the mean, so a correlation here may just be "
            "restating that."
        )

    return tuple(caveats)


def _paired_with_own_sum(x: Measure, y: Measure) -> Measure | None:
    """The field-sum measure, if ``x``/``y`` is a stream's count paired with a
    numeric field's sum on that same stream and category -- ``None``
    otherwise. Named separately from :func:`_is_ancestor_or_self` and
    :func:`_is_aggregate_variant`, which *exclude* a pair outright: this one
    is only ever attached as a caveat, per the module docstring's rule that a
    real confound about the reading is flagged rather than hidden."""
    count, field = (x, y) if x.kind is MeasureKind.COUNT else (y, x)
    if count.kind is not MeasureKind.COUNT or field.kind is not MeasureKind.FIELD:
        return None
    if field.aggregate is not Aggregate.SUM:
        return None
    if count.stream_key != field.stream_key or count.category_path != field.category_path:
        return None
    return field


def _truncate(repository: Repository, measures: list[Measure], max_series: int) -> list[Measure]:
    """Keep every grand total, then fill the remainder with the most-used
    categories -- rather than an arbitrary (e.g. alphabetical) subset, which
    could easily keep a barely-used category over the one actually worth
    reading about."""
    if len(measures) <= max_series:
        return measures

    grand = [measure for measure in measures if measure.category_path is None]
    if len(grand) >= max_series:
        return grand[:max_series]

    by_category = [measure for measure in measures if measure.category_path is not None]
    ranked = sorted(by_category, key=lambda measure: _volume(repository, measure), reverse=True)
    return grand + ranked[: max_series - len(grand)]


def _volume(repository: Repository, measure: Measure) -> float:
    """How much a measure's category has actually been used, for ranking a
    truncation only -- never used statistically."""
    stream = repository.get_stream(measure.stream_key)
    if stream is None or stream.id is None:
        return 0.0
    bounds = repository.observed_range(stream.id)
    if bounds is None:
        return 0.0
    if stream.kind is StreamKind.INTERVAL:
        duration_totals = repository.totals_by_category(
            stream.id, bounds[0], bounds[1], path_prefix=measure.category_path
        )
        return float(sum(total.seconds for total in duration_totals))
    event_totals = repository.counts_by_category(
        stream.id, bounds[0], bounds[1] + 1, path_prefix=measure.category_path
    )
    return float(sum(total.record_count for total in event_totals))
