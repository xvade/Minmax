"""A set of disjoint time spans -- the primitive a view is built from.

Every other range in Minmax is contiguous: one ``(start_epoch, end_epoch)``
pair, one ``start_bucket``/``end_bucket`` pair. A view needs something wider --
"only while `work` was running" is generally *several* separate stretches
across a day, a week, a year. Nothing like that exists elsewhere in the
codebase; the only clipping anywhere is a single-range inline ``max``/``min``
(``core/timeline.py``'s pixel geometry, and the ``MIN(ended_at, ?)`` arithmetic
in ``storage/repository.py``'s ``totals_by_category``). This module is that
primitive, built once so ``storage/masked.py`` does not reinvent interval-set
arithmetic.

Pure and Qt/SQL-free, like the rest of ``core``. Every span is a half-open
``[start, end)`` epoch-second pair, matching the convention every raw-row
reader already uses (``events_in_range``'s ``occurred_at >= ? AND < ?``,
``intervals_in_range``'s overlap test).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["TimeMask"]


@dataclass(frozen=True, slots=True)
class TimeMask:
    """A sorted, disjoint set of half-open epoch spans.

    Construct via :meth:`from_spans`, never by passing ``spans`` directly --
    every other method assumes the sorted-and-merged invariant already holds.
    """

    spans: tuple[tuple[int, int], ...]

    @classmethod
    def from_spans(cls, spans: Iterable[tuple[int, int]]) -> TimeMask:
        """Sort, drop empty/inverted spans, and merge touching or overlapping ones.

        Touching spans merge (``[0, 10)`` and ``[10, 20)`` become ``[0, 20)``):
        two back-to-back runs of the same category are one uninterrupted
        stretch of activity, not a one-second gap a downstream record could
        get split across.
        """
        ordered = sorted(span for span in spans if span[1] > span[0])
        merged: list[tuple[int, int]] = []
        for start, end in ordered:
            if merged and start <= merged[-1][1]:
                prior_start, prior_end = merged[-1]
                merged[-1] = (prior_start, max(prior_end, end))
            else:
                merged.append((start, end))
        return cls(tuple(merged))

    def is_empty(self) -> bool:
        return not self.spans

    def bounds(self) -> tuple[int, int] | None:
        """The earliest start and latest end across every span, or ``None`` if empty."""
        if not self.spans:
            return None
        return self.spans[0][0], self.spans[-1][1]

    def contains(self, epoch: int) -> bool:
        """Whether ``epoch`` falls in some span, half-open at both ends."""
        index = _bisect_span(self.spans, epoch)
        if index == 0:
            return False
        start, end = self.spans[index - 1]
        return start <= epoch < end

    def clip(self, start: int, end: int) -> list[tuple[int, int]]:
        """The pieces of ``[start, end)`` that survive the mask, in order.

        Each returned piece is the intersection of ``[start, end)`` with one
        span -- an input range straddling a gap yields several pieces, which
        is exactly what a record that was running while the view's category
        started or stopped needs.
        """
        if end <= start:
            return []
        pieces: list[tuple[int, int]] = []
        for span_start, span_end in self.spans:
            if span_end <= start:
                continue
            if span_start >= end:
                break
            pieces.append((max(span_start, start), min(span_end, end)))
        return pieces

    def covered_seconds(self, start: int, end: int) -> int:
        """Total seconds of ``[start, end)`` that survive the mask."""
        return sum(piece_end - piece_start for piece_start, piece_end in self.clip(start, end))

    def restricted_to(self, start: int, end: int) -> TimeMask:
        """The mask's own spans, clipped to ``[start, end)``.

        Unlike :meth:`clip`, the input is the mask's own spans rather than an
        arbitrary caller range, so this is for narrowing a mask to a query
        window before repeatedly clipping many records against it.
        """
        return TimeMask(tuple(self.clip(start, end)))


def _bisect_span(spans: tuple[tuple[int, int], ...], epoch: int) -> int:
    """Index of the first span whose start exceeds ``epoch`` (``bisect_right`` by start)."""
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        if spans[mid][0] <= epoch:
            lo = mid + 1
        else:
            hi = mid
    return lo
