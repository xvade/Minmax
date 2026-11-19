"""The colour palette used to distinguish top-level category groups.

Timecard gives colour to each **top-level folder** rather than to each timer, so
a whole branch reads as one colour at a glance. Minmax keeps that model: the
colour belongs to the first component of a category path (``media`` for
``media/gaming/chess``).

Three properties this module exists to guarantee:

**Colour follows the entity, not its rank.** A group is assigned a *slot* once
and that assignment is stored (see the ``group_color`` table). Filtering,
scrolling, or re-ranking never repaints a group the reader has already learned.

**Slots, not hex.** Storage keeps the slot index, so each slot can carry
different steps for light and dark surfaces -- a dark theme needs its own steps
selected against the dark surface, not an automatic inversion of the light ones.
It also means the palette can be re-stepped later without a data migration.

**The steps are validated, not eyeballed.** Both columns below clear the
lightness band, chroma floor, adjacent-pair colour-vision-deficiency separation
(worst ΔE 9.1 light / 8.4 dark against a target of 8), and the normal-vision
floor (19.6 / 19.3 against a floor of 15). Three light steps sit below 3:1
contrast against a light surface, which obliges visible labels rather than
colour alone -- the trends legend and the timeline's in-block labels supply that.

**The user can override.** A series' colour is *auto* (no stored choice) or a
:class:`ColourChoice` -- one of the eight slots, or any hex the user picked. Auto
colours are not free-floating: :func:`resolve_auto` keeps each group's stored
slot unless a manual colour on screen is too close to it, in which case only
that group moves, to the free hue furthest from everything else displayed.

There are deliberately only eight. Past eight, adjacent hues stop being
distinguishable under colour-vision deficiency, so a chart folds its tail into
:data:`OTHER` rather than inventing a ninth hue.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from minmax.core.colourmath import delta_e, linear_rgb, normalise_hex, separation

__all__ = [
    "CLEAR_SEPARATION",
    "OTHER",
    "PALETTE",
    "SLOT_NAMES",
    "ColourChoice",
    "Swatch",
    "next_slot",
    "resolve_auto",
    "swatch_for_slot",
    "text_colour_on",
]


@dataclass(frozen=True, slots=True)
class Swatch:
    """One categorical colour, stepped for each surface."""

    light: str
    dark: str

    def for_surface(self, *, dark: bool) -> str:
        return self.dark if dark else self.light


#: Fixed assignment order. Never cycled within a chart; never reordered, since
#: the stored slot index refers to a position here.
PALETTE: tuple[Swatch, ...] = (
    Swatch(light="#2a78d6", dark="#3987e5"),  # blue
    Swatch(light="#eb6834", dark="#d95926"),  # orange
    Swatch(light="#1baf7a", dark="#199e70"),  # aqua
    Swatch(light="#eda100", dark="#c98500"),  # yellow
    Swatch(light="#e87ba4", dark="#d55181"),  # magenta
    Swatch(light="#008300", dark="#008300"),  # green
    Swatch(light="#4a3aa7", dark="#9085e9"),  # violet
    Swatch(light="#e34948", dark="#e66767"),  # red
)

#: What to call each slot, for a picker's tooltips and a screen reader. Parallel
#: to :data:`PALETTE`.
SLOT_NAMES: tuple[str, ...] = (
    "Blue",
    "Orange",
    "Aqua",
    "Yellow",
    "Magenta",
    "Green",
    "Violet",
    "Red",
)

#: How far (see :func:`~minmax.core.colourmath.separation`) an auto colour must
#: stay from a colour the user chose. ``1.0`` is exactly the "clear" thresholds.
CLEAR_SEPARATION = 1.0


@dataclass(frozen=True, slots=True)
class ColourChoice:
    """A colour the user picked: one palette slot, or an arbitrary hex.

    Exactly one field is set. A slot keeps its separate light and dark steps; a
    custom hex is one colour on both surfaces, because there is no principled way
    to re-step an arbitrary colour for a dark surface without changing which
    colour the user chose.
    """

    slot: int | None = None
    hex: str | None = None

    def __post_init__(self) -> None:
        if (self.slot is None) == (self.hex is None):
            raise ValueError("a colour choice is exactly one of a slot or a hex")
        if self.slot is not None and not 0 <= self.slot < len(PALETTE):
            raise ValueError(f"palette slot {self.slot} is out of range")
        if self.hex is not None:
            object.__setattr__(self, "hex", normalise_hex(self.hex))

    def swatch(self) -> Swatch:
        if self.slot is not None:
            return PALETTE[self.slot]
        assert self.hex is not None  # guaranteed by __post_init__
        return Swatch(light=self.hex, dark=self.hex)


#: The folded tail. Deliberately neutral: "Other" is not an identity, and giving
#: it a hue would imply it were one more category alongside the rest.
OTHER = Swatch(light="#8c8c8c", dark="#8c8c8c")


def swatch_for_slot(slot: int) -> Swatch:
    """The swatch for a stored slot index.

    Slots beyond the palette wrap. That only happens for a stream with more than
    eight top-level groups, and only affects views that show every group at once
    -- charts cap their series and fold the rest into :data:`OTHER` instead.
    """
    return PALETTE[slot % len(PALETTE)]


def next_slot(already_assigned: Iterable[int]) -> int:
    """Pick the slot for a new group.

    Returns the least-used slot, breaking ties by palette order, so the first
    eight groups take the eight hues in their fixed order and any further groups
    spread evenly rather than piling onto one.
    """
    counts = dict.fromkeys(range(len(PALETTE)), 0)
    for slot in already_assigned:
        if slot in counts:
            counts[slot] += 1
    return min(counts, key=lambda slot: (counts[slot], slot))


def _apart(first: Swatch, second: Swatch) -> float:
    """Separation of two swatches on their worst surface."""
    return min(
        separation(first.light, second.light),
        separation(first.dark, second.dark),
    )


def _visual_gap(first: Swatch, second: Swatch) -> float:
    """Plain normal-vision distance between two swatches, on their worst surface."""
    return min(delta_e(first.light, second.light), delta_e(first.dark, second.dark))


def resolve_auto(
    auto_keys: Sequence[str],
    home_slots: Mapping[str, int],
    manual: Sequence[Swatch],
) -> dict[str, int]:
    """Choose a palette slot for each auto-coloured series on screen.

    ``auto_keys`` are the series with no manual colour, ``home_slots`` the slot
    each was stored with (see :func:`next_slot`), and ``manual`` the swatches of
    every series on screen the user coloured by hand. Only what is displayed
    together matters: a manual colour on a series not on screen constrains nothing.

    Two rules, in order:

    1. **Stay put.** A series keeps its home slot unless a manual colour is
       closer than :data:`CLEAR_SEPARATION` to it, or another auto series already
       holds it. Colour follows the entity, so a pick elsewhere never repaints a
       series it does not collide with.
    2. **Otherwise go furthest.** The displaced series are given the unused
       slots in order of how visibly far each is (plain colour distance, not the
       stricter test above) from its closest *fixed* colour -- the
       manual colours and the auto series that stayed put. Ranking against the
       fixed set only, rather than picking one series at a time, is what leaves
       the slots that clash with a manual colour for last: a series-by-series
       pick would spend a clashing slot early on and strand a clear one. Ties
       go to the lower slot.

    Series are visited in sorted order so the result never depends on the order
    the caller happened to list them in. If there are more auto series than slots
    the least-used slots are reused rather than failing.
    """
    chosen: dict[str, int] = {}
    uses = dict.fromkeys(range(len(PALETTE)), 0)

    def clear_of_manual(slot: int) -> bool:
        return all(_apart(PALETTE[slot], other) >= CLEAR_SEPARATION for other in manual)

    ordered = sorted(auto_keys)
    displaced: list[str] = []
    for key in ordered:
        home = home_slots.get(key)
        if (
            home is not None
            and 0 <= home < len(PALETTE)
            and uses[home] == 0
            and clear_of_manual(home)
        ):
            chosen[key] = home
            uses[home] += 1
        else:
            displaced.append(key)

    fixed = [*manual, *(PALETTE[slot] for slot in chosen.values())]

    def room_from_fixed(slot: int) -> float:
        return min((_visual_gap(PALETTE[slot], other) for other in fixed), default=float("inf"))

    room = {slot: room_from_fixed(slot) for slot in range(len(PALETTE))}
    for key in displaced:
        # Unused slots first, then by room; reuse only begins once every slot
        # has been given out.
        best = min(uses, key=lambda slot: (uses[slot], -room[slot], slot))
        chosen[key] = best
        uses[best] += 1

    return chosen


def text_colour_on(background: str) -> str:
    """Black or white, whichever stays readable on ``background``.

    Uses the WCAG relative-luminance threshold so the choice is a contrast
    judgement rather than a guess.
    """
    red, green, blue = linear_rgb(background)
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "#000000" if luminance > 0.179 else "#FFFFFF"
