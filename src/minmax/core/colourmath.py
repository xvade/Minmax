"""Colour arithmetic: how far apart do two colours look?

Pure functions, no I/O and no Qt. The metric is the one the categorical palette
was validated with (see :mod:`minmax.core.palette`): Euclidean distance in
OKLab, scaled by 100, and for readers with colour-vision deficiency the same
distance after a Machado, Oliveira & Fernandes (2009) simulation of protan and
deutan vision.

The two checks have different thresholds, so comparing raw distances would mix
units. :func:`separation` folds them into one number where ``1.0`` means "just
clear enough to tell apart", which lets a caller ask the question with a single
comparison.

The thresholds are deliberately *lenient* ones -- :data:`CLEAR_NORMAL` and
:data:`CLEAR_CVD` -- not the palette's own validation targets (normal-vision
15, CVD 8). The palette's eight hues were chosen against those targets only for
*adjacent* pairs. A colour the user picked by hand can be anything, and holding
the other series to the strict targets displaces most of the palette for almost
any pick: a plain red would be "too close" to orange, magenta, aqua and green,
moving six series for one change. The lenient floor moves only the series that
actually clash.
"""

from __future__ import annotations

__all__ = [
    "CLEAR_CVD",
    "CLEAR_NORMAL",
    "delta_e",
    "linear_rgb",
    "normalise_hex",
    "separation",
]

#: Smallest normal-vision OKLab ΔE (x100) at which two colours count as clear.
CLEAR_NORMAL = 12.0

#: Smallest ΔE (x100) under simulated protan and deutan vision. This is the
#: palette validator's own "floor" band for colour-vision deficiency, which it
#: accepts when a secondary encoding carries identity -- here the legend's labels.
CLEAR_CVD = 6.0

# Machado, Oliveira & Fernandes (2009), severity 1.0, applied to linear RGB.
_PROTAN = (
    (0.152286, 1.052583, -0.204868),
    (0.114503, 0.786281, 0.099216),
    (-0.003882, -0.048116, 1.051998),
)
_DEUTAN = (
    (0.367322, 0.860646, -0.227968),
    (0.280085, 0.672501, 0.047413),
    (-0.011820, 0.042940, 0.968881),
)

_Triple = tuple[float, float, float]


def normalise_hex(text: str) -> str:
    """``#RRGGBB`` in lower case, or ``ValueError`` if ``text`` is not one."""
    stripped = text.strip()
    body = stripped.removeprefix("#")
    if len(body) != 6 or any(char not in "0123456789abcdefABCDEF" for char in body):
        raise ValueError(f"{text!r} is not a #RRGGBB colour")
    return "#" + body.lower()


def linear_rgb(hex_colour: str) -> _Triple:
    """The colour's channels as linear-light values in 0..1."""
    body = hex_colour.lstrip("#")
    if len(body) != 6:
        raise ValueError(f"{hex_colour!r} is not a #RRGGBB colour")

    channels = []
    for offset in (0, 2, 4):
        value = int(body[offset : offset + 2], 16) / 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return channels[0], channels[1], channels[2]


def _oklab(rgb: _Triple) -> _Triple:
    red, green, blue = rgb
    long = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    return (
        0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short,
        1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short,
        0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short,
    )


def _simulate(rgb: _Triple, matrix: tuple[_Triple, _Triple, _Triple]) -> _Triple:
    def clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    red, green, blue = rgb
    return (
        clamp(matrix[0][0] * red + matrix[0][1] * green + matrix[0][2] * blue),
        clamp(matrix[1][0] * red + matrix[1][1] * green + matrix[1][2] * blue),
        clamp(matrix[2][0] * red + matrix[2][1] * green + matrix[2][2] * blue),
    )


def _distance(first: _Triple, second: _Triple) -> float:
    lab_a, lab_b = _oklab(first), _oklab(second)
    squared = sum((x - y) ** 2 for x, y in zip(lab_a, lab_b, strict=True))
    return float(100.0 * squared**0.5)


def delta_e(first: str, second: str) -> float:
    """Normal-vision OKLab distance (x100) between two ``#RRGGBB`` colours."""
    return _distance(linear_rgb(first), linear_rgb(second))


def separation(first: str, second: str) -> float:
    """How clearly two colours differ, as a multiple of the "clear" thresholds.

    The smallest of normal-vision ΔE over :data:`CLEAR_NORMAL` and protan and
    deutan ΔE over :data:`CLEAR_CVD`. ``1.0`` is "just clear"; larger is better.
    Taking the minimum is deliberate: two colours that a deutan reader confuses
    are not distinct, however far apart they look to everyone else.
    """
    rgb_a, rgb_b = linear_rgb(first), linear_rgb(second)
    return min(
        _distance(rgb_a, rgb_b) / CLEAR_NORMAL,
        _distance(_simulate(rgb_a, _PROTAN), _simulate(rgb_b, _PROTAN)) / CLEAR_CVD,
        _distance(_simulate(rgb_a, _DEUTAN), _simulate(rgb_b, _DEUTAN)) / CLEAR_CVD,
    )
