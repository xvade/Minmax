"""Formatting every client must agree on.

Lives beside the client facade rather than in a UI module because the CLI, the
desktop app, and a future web client should all render a duration identically --
if they diverge, the same data appears to disagree with itself.
"""

from __future__ import annotations

from minmax.core.models import StreamKind

__all__ = ["format_clock_span", "format_duration", "format_measure"]


def format_duration(seconds: int) -> str:
    """Render a span the way a person reads it: ``2h 30m``, ``45m``, ``12s``.

    Minutes are zero-padded only when they follow hours, so a column of values
    stays aligned without ``1h 05m`` becoming ``1h 5m``.
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"


def format_measure(value: int, stream_kind: StreamKind) -> str:
    """A total the way its stream's shape reads.

    An interval stream's total is a duration; a point-in-time stream has no
    duration to report, only how many -- see ``docs/architecture.md``'s note
    on reusing the seconds-shaped trends machinery for counts. The one place
    this branches, so the day summary, the trends axis, and its legend all
    agree on how a count reads rather than each reimplementing the plural
    check.
    """
    if stream_kind is StreamKind.EVENT:
        return f"{value} event" if value == 1 else f"{value} events"
    return format_duration(value)


def format_clock_span(start_label: str, end_label: str) -> str:
    """``09:00-10:30`` from two ``HH:MM`` readings."""
    return f"{start_label}-{end_label}"
