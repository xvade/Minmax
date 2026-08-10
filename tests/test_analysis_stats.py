"""Tests for :mod:`minmax.analysis.stats`.

Per the architecture doc's rule for this layer, results are checked against
hand-computed fixtures -- not just "assert scipy agrees with itself" for the
p-value, which would catch nothing if scipy's own call were wired up wrong.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("scipy", reason="the correlation engine is an optional extra")

from minmax.analysis.stats import benjamini_hochberg, pearson


class TestPearson:
    def test_hand_computed_r(self) -> None:
        # x = [1,2,3,4,5], y = [2,4,5,4,5].
        # dx = x - mean(x) = [-2,-1,0,1,2]; dy = y - mean(y) = [-2,0,1,0,1].
        # sum(dx*dy) = 4+0+0+0+2 = 6
        # sum(dx^2) = 4+1+0+1+4 = 10
        # sum(dy^2) = 4+0+1+0+1 = 6
        # r = 6 / sqrt(10*6) = 6 / sqrt(60) = 0.7745966692414834
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        result = pearson(x, y)
        assert result.r == pytest.approx(6 / math.sqrt(60), abs=1e-12)
        assert result.n == 5

    def test_p_value_matches_an_independent_closed_form(self) -> None:
        # For n=5 (df=3), the two-sided p-value has a closed form in terms of
        # t = r * sqrt(n-2) / sqrt(1-r^2):
        #   p = 1 - (2/pi) * [atan(t/sqrt(3)) + t*sqrt(3)/(3+t^2)]
        # computed independently of scipy, as a check that the wrapper is
        # actually calling into the right statistic rather than merely
        # returning *a* number.
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 5.0, 4.0, 5.0]
        result = pearson(x, y)

        r = result.r
        n = result.n
        t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
        expected_p = 1 - (2 / math.pi) * (
            math.atan(t / math.sqrt(3)) + t * math.sqrt(3) / (3 + t * t)
        )
        assert result.p_value == pytest.approx(expected_p, abs=1e-9)

    def test_perfect_positive_correlation(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0]
        y = [2.0, 4.0, 6.0, 8.0]
        assert pearson(x, y).r == pytest.approx(1.0)

    def test_perfect_negative_correlation(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0]
        y = [8.0, 6.0, 4.0, 2.0]
        assert pearson(x, y).r == pytest.approx(-1.0)

    def test_mismatched_lengths_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            pearson([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_too_few_points_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            pearson([1.0], [1.0])

    def test_a_constant_series_is_rejected_before_scipy_sees_it(self) -> None:
        with pytest.raises(ValueError, match="constant"):
            pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
        with pytest.raises(ValueError, match="constant"):
            pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0])


class TestBenjaminiHochberg:
    def test_empty_input(self) -> None:
        assert benjamini_hochberg([]) == []

    def test_evenly_spaced_p_values_all_land_on_the_same_q(self) -> None:
        # Each p_(j) * m / j = 0.05 exactly for j = 1..5, so the running
        # minimum never has to correct anything and every q is 0.05.
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        q_values = benjamini_hochberg(p_values)
        assert q_values == [pytest.approx(0.05)] * 5

    def test_monotonicity_is_enforced_against_a_hand_worked_example(self) -> None:
        # p = [0.005, 0.20, 0.03], m = 3. Sorted ascending: 0.005 (rank 1),
        # 0.03 (rank 2), 0.20 (rank 3).
        #   rank 3: candidate = 0.20 * 3/3 = 0.20 -> running_min = 0.20
        #   rank 2: candidate = 0.03 * 3/2 = 0.045 -> running_min = 0.045
        #   rank 1: candidate = 0.005 * 3/1 = 0.015 -> running_min = 0.015
        # So q = [0.015, 0.20, 0.045] in the original order.
        p_values = [0.005, 0.20, 0.03]
        q_values = benjamini_hochberg(p_values)
        assert q_values[0] == pytest.approx(0.015)
        assert q_values[1] == pytest.approx(0.20)
        assert q_values[2] == pytest.approx(0.045)

    def test_a_non_monotone_raw_scaling_still_produces_non_decreasing_q_by_rank(
        self,
    ) -> None:
        # Construct p-values whose raw p*m/rank is NOT already sorted, so the
        # running-minimum correction actually has to do something -- q sorted
        # by ascending p must itself be non-decreasing, which a naive
        # per-element scaling (without the running minimum) would violate.
        p_values = [0.5, 0.001, 0.4, 0.01]
        q_values = benjamini_hochberg(p_values)
        by_p = sorted(zip(p_values, q_values, strict=True))
        qs_in_p_order = [q for _, q in by_p]
        assert qs_in_p_order == sorted(qs_in_p_order)

    def test_q_values_never_exceed_one(self) -> None:
        assert all(q <= 1.0 for q in benjamini_hochberg([0.9, 0.95, 0.99]))
