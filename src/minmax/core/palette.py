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

There are deliberately only eight. Past eight, adjacent hues stop being
distinguishable under colour-vision deficiency, so a chart folds its tail into
:data:`OTHER` rather than inventing a ninth hue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["OTHER", "PALETTE", "Swatch", "next_slot", "swatch_for_slot", "text_colour_on"]


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


def text_colour_on(background: str) -> str:
    """Black or white, whichever stays readable on ``background``.

    Uses the WCAG relative-luminance threshold so the choice is a contrast
    judgement rather than a guess.
    """
    red, green, blue = _to_linear_rgb(background)
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "#000000" if luminance > 0.179 else "#FFFFFF"


def _to_linear_rgb(hex_colour: str) -> tuple[float, float, float]:
    text = hex_colour.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"{hex_colour!r} is not a #RRGGBB colour")

    channels = []
    for offset in (0, 2, 4):
        value = int(text[offset : offset + 2], 16) / 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return channels[0], channels[1], channels[2]
