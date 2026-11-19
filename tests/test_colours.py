"""Manual series colours and the auto colours that step aside for them.

Three layers, each tested where it lives: the pure colour arithmetic and the
auto-assignment rules (``core``), the ``series_colour`` table (``storage``), and
what a client hands the UI (``api``).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from minmax.api import InProcessClient, MinmaxClient
from minmax.core import colourmath, palette
from minmax.core.palette import PALETTE, ColourChoice, resolve_auto
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository

PACIFIC = ZoneInfo("America/Los_Angeles")


class TestColourMath:
    def test_identical_colours_have_no_distance(self) -> None:
        assert colourmath.delta_e("#2a78d6", "#2a78d6") == pytest.approx(0.0)
        assert colourmath.separation("#2a78d6", "#2a78d6") == pytest.approx(0.0)

    def test_distance_is_symmetric(self) -> None:
        forwards = colourmath.delta_e("#eb6834", "#1baf7a")
        assert forwards == pytest.approx(colourmath.delta_e("#1baf7a", "#eb6834"))

    def test_black_and_white_are_a_known_distance_apart(self) -> None:
        # OKLab lightness runs 0..1, so the scaled distance is 100.
        assert colourmath.delta_e("#000000", "#ffffff") == pytest.approx(100.0, abs=0.01)

    def test_colours_a_deutan_reader_confuses_are_not_separated(self) -> None:
        # A saturated red and green differ enormously to normal vision and very
        # little to a reader without green cones; separation() is the minimum.
        normal = colourmath.delta_e("#d62728", "#2ca02c")
        assert normal > 30
        assert colourmath.separation("#d62728", "#2ca02c") < normal / colourmath.CLEAR_NORMAL

    @pytest.mark.parametrize("bad", ["", "#fff", "red", "#12345g", "#1234567"])
    def test_normalise_hex_rejects_anything_but_six_digits(self, bad: str) -> None:
        with pytest.raises(ValueError, match="RRGGBB"):
            colourmath.normalise_hex(bad)

    def test_normalise_hex_lowercases_and_adds_the_hash(self) -> None:
        assert colourmath.normalise_hex("  #2A78D6 ") == "#2a78d6"
        assert colourmath.normalise_hex("2A78D6") == "#2a78d6"


class TestColourChoice:
    def test_a_choice_is_exactly_a_slot_or_a_hex(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ColourChoice()
        with pytest.raises(ValueError, match="exactly one"):
            ColourChoice(slot=1, hex="#ffffff")

    def test_a_slot_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            ColourChoice(slot=len(PALETTE))
        with pytest.raises(ValueError, match="out of range"):
            ColourChoice(slot=-1)

    def test_a_slot_choice_keeps_separate_light_and_dark_steps(self) -> None:
        assert ColourChoice(slot=0).swatch() == PALETTE[0]

    def test_a_custom_hex_is_one_colour_on_both_surfaces(self) -> None:
        swatch = ColourChoice(hex="#FF2D95").swatch()
        assert swatch.light == swatch.dark == "#ff2d95"

    def test_a_bad_hex_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="RRGGBB"):
            ColourChoice(hex="pink")


def _homes(keys: list[str]) -> dict[str, int]:
    """Each key's home slot, the way a fresh stream assigns them."""
    return {key: index % len(PALETTE) for index, key in enumerate(sorted(keys))}


class TestResolveAuto:
    def test_with_nothing_chosen_every_series_keeps_its_home_slot(self) -> None:
        keys = list("abcdefgh")
        assert resolve_auto(keys, _homes(keys), []) == _homes(keys)

    def test_colours_stay_put_when_no_manual_colour_is_near(self) -> None:
        # Yellow is at least 10 from Orange and Red and far from the rest, so
        # of these four homes only a series sitting on Yellow itself would move.
        keys = ["a", "b", "c", "d"]
        homes = {"a": 0, "b": 2, "c": 5, "d": 6}
        assert resolve_auto(keys, homes, [PALETTE[3]]) == homes

    def test_a_manual_pick_of_a_slot_displaces_only_that_slots_owner(self) -> None:
        keys = ["a", "b", "c", "d"]
        homes = {"a": 0, "b": 2, "c": 5, "d": 3}
        resolved = resolve_auto(keys, homes, [PALETTE[0]])  # Blue picked by hand
        assert resolved["b"] == 2 and resolved["c"] == 5 and resolved["d"] == 3
        assert resolved["a"] != 0

    @pytest.mark.parametrize("picked", range(len(PALETTE)))
    def test_no_auto_series_takes_a_hue_somebody_picked_when_one_is_spare(
        self, picked: int
    ) -> None:
        keys = [f"s{i}" for i in range(len(PALETTE) - 1)]  # one slot spare
        resolved = resolve_auto(keys, _homes(keys), [PALETTE[picked]])
        assert picked not in resolved.values()
        assert len(set(resolved.values())) == len(keys)

    def test_a_custom_colour_near_a_hue_pushes_that_hue_out(self) -> None:
        near_blue = ColourChoice(hex="#2f80ed").swatch()
        keys = [f"s{i}" for i in range(len(PALETTE) - 1)]
        resolved = resolve_auto(keys, _homes(keys), [near_blue])
        assert 0 not in resolved.values()

    def test_a_displaced_series_takes_the_hue_furthest_from_what_is_fixed(self) -> None:
        # Orange picked by hand, and the only auto series lived on Orange: it
        # must land on whichever hue is visibly furthest from it, on the worse of
        # the two surfaces.
        def gap(slot: int) -> float:
            return min(
                colourmath.delta_e(PALETTE[slot].light, PALETTE[1].light),
                colourmath.delta_e(PALETTE[slot].dark, PALETTE[1].dark),
            )

        furthest = max(range(len(PALETTE)), key=gap)
        assert resolve_auto(["mine"], {"mine": 1}, [PALETTE[1]]) == {"mine": furthest}

    def test_the_result_does_not_depend_on_the_order_series_are_listed_in(self) -> None:
        keys = [f"s{i}" for i in range(6)]
        homes = _homes(keys)
        manual = [PALETTE[2], ColourChoice(hex="#ff2d95").swatch()]
        forwards = resolve_auto(keys, homes, manual)
        backwards = resolve_auto(list(reversed(keys)), homes, manual)
        assert forwards == backwards

    def test_two_series_stored_with_the_same_slot_are_told_apart(self) -> None:
        # More than eight stored groups wrap their slots; the eight on screen
        # at once must still all differ.
        keys = ["a", "b"]
        resolved = resolve_auto(keys, {"a": 3, "b": 3}, [])
        assert resolved["a"] == 3
        assert resolved["b"] != 3

    def test_a_series_with_no_stored_slot_still_gets_one(self) -> None:
        assert resolve_auto(["new"], {}, []) != {}

    def test_more_series_than_hues_reuses_slots_instead_of_failing(self) -> None:
        keys = [f"s{i:02d}" for i in range(len(PALETTE) + 3)]
        resolved = resolve_auto(keys, {}, [])
        assert set(resolved) == set(keys)
        assert set(resolved.values()) == set(range(len(PALETTE)))

    def test_with_no_series_there_is_nothing_to_resolve(self) -> None:
        assert resolve_auto([], {}, [PALETTE[0]]) == {}

    def test_manual_colours_need_not_be_palette_colours(self) -> None:
        # Hot pink is nowhere near a palette hue except Magenta.
        keys = [f"s{i}" for i in range(6)]
        pink = ColourChoice(hex="#ff2d95").swatch()
        resolved = resolve_auto(keys, _homes(keys), [pink])
        assert 4 not in resolved.values()


class TestRepositorySeriesColours:
    def test_nothing_is_stored_until_somebody_picks(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        assert repository.series_colours(stream.id) == {}

    def test_a_pick_round_trips_for_both_kinds_of_choice(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        repository.set_series_colour(stream.id, "work", ColourChoice(slot=3))
        repository.set_series_colour(stream.id, "work/deep", ColourChoice(hex="#FF2D95"))
        assert repository.series_colours(stream.id) == {
            "work": ColourChoice(slot=3),
            "work/deep": ColourChoice(hex="#ff2d95"),
        }

    def test_picking_again_replaces_the_earlier_choice(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        repository.set_series_colour(stream.id, "work", ColourChoice(slot=3))
        repository.set_series_colour(stream.id, "work", ColourChoice(hex="#123456"))
        assert repository.series_colours(stream.id) == {"work": ColourChoice(hex="#123456")}

    def test_none_puts_a_series_back_on_auto(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        repository.set_series_colour(stream.id, "work", ColourChoice(slot=3))
        repository.set_series_colour(stream.id, "work", None)
        assert repository.series_colours(stream.id) == {}

    def test_clearing_a_series_that_was_never_set_is_harmless(
        self, repository: Repository, home: Path, sample_csv: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        repository.set_series_colour(stream.id, "never", None)

    def test_the_database_itself_refuses_a_row_with_both_or_neither(
        self, repository: Repository, connection: sqlite3.Connection, sample_csv: Path, home: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        for slot, hex_colour in ((1, "#ffffff"), (None, None), (None, "ffffff"), (-1, None)):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO series_colour (stream_id, category_path, slot, hex, chosen_at)"
                    " VALUES (?, 'x', ?, ?, 0)",
                    (stream.id, slot, hex_colour),
                )

    def test_deleting_a_stream_deletes_its_colour_choices(
        self, repository: Repository, connection: sqlite3.Connection, sample_csv: Path, home: Path
    ) -> None:
        run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
        stream = repository.get_stream("timecard")
        assert stream is not None and stream.id is not None
        repository.set_series_colour(stream.id, "work", ColourChoice(slot=1))
        repository.delete_stream("timecard")
        left = connection.execute("SELECT COUNT(*) FROM series_colour").fetchone()[0]
        assert left == 0


@pytest.fixture
def colour_client(repository: Repository, home: Path, sample_csv: Path) -> MinmaxClient:
    run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
    return InProcessClient(repository, timezone=PACIFIC, home=home)


def _top_level_swatches(client: MinmaxClient) -> dict[str, palette.Swatch]:
    totals = client.range_totals("timecard", date(2026, 6, 5), date(2026, 6, 6))
    return {s.top_level: s.swatch for s in totals.slices}


class TestClientSeriesColours:
    def test_series_report_their_path_and_that_they_are_on_auto(
        self, colour_client: MinmaxClient
    ) -> None:
        view = colour_client.trends("timecard")
        real = [s for s in view.series if not s.is_other]
        assert real
        assert all(s.path == s.label and s.colour is None for s in real)

    def test_a_manual_pick_is_what_the_chart_draws(self, colour_client: MinmaxClient) -> None:
        group = colour_client.trends("timecard").series[0].path
        colour_client.set_series_colour("timecard", group, ColourChoice(hex="#ff2d95"))

        series = next(s for s in colour_client.trends("timecard").series if s.path == group)
        assert series.swatch.light == "#ff2d95" == series.swatch.dark
        assert series.colour == ColourChoice(hex="#ff2d95")

    def test_a_manual_pick_is_honoured_on_the_day_timeline_too(
        self, colour_client: MinmaxClient
    ) -> None:
        group = colour_client.trends("timecard").series[0].path
        colour_client.set_series_colour("timecard", group, ColourChoice(slot=6))
        assert _top_level_swatches(colour_client)[group] == PALETTE[6]

    def test_no_other_series_shares_a_manual_colour(self, colour_client: MinmaxClient) -> None:
        group = colour_client.trends("timecard").series[0].path
        colour_client.set_series_colour("timecard", group, ColourChoice(slot=6))
        swatches = _top_level_swatches(colour_client)
        assert len(set(swatches.values())) == len(swatches)

    def test_going_back_to_auto_restores_the_original_colours(
        self, colour_client: MinmaxClient
    ) -> None:
        before = _top_level_swatches(colour_client)
        group = next(iter(before))
        colour_client.set_series_colour("timecard", group, ColourChoice(hex="#ff2d95"))
        assert _top_level_swatches(colour_client) != before
        colour_client.set_series_colour("timecard", group, None)
        assert _top_level_swatches(colour_client) == before

    def test_series_that_do_not_clash_keep_their_colour(self, colour_client: MinmaxClient) -> None:
        before = _top_level_swatches(colour_client)
        group = next(iter(before))
        picked = PALETTE[6]
        colour_client.set_series_colour("timecard", group, ColourChoice(slot=6))
        after = _top_level_swatches(colour_client)

        def clear_of_picked(swatch: palette.Swatch) -> bool:
            return (
                colourmath.separation(swatch.light, picked.light) >= palette.CLEAR_SEPARATION
                and colourmath.separation(swatch.dark, picked.dark) >= palette.CLEAR_SEPARATION
            )

        others = [name for name in before if name != group]
        unaffected = [name for name in others if clear_of_picked(before[name])]
        assert unaffected, "the sample should have at least one group clear of violet"
        for name in unaffected:
            assert after[name] == before[name]

    def test_a_pick_for_a_series_not_on_screen_moves_nothing(
        self, colour_client: MinmaxClient
    ) -> None:
        before = _top_level_swatches(colour_client)
        colour_client.set_series_colour("timecard", "not-a-group-anyone-has", ColourChoice(slot=0))
        assert _top_level_swatches(colour_client) == before

    def test_an_unknown_stream_is_reported(self, colour_client: MinmaxClient) -> None:
        with pytest.raises(LookupError):
            colour_client.set_series_colour("nope", "x", None)

    def test_a_focused_sub_category_can_be_coloured_on_its_own_path(
        self, colour_client: MinmaxClient
    ) -> None:
        parent = next(s for s in colour_client.trends("timecard").series if s.can_drill)
        child = colour_client.trends("timecard", prefix=parent.path).series[0]
        assert "/" in child.path

        colour_client.set_series_colour("timecard", child.path, ColourChoice(hex="#ff2d95"))

        focused = colour_client.trends("timecard", prefix=parent.path)
        assert next(s for s in focused.series if s.path == child.path).swatch.light == "#ff2d95"
        # The parent's own colour, at the top level, is a different entity.
        top = next(s for s in colour_client.trends("timecard").series if s.path == parent.path)
        assert top.colour is None

    def test_other_cannot_be_coloured(self, colour_client: MinmaxClient) -> None:
        view = colour_client.trends("timecard", max_series=1)
        other = next(s for s in view.series if s.is_other)
        assert other.path == "" and other.colour is None and other.swatch == palette.OTHER
