"""Tests for :class:`~minmax.core.windows.TimeMask`.

The only interval-set primitive in the codebase, so its edge cases -- touching
spans, zero-length/inverted spans, clipping across a gap -- are covered here
rather than being rediscovered later inside a masked-rollup query.
"""

from __future__ import annotations

from minmax.core.windows import TimeMask


class TestFromSpans:
    def test_overlapping_spans_merge_into_one(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (5, 15)])
        assert mask.spans == ((0, 15),)

    def test_nested_spans_merge_into_the_outer_one(self) -> None:
        mask = TimeMask.from_spans([(0, 20), (5, 10)])
        assert mask.spans == ((0, 20),)

    def test_touching_spans_merge_with_no_gap(self) -> None:
        # Two back-to-back runs of the same category are one uninterrupted
        # stretch, not a one-second gap a record could get split across.
        mask = TimeMask.from_spans([(0, 10), (10, 20)])
        assert mask.spans == ((0, 20),)

    def test_disjoint_spans_stay_separate_and_sorted(self) -> None:
        mask = TimeMask.from_spans([(100, 110), (0, 10)])
        assert mask.spans == ((0, 10), (100, 110))

    def test_a_zero_length_span_vanishes(self) -> None:
        mask = TimeMask.from_spans([(5, 5)])
        assert mask.spans == ()

    def test_an_inverted_span_vanishes(self) -> None:
        mask = TimeMask.from_spans([(10, 5)])
        assert mask.spans == ()

    def test_no_spans_at_all_is_a_valid_empty_mask(self) -> None:
        mask = TimeMask.from_spans([])
        assert mask.is_empty()


class TestBounds:
    def test_bounds_of_an_empty_mask_is_none(self) -> None:
        assert TimeMask.from_spans([]).bounds() is None

    def test_bounds_spans_every_span(self) -> None:
        mask = TimeMask.from_spans([(50, 60), (0, 10)])
        assert mask.bounds() == (0, 60)


class TestContains:
    def test_the_start_of_a_span_is_inside(self) -> None:
        mask = TimeMask.from_spans([(10, 20)])
        assert mask.contains(10) is True

    def test_the_end_of_a_span_is_outside(self) -> None:
        # Half-open, matching events_in_range's convention.
        mask = TimeMask.from_spans([(10, 20)])
        assert mask.contains(20) is False

    def test_a_point_in_the_gap_between_two_spans_is_outside(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (20, 30)])
        assert mask.contains(15) is False

    def test_a_point_after_every_span_is_outside(self) -> None:
        mask = TimeMask.from_spans([(0, 10)])
        assert mask.contains(1000) is False

    def test_a_point_before_every_span_is_outside(self) -> None:
        mask = TimeMask.from_spans([(100, 200)])
        assert mask.contains(0) is False


class TestClip:
    def test_a_range_fully_inside_one_span_is_unchanged(self) -> None:
        mask = TimeMask.from_spans([(0, 100)])
        assert mask.clip(10, 20) == [(10, 20)]

    def test_a_range_straddling_a_gap_splits_into_two_pieces(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (20, 30)])
        assert mask.clip(5, 25) == [(5, 10), (20, 25)]

    def test_a_range_entirely_in_a_gap_yields_nothing(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (20, 30)])
        assert mask.clip(12, 18) == []

    def test_an_inverted_range_yields_nothing(self) -> None:
        mask = TimeMask.from_spans([(0, 100)])
        assert mask.clip(50, 40) == []

    def test_a_range_touching_three_spans_yields_three_pieces(self) -> None:
        mask = TimeMask.from_spans([(0, 5), (10, 15), (20, 25)])
        assert mask.clip(0, 25) == [(0, 5), (10, 15), (20, 25)]


class TestCoveredSeconds:
    def test_full_coverage_equals_the_whole_range(self) -> None:
        mask = TimeMask.from_spans([(0, 100)])
        assert mask.covered_seconds(0, 100) == 100

    def test_a_gap_is_subtracted(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (20, 30)])
        assert mask.covered_seconds(0, 30) == 20

    def test_no_overlap_at_all_is_zero(self) -> None:
        mask = TimeMask.from_spans([(1000, 2000)])
        assert mask.covered_seconds(0, 10) == 0


class TestRestrictedTo:
    def test_spans_outside_the_window_are_dropped(self) -> None:
        mask = TimeMask.from_spans([(0, 10), (1000, 1010)])
        assert mask.restricted_to(0, 100).spans == ((0, 10),)

    def test_a_span_crossing_the_window_edge_is_clipped(self) -> None:
        mask = TimeMask.from_spans([(0, 100)])
        assert mask.restricted_to(20, 40).spans == ((20, 40),)
