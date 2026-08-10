"""Importer for Timecard CSV exports.

Timecard (``/Users/xvade/dev/Misc/Timecard``) is a single-tasking iOS time
tracker: one timer runs at a time, and switching timers closes the previous run
and opens the next. Its export is one row per *completed* run -- a timer still
running has no stop time and is simply absent.

Format (documented in Timecard's own README under "Export format"):

    timer_id,timer_name,started_at,stopped_at,duration_seconds

- ``timer_id``   -- stable UUID, unchanged across renames and folder moves.
- ``timer_name`` -- full folder path (``media/gaming/chess``) *as of the moment
  the run was recorded*, RFC 4180-quoted when it contains a comma or quote.
- timestamps     -- ISO 8601 local time with a UTC offset.

Two quirks of the real data drive the code below; both were measured against the
first export (338 rows, 2026-06-05 to 2026-08-05) rather than assumed:

1. ``duration_seconds`` disagrees with ``stopped_at - started_at`` on 27% of
   rows, always by exactly one second, because the export truncates sub-second
   components of the two timestamps independently. Duration is therefore derived
   from the timestamps and the exported figure is kept only for auditing. A
   drift larger than a second or two is treated as corruption and rejects the
   row.
2. One ``timer_id`` appears under two names (``social``, later
   ``social/friends``). Identity is the id, so that timer's history stays whole
   -- see :class:`~minmax.core.models.Category`.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from minmax.core.models import EventRecord, IntervalRecord, StreamKind
from minmax.ingest.base import ParseResult, Rejection
from minmax.ingest.rows import RowError, parse_instant, parse_optional_int, render_row

__all__ = ["TimecardCsvImporter"]


class TimecardCsvImporter:
    """Parses a Timecard CSV export into interval records."""

    name = "timecard-csv"
    stream_key = "timecard"
    stream_name = "Timecard"
    stream_kind = StreamKind.INTERVAL
    # One timer runs at a time, so overlapping runs are a data error rather
    # than something to be stored and reasoned about later.
    stream_exclusive = True

    REQUIRED_COLUMNS = ("timer_id", "timer_name", "started_at", "stopped_at")
    # Tolerance for the known sub-second truncation described in the module
    # docstring. Anything beyond this is not rounding, it is a broken row.
    MAX_DURATION_DRIFT_SECONDS = 2

    def parse(self, path: Path) -> ParseResult:
        records: list[IntervalRecord | EventRecord] = []
        rejections: list[Rejection] = []
        notes: list[str] = []
        drift_counts: Counter[int] = Counter()

        # newline="" per the csv module's contract: it must see raw line
        # endings to correctly handle quoted fields containing newlines.
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)

            missing = [c for c in self.REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(
                    f"{path.name} is not a Timecard export: missing column(s) {', '.join(missing)}"
                )

            for row in reader:
                line = reader.line_num
                try:
                    record = self._record_from_row(row)
                except RowError as error:
                    rejections.append(Rejection(line=line, reason=str(error), raw=render_row(row)))
                    continue

                drift = record.source_duration_drift
                if drift is not None:
                    drift_counts[drift] += 1
                records.append(record)

        if drift_counts:
            notes.append(_drift_note(drift_counts))
        if rejections:
            notes.append(f"{len(rejections)} row(s) rejected")

        return ParseResult(records=records, rejections=rejections, notes=notes)

    # ------------------------------------------------------------------

    def _record_from_row(self, row: dict[str, str | None]) -> IntervalRecord:
        timer_name = (row.get("timer_name") or "").strip()
        if not timer_name:
            raise RowError("timer_name is empty")

        # A blank id is tolerated (the category then keys on its path) because
        # losing a run is worse than losing the ability to track it across a
        # rename. Timecard itself always supplies one.
        timer_id = (row.get("timer_id") or "").strip() or None

        start = parse_instant(row.get("started_at"), "started_at")
        end = parse_instant(row.get("stopped_at"), "stopped_at")
        if end.epoch_seconds < start.epoch_seconds:
            raise RowError("stopped_at is before started_at")

        source_duration = parse_optional_int(row.get("duration_seconds"), "duration_seconds")
        if source_duration is not None:
            derived = end.epoch_seconds - start.epoch_seconds
            drift = source_duration - derived
            if abs(drift) > self.MAX_DURATION_DRIFT_SECONDS:
                raise RowError(
                    f"duration_seconds {source_duration} disagrees with the timestamps "
                    f"({derived}s) by {drift}s, beyond the "
                    f"{self.MAX_DURATION_DRIFT_SECONDS}s truncation tolerance"
                )

        return IntervalRecord(
            category_source_id=timer_id,
            category_path=timer_name,
            start=start,
            end=end,
            source_duration_seconds=source_duration,
        )


def _drift_note(drift_counts: Counter[int]) -> str:
    total = sum(drift_counts.values())
    exact = drift_counts.get(0, 0)
    if exact == total:
        return f"{total} rows: exported duration matched the timestamps exactly"
    spread = ", ".join(f"{d:+d}s x{n}" for d, n in sorted(drift_counts.items()) if d != 0)
    return (
        f"{total - exact} of {total} rows had an exported duration differing from the "
        f"timestamps ({spread}); durations were derived from the timestamps"
    )
