"""Row-level parsing helpers shared by every CSV importer.

Extracted from the Timecard parser once a second importer needed the same
rules unchanged: validating one timestamp field is the same problem whatever
the source, so that logic lives here and only source-specific knowledge (like
Timecard's duration-drift tolerance) stays in the importer.
"""

from __future__ import annotations

from datetime import datetime

from minmax.core.timeref import Instant

__all__ = ["RowError", "parse_instant", "parse_optional_float", "parse_optional_int", "render_row"]


class RowError(ValueError):
    """A single row is unusable; the rest of the file is still fine."""


def parse_instant(raw: str | None, column: str) -> Instant:
    text = (raw or "").strip()
    if not text:
        raise RowError(f"{column} is empty")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise RowError(f"{column} {text!r} is not an ISO 8601 timestamp") from error
    if parsed.utcoffset() is None:
        raise RowError(f"{column} {text!r} has no UTC offset; the local reading would be ambiguous")
    return Instant.from_datetime(parsed)


def parse_optional_int(raw: str | None, column: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise RowError(f"{column} {text!r} is not an integer") from error


def parse_optional_float(raw: str | None, column: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as error:
        raise RowError(f"{column} {text!r} is not a number") from error


def render_row(row: dict[str, str | None]) -> str:
    return ",".join(f"{key}={value!r}" for key, value in row.items())
