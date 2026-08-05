"""Tests for instants and local-time bucketing.

The bucketing rules are the least obvious and most consequential logic in
``core``: get them wrong and every daily total is quietly off, with nothing to
signal it. They are therefore tested against hand-computed expectations rather
than against the implementation's own output.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from minmax.core.timeref import Instant, Period, split_by_local_period

HOUR = 3600
DAY = 86_400
PDT = -7 * HOUR
PST = -8 * HOUR


def at(text: str) -> Instant:
    return Instant.from_iso(text)


class TestInstant:
    def test_parses_iso_with_offset(self) -> None:
        instant = at("2026-06-05T23:00:00-07:00")
        assert instant.utc_offset_seconds == PDT
        assert instant.local_date.isoformat() == "2026-06-05"

    def test_rejects_naive_datetime(self) -> None:
        # Guessing a timezone is exactly the silent error this type prevents.
        with pytest.raises(ValueError, match="naive"):
            Instant.from_datetime(datetime(2026, 6, 5, 23, 0))

    def test_rejects_impossible_offset(self) -> None:
        with pytest.raises(ValueError, match="outside the range"):
            Instant(epoch_seconds=0, utc_offset_seconds=20 * HOUR)

    def test_local_date_reflects_the_offset_not_utc(self) -> None:
        # 23:00 on the 5th in PDT is already the 6th in UTC. The user's answer
        # to "when was this?" is the 5th, and that is what must be stored.
        instant = at("2026-06-05T23:00:00-07:00")
        assert instant.to_utc_datetime().date().isoformat() == "2026-06-06"
        assert instant.local_date.isoformat() == "2026-06-05"

    def test_same_moment_in_two_offsets_is_distinct_but_sorts_chronologically(self) -> None:
        pacific = at("2026-06-05T12:00:00-07:00")
        utc = at("2026-06-05T19:00:00+00:00")
        assert pacific.epoch_seconds == utc.epoch_seconds
        # Not equal: the local reading differs, and preserving it is the point.
        assert pacific != utc
        # Ordering is still chronological across differing offsets.
        earlier = at("2026-06-05T11:00:00-07:00")
        assert earlier < pacific and earlier < utc
        assert sorted([utc, earlier])[0] == earlier

    def test_bucket_key_formats_sort_chronologically(self) -> None:
        earlier = at("2026-06-05T23:00:00-07:00")
        later = at("2026-06-06T00:30:00-07:00")
        assert earlier.bucket_key(Period.DAY) == "2026-06-05"
        assert later.bucket_key(Period.HOUR) == "2026-06-06T00"
        assert earlier.bucket_key(Period.DAY) < later.bucket_key(Period.DAY)
        assert earlier.bucket_key(Period.HOUR) < later.bucket_key(Period.HOUR)


class TestSplitByLocalPeriod:
    def test_interval_inside_one_day_is_one_bucket(self) -> None:
        pieces = split_by_local_period(
            at("2026-06-06T09:00:00-07:00"), at("2026-06-06T10:30:00-07:00"), Period.DAY
        )
        assert pieces == [("2026-06-06", 90 * 60)]

    def test_midnight_crossing_run_splits_across_days(self) -> None:
        # The case that makes this function necessary: 29 of the 338 runs in the
        # first real export cross local midnight.
        pieces = split_by_local_period(
            at("2026-06-05T23:00:00-07:00"), at("2026-06-06T07:00:00-07:00"), Period.DAY
        )
        assert pieces == [("2026-06-05", HOUR), ("2026-06-06", 7 * HOUR)]

    def test_hour_buckets_split_on_the_hour(self) -> None:
        pieces = split_by_local_period(
            at("2026-06-06T07:40:00-07:00"), at("2026-06-06T09:10:00-07:00"), Period.HOUR
        )
        assert pieces == [
            ("2026-06-06T07", 20 * 60),
            ("2026-06-06T08", HOUR),
            ("2026-06-06T09", 10 * 60),
        ]

    def test_multi_day_run_fills_whole_days_in_between(self) -> None:
        pieces = split_by_local_period(
            at("2026-06-05T22:00:00-07:00"), at("2026-06-08T02:00:00-07:00"), Period.DAY
        )
        assert pieces == [
            ("2026-06-05", 2 * HOUR),
            ("2026-06-06", DAY),
            ("2026-06-07", DAY),
            ("2026-06-08", 2 * HOUR),
        ]

    @pytest.mark.parametrize("period", list(Period))
    def test_pieces_always_sum_to_the_full_duration(self, period: Period) -> None:
        start, end = at("2026-06-05T22:13:07-07:00"), at("2026-06-08T02:41:52-07:00")
        pieces = split_by_local_period(start, end, period)
        total = end.epoch_seconds - start.epoch_seconds
        assert sum(seconds for _, seconds in pieces) == total

    def test_buckets_are_unique_and_ordered(self) -> None:
        pieces = split_by_local_period(
            at("2026-06-05T22:00:00-07:00"), at("2026-06-08T02:00:00-07:00"), Period.HOUR
        )
        keys = [key for key, _ in pieces]
        assert keys == sorted(keys)
        assert len(keys) == len(set(keys))

    def test_dst_transition_keeps_the_total_exact(self) -> None:
        # A sleep run through the US fall-back night: the clock reads 23:00 to
        # 07:00 (8 wall hours) but 9 real hours elapse. The stored offsets make
        # both facts recoverable, and attribution must use the real elapsed time.
        start = at("2026-10-31T23:00:00-07:00")
        end = at("2026-11-01T07:00:00-08:00")
        assert end.epoch_seconds - start.epoch_seconds == 9 * HOUR

        pieces = split_by_local_period(start, end, Period.DAY)
        assert pieces == [("2026-10-31", HOUR), ("2026-11-01", 8 * HOUR)]

    def test_empty_and_inverted_intervals_yield_nothing(self) -> None:
        moment = at("2026-06-06T09:00:00-07:00")
        assert split_by_local_period(moment, moment, Period.DAY) == []
        assert split_by_local_period(at("2026-06-06T10:00:00-07:00"), moment, Period.DAY) == []


class TestPeriod:
    def test_floor_handles_pre_epoch_wall_seconds(self) -> None:
        # Floor division, not truncation: -1 second must floor to the start of
        # the previous day, not to zero.
        assert Period.DAY.floor_wall_seconds(-1) == -DAY
        assert Period.HOUR.floor_wall_seconds(-1) == -HOUR
        assert Period.DAY.floor_wall_seconds(DAY + 5) == DAY
