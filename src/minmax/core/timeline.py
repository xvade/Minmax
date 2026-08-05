"""Arranging a day's observations on a vertical timeline.

This is the arithmetic behind the history view: where each block sits, how tall
it is, whether it is tall enough to carry a label, and how the scroll position
must move so that zooming stays anchored under the pointer.

It lives in ``core`` rather than ``ui`` on purpose. All of it is exact,
deterministic, and easy to get subtly wrong -- an off-by-one in the label
threshold is cosmetic, but an error in :meth:`DayGeometry.y_for_epoch` silently
misplaces every block. Keeping it free of Qt means it is unit-tested directly
and a future web client can reuse it rather than reimplementing it.

The model follows Timecard's own history screen: blocks are sized to their real
duration so they tile without overlapping, and a run crossing midnight stays a
single run -- clipped for drawing, but flagged so the view can mark the overhang
instead of pretending the run ended at the boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_PIXELS_PER_HOUR",
    "MAX_PIXELS_PER_HOUR",
    "MIN_PIXELS_PER_HOUR",
    "DayGeometry",
    "LaidOutBlock",
    "anchored_scroll_offset",
    "clamp_zoom",
    "hour_boundaries",
    "lay_out_day",
]

SECONDS_PER_HOUR = 3600

#: Zoom limits, in pixels per hour of wall time.
#:
#: The minimum fits a whole 24-hour day in roughly 700px, which is a comfortable
#: window height. The maximum is what makes a one-minute run about 8px tall --
#: Timecard's stated bar for being able to comfortably select and edit even very
#: short runs, and the same bar is worth clearing here.
MIN_PIXELS_PER_HOUR = 28.0
MAX_PIXELS_PER_HOUR = 480.0
DEFAULT_PIXELS_PER_HOUR = 64.0

#: A block must be at least this tall before its name is drawn, and this tall
#: again before the duration joins it in the corner. Below the threshold the
#: text would be clipped or overlap its neighbours.
MIN_LABEL_HEIGHT = 15.0
MIN_DURATION_HEIGHT = 26.0


@dataclass(frozen=True, slots=True)
class DayGeometry:
    """Maps a local day's epoch range onto vertical pixels.

    The day's bounds are passed in rather than assumed to be 24 hours, because
    across a DST transition a local day really is 23 or 25 hours long. Sizing
    the timeline from the actual elapsed seconds renders that correctly with no
    special case: the short day is simply shorter.
    """

    day_start_epoch: int
    day_end_epoch: int
    pixels_per_hour: float

    def __post_init__(self) -> None:
        if self.day_end_epoch <= self.day_start_epoch:
            raise ValueError("a day must end after it starts")
        if self.pixels_per_hour <= 0:
            raise ValueError("pixels_per_hour must be positive")

    @property
    def seconds(self) -> int:
        return self.day_end_epoch - self.day_start_epoch

    @property
    def height(self) -> float:
        """Total pixel height of the day."""
        return self.seconds / SECONDS_PER_HOUR * self.pixels_per_hour

    @property
    def pixels_per_second(self) -> float:
        return self.pixels_per_hour / SECONDS_PER_HOUR

    def y_for_epoch(self, epoch_seconds: float) -> float:
        """Vertical position of an instant, in pixels from the day's top.

        Not clamped: callers that need a value inside the day should clip, and
        callers drawing an overhanging run want the true (negative or past-end)
        position.
        """
        return (epoch_seconds - self.day_start_epoch) * self.pixels_per_second

    def epoch_for_y(self, y: float) -> int:
        """The instant at a vertical position -- the inverse of :meth:`y_for_epoch`."""
        return round(self.day_start_epoch + y / self.pixels_per_second)

    def with_zoom(self, pixels_per_hour: float) -> DayGeometry:
        return DayGeometry(
            day_start_epoch=self.day_start_epoch,
            day_end_epoch=self.day_end_epoch,
            pixels_per_hour=clamp_zoom(pixels_per_hour),
        )


@dataclass(frozen=True, slots=True)
class LaidOutBlock:
    """One observation's rectangle on the day, already clipped to it."""

    index: int
    """Position in the sequence handed to :func:`lay_out_day`."""

    top: float
    height: float

    continues_before: bool
    """The run began before this day; the view should mark the top edge."""

    continues_after: bool
    """The run ends after this day; the view should mark the bottom edge."""

    show_label: bool
    show_duration: bool

    @property
    def bottom(self) -> float:
        return self.top + self.height


def clamp_zoom(pixels_per_hour: float) -> float:
    return max(MIN_PIXELS_PER_HOUR, min(MAX_PIXELS_PER_HOUR, pixels_per_hour))


def lay_out_day(
    spans: Sequence[tuple[int, int]],
    geometry: DayGeometry,
    *,
    min_label_height: float = MIN_LABEL_HEIGHT,
    min_duration_height: float = MIN_DURATION_HEIGHT,
) -> list[LaidOutBlock]:
    """Place ``(start_epoch, end_epoch)`` spans on the day.

    Spans that do not touch the day are dropped; spans that overhang it are
    clipped and flagged. The result preserves the input order, and each entry
    carries the index of the span it came from so the caller can pair it back up
    with the record's colour and label without a lookup table.

    Blocks are *not* de-overlapped: for an exclusive stream they cannot overlap,
    and for a stream that permits overlaps the honest rendering is to draw them
    overlapping rather than to invent lanes. Lane assignment belongs to the
    multi-stream timeline, when that goal is reached.
    """
    blocks: list[LaidOutBlock] = []

    for index, (start_epoch, end_epoch) in enumerate(spans):
        if end_epoch <= geometry.day_start_epoch or start_epoch >= geometry.day_end_epoch:
            continue

        clipped_start = max(start_epoch, geometry.day_start_epoch)
        clipped_end = min(end_epoch, geometry.day_end_epoch)

        top = geometry.y_for_epoch(clipped_start)
        height = geometry.y_for_epoch(clipped_end) - top

        blocks.append(
            LaidOutBlock(
                index=index,
                top=top,
                height=height,
                continues_before=start_epoch < geometry.day_start_epoch,
                continues_after=end_epoch > geometry.day_end_epoch,
                show_label=height >= min_label_height,
                show_duration=height >= min_duration_height,
            )
        )

    return blocks


def hour_boundaries(day_start_epoch: int, day_end_epoch: int, *, every: int = 1) -> list[int]:
    """Instants of each hour boundary within a day, starting at its first moment.

    Walks the day's own bounds rather than assuming 24 evenly spaced hours, so a
    23- or 25-hour DST day yields the right number of boundaries.

    Only the instants are returned. Turning them into clock readings needs a
    timezone, which ``core`` deliberately does not reach for -- the client
    facade attaches labels, so every UI shows the same ones.
    """
    if every < 1:
        raise ValueError(f"every must be at least 1, got {every}")

    step = SECONDS_PER_HOUR * every
    return list(range(day_start_epoch, day_end_epoch, step))


def anchored_scroll_offset(
    scroll_offset: float,
    anchor_y: float,
    old_pixels_per_hour: float,
    new_pixels_per_hour: float,
) -> float:
    """Scroll offset that keeps the instant under ``anchor_y`` in place while zooming.

    ``anchor_y`` is measured from the top of the *viewport* (the pointer
    position); ``scroll_offset`` is the viewport's top in content coordinates.
    Without this, zooming appears to fling the timeline around, because the
    content grows from its top edge rather than around the point of interest --
    the same behaviour Timecard's pinch-to-zoom takes care to avoid.

    The result may be negative; the caller clamps it to its own scroll range.
    """
    if old_pixels_per_hour <= 0:
        raise ValueError("old_pixels_per_hour must be positive")
    scale = new_pixels_per_hour / old_pixels_per_hour
    return (scroll_offset + anchor_y) * scale - anchor_y
