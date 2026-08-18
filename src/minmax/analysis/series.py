"""Turning a :class:`~minmax.core.measures.Measure` into per-day values.

The repository adapter for the correlation engine: everything above this
module works with :class:`~minmax.core.measures.DailyValue`, never with a
``rollup``/``field_rollup`` row directly. Per-stream data is read once and
cached, so enumerating many measures over one stream (a grand total plus
several top-level categories, say) costs at most two queries -- one call to
:meth:`~minmax.storage.repository.Repository.daily_totals_by_category` and,
for an event stream, one to
:meth:`~minmax.storage.repository.Repository.daily_field_totals_by_category`
-- rather than one per measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from minmax.core import paths
from minmax.core.measures import DailyValue, Measure, MeasureKind
from minmax.core.models import DailyCategoryTotal, DailyFieldTotal, StreamKind
from minmax.core.windows import TimeMask
from minmax.storage.repository import Repository

__all__ = ["SeriesLoader"]


@dataclass(frozen=True, slots=True)
class _StreamDaily:
    """One stream's rollups, grouped by local day -- the cached unit of work."""

    kind: StreamKind
    by_day: dict[date, list[DailyCategoryTotal]] = field(default_factory=dict)
    fields_by_day: dict[date, list[DailyFieldTotal]] = field(default_factory=dict)


class SeriesLoader:
    """Reads ``rollup``/``field_rollup`` on behalf of :class:`Measure`\\ s.

    ``since``/``until`` bound every query the same way, so a caller comparing
    two measures over the same window only has to state it once. ``mask``,
    when given, restricts every stream read to a view's active windows --
    see ``docs/architecture.md``'s decision D9. It comes in already built
    (from ``api/client.py``, the one layer with a timezone to build one
    with), so this module stays as free of environment access as the rest of
    ``analysis``.
    """

    def __init__(
        self,
        repository: Repository,
        *,
        since: date | None = None,
        until: date | None = None,
        mask: TimeMask | None = None,
    ) -> None:
        self._repository = repository
        self._since = since
        self._until = until
        self._mask = mask
        self._cache: dict[str, _StreamDaily | None] = {}

    def daily(self, measure: Measure) -> list[DailyValue]:
        """``measure``'s value on every local day it has one, ascending.

        A day with nothing recorded *for this measure* is omitted -- not the
        same thing as a genuine zero, which :mod:`minmax.analysis.sweep`
        decides using :meth:`active_days` (a duration/count measure treats an
        omitted day within an active stretch as zero; a field measure never
        does, since no reading is not a reading of zero).
        """
        loaded = self._require(measure.stream_key)
        self._check_kind(measure, loaded.kind)
        if measure.kind is MeasureKind.FIELD:
            return self._field_values(measure, loaded)
        return self._count_or_duration_values(measure, loaded)

    def active_days(self, stream_key: str) -> frozenset[date]:
        """Local days the stream recorded *anything at all*.

        Every observation -- interval or event, with or without a numeric
        field -- produces a ``rollup`` row (see ``storage/rollups.py``'s
        ``apply_intervals``/``apply_events``), so ``by_day``'s keys already
        cover every day a field measure's ``fields_by_day`` could ever have;
        nothing is lost by not also unioning that in.
        """
        loaded = self._require(stream_key)
        return frozenset(loaded.by_day)

    # -- internals ---------------------------------------------------------

    def _count_or_duration_values(self, measure: Measure, loaded: _StreamDaily) -> list[DailyValue]:
        values: list[DailyValue] = []
        for day in sorted(loaded.by_day):
            matching = self._matching_categories(loaded.by_day[day], measure.category_path)
            if not matching:
                continue
            amount = sum(
                total.seconds if measure.kind is MeasureKind.DURATION else total.record_count
                for total in matching
            )
            values.append(DailyValue(day=day, total=float(amount)))
        return values

    def _field_values(self, measure: Measure, loaded: _StreamDaily) -> list[DailyValue]:
        assert measure.field_key is not None  # guaranteed by Measure.__post_init__
        values: list[DailyValue] = []
        for day in sorted(loaded.fields_by_day):
            same_field = [t for t in loaded.fields_by_day[day] if t.field_key == measure.field_key]
            matching = self._matching_field_categories(same_field, measure.category_path)
            if not matching:
                continue
            lows = [t.value_min for t in matching if t.value_min is not None]
            highs = [t.value_max for t in matching if t.value_max is not None]
            values.append(
                DailyValue(
                    day=day,
                    total=sum(t.value_sum for t in matching),
                    weight=float(sum(t.value_count for t in matching)),
                    low=min(lows) if lows else None,
                    high=max(highs) if highs else None,
                )
            )
        return values

    @staticmethod
    def _matching_categories(
        totals: list[DailyCategoryTotal], category_path: str | None
    ) -> list[DailyCategoryTotal]:
        if category_path is None:
            return totals
        return [t for t in totals if paths.is_descendant(t.category_path, category_path)]

    @staticmethod
    def _matching_field_categories(
        totals: list[DailyFieldTotal], category_path: str | None
    ) -> list[DailyFieldTotal]:
        if category_path is None:
            return totals
        return [t for t in totals if paths.is_descendant(t.category_path, category_path)]

    @staticmethod
    def _check_kind(measure: Measure, stream_kind: StreamKind) -> None:
        wants = {
            MeasureKind.DURATION: StreamKind.INTERVAL,
            MeasureKind.COUNT: StreamKind.EVENT,
            MeasureKind.FIELD: StreamKind.EVENT,
        }[measure.kind]
        if stream_kind is not wants:
            raise ValueError(
                f"{measure.key!r}: a {measure.kind} measure needs an {wants} stream, "
                f"but {measure.stream_key!r} is {stream_kind}"
            )

    def _require(self, stream_key: str) -> _StreamDaily:
        if stream_key not in self._cache:
            self._cache[stream_key] = self._load(stream_key)
        loaded = self._cache[stream_key]
        if loaded is None:
            raise LookupError(f"no stream {stream_key!r}")
        return loaded

    def _load(self, stream_key: str) -> _StreamDaily | None:
        stream = self._repository.get_stream(stream_key)
        if stream is None or stream.id is None:
            return None
        stream_id = stream.id

        start = self._since.isoformat() if self._since is not None else None
        end = self._until.isoformat() if self._until is not None else None

        by_day: dict[date, list[DailyCategoryTotal]] = {}
        for total in self._own_shape_rows(stream_id, stream.kind, start, end):
            by_day.setdefault(date.fromisoformat(total.bucket), []).append(total)

        fields_by_day: dict[date, list[DailyFieldTotal]] = {}
        if stream.kind is StreamKind.EVENT:
            for field_total in self._field_rows(stream_id, start, end):
                fields_by_day.setdefault(date.fromisoformat(field_total.bucket), []).append(
                    field_total
                )

        return _StreamDaily(kind=stream.kind, by_day=by_day, fields_by_day=fields_by_day)

    def _own_shape_rows(
        self, stream_id: int, kind: StreamKind, start: str | None, end: str | None
    ) -> list[DailyCategoryTotal]:
        """A stream's own-shape rows -- seconds for an interval stream, a
        count for an event stream -- from the rollup table, or, under
        :attr:`_mask`, from the masked substitutes (``storage/masked.py``)
        instead. See ``docs/architecture.md``'s decision D9.
        """
        if self._mask is None:
            return self._repository.daily_totals_by_category(
                stream_id, start_bucket=start, end_bucket=end
            )
        if self._mask.is_empty():
            return []
        if kind is StreamKind.EVENT:
            return self._repository.masked_daily_event_counts_by_category(
                stream_id, self._mask, start_bucket=start, end_bucket=end
            )
        return self._repository.masked_daily_totals_by_category(
            stream_id, self._mask, start_bucket=start, end_bucket=end
        )

    def _field_rows(
        self, stream_id: int, start: str | None, end: str | None
    ) -> list[DailyFieldTotal]:
        """A declared numeric field's rows, masked the same way as
        :meth:`_own_shape_rows`."""
        if self._mask is None:
            return self._repository.daily_field_totals_by_category(
                stream_id, start_bucket=start, end_bucket=end
            )
        if self._mask.is_empty():
            return []
        return self._repository.masked_daily_field_totals_by_category(
            stream_id, self._mask, start_bucket=start, end_bucket=end
        )
