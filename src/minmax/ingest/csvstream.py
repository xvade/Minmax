"""A config-driven CSV importer for user-declared point-in-time streams.

Where :mod:`minmax.ingest.timecard` is a hand-written parser for one known
export shape, this one is driven entirely by a stream's stored
:class:`~minmax.core.models.StreamField` mapping -- built once, in the
new-stream dialog or ``minmax stream add``, from the CSV's own header -- so a
user-declared stream needs no code of its own. See ``docs/architecture.md``
§6.1.

Interval-shaped user-created streams (a second Timecard-like source) are not
handled here; see the note in ``TODO.md``. Everything below assumes a
point-in-time stream: exactly one instant per row, plus whatever fields it
declared.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from minmax.core.models import (
    EventRecord,
    FieldRole,
    FieldSpec,
    IntervalRecord,
    StreamField,
    StreamKind,
    implicit_category_path,
)
from minmax.ingest.base import ParseResult, Rejection
from minmax.ingest.rows import RowError, parse_instant, parse_optional_float, render_row

__all__ = ["CsvStreamImporter", "guess_fields", "read_sample", "sniff_fields"]

#: Rows read when guessing a column mapping. Enough to be confident about a
#: mixed file (a handful of blank cells) without reading a potentially huge
#: export just to declare a stream.
_SNIFF_SAMPLE_ROWS = 20


class CsvStreamImporter:
    """Parses a CSV into event records using one stream's declared field mapping.

    Built per stream (see :func:`~minmax.ingest.importer_for`) rather than
    existing as a single hardcoded class, since the column mapping is data,
    not code.
    """

    stream_kind = StreamKind.EVENT
    # A point-in-time stream has no span to overlap, so exclusivity -- a
    # concept about spans contradicting each other -- has nothing to check.
    stream_exclusive = False

    def __init__(self, stream_key: str, stream_name: str, fields: Sequence[StreamField]) -> None:
        self.name = f"csv-stream:{stream_key}"
        self.stream_key = stream_key
        self.stream_name = stream_name
        self._fields = fields

        time_fields = [f for f in fields if f.role is FieldRole.AT]
        if len(time_fields) != 1:
            raise ValueError(
                f"stream {stream_key!r} must declare exactly one 'at' field, found"
                f" {len(time_fields)}"
            )
        self._time_field = time_fields[0]
        self._category_field = next((f for f in fields if f.role is FieldRole.CATEGORY), None)
        self._source_id_field = next((f for f in fields if f.role is FieldRole.SOURCE_ID), None)
        self._value_fields = [f for f in fields if f.role in (FieldRole.NUMBER, FieldRole.TEXT)]

    def parse(self, path: Path) -> ParseResult:
        records: list[IntervalRecord | EventRecord] = []
        rejections: list[Rejection] = []

        # newline="" per the csv module's contract, as in TimecardCsvImporter.
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            missing = [f.source_column for f in self._fields if f.source_column not in header]
            if missing:
                raise ValueError(
                    f"{path.name} does not match {self.stream_name!r}'s declared columns:"
                    f" missing {', '.join(missing)}"
                )

            for row in reader:
                line = reader.line_num
                try:
                    records.append(self._record_from_row(row))
                except RowError as error:
                    rejections.append(Rejection(line=line, reason=str(error), raw=render_row(row)))

        notes = [f"{len(rejections)} row(s) rejected"] if rejections else []
        return ParseResult(records=records, rejections=rejections, notes=notes)

    # ------------------------------------------------------------------

    def _record_from_row(self, row: dict[str, str | None]) -> EventRecord:
        at = parse_instant(row.get(self._time_field.source_column), self._time_field.label)

        if self._category_field is not None:
            category_path = (row.get(self._category_field.source_column) or "").strip()
            if not category_path:
                raise RowError(f"{self._category_field.label} is empty")
        else:
            category_path = implicit_category_path(self.stream_key)

        source_id = None
        if self._source_id_field is not None:
            source_id = (row.get(self._source_id_field.source_column) or "").strip() or None

        values: dict[str, float | str] = {}
        for declared in self._value_fields:
            raw = row.get(declared.source_column)
            if declared.role is FieldRole.NUMBER:
                number = parse_optional_float(raw, declared.label)
                if number is not None:
                    values[declared.key] = number
            else:
                text = (raw or "").strip()
                if text:
                    values[declared.key] = text

        return EventRecord(
            category_source_id=source_id,
            category_path=category_path,
            at=at,
            values=values,
        )


def read_sample(
    path: Path, *, sample_rows: int = _SNIFF_SAMPLE_ROWS
) -> tuple[list[str], list[dict[str, str | None]]]:
    """A CSV's header and its first ``sample_rows`` data rows.

    The one place both :func:`sniff_fields` (guessing roles) and a new-stream
    dialog's preview table (showing the user what it read) read a candidate
    file, so the two can never disagree about what the header or a sample
    row looked like.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        sample: list[dict[str, str | None]] = []
        for row in reader:
            sample.append(row)
            if len(sample) >= sample_rows:
                break
    return header, sample


def guess_fields(header: Sequence[str], sample: list[dict[str, str | None]]) -> list[FieldSpec]:
    """Guess a column mapping from a header and a sample of rows.

    The only role guessed with any confidence is the timestamp: a column
    where every sampled value parses as an ISO 8601 instant with a UTC offset
    is assumed to be it, and the first such column wins if more than one
    qualifies. Every other column defaults to ``NUMBER`` if every sampled
    value parses as a float, else ``TEXT`` -- ``CATEGORY`` and ``SOURCE_ID``
    are never guessed, since nothing about a column's *values* distinguishes
    "this identifies the row" from "this is just text"; a caller overrides
    those explicitly (the new-stream dialog's role dropdown, or
    ``minmax stream add --role``).

    Raises nothing on an empty or all-blank column; it simply falls back to
    ``TEXT``. The caller is responsible for checking the result declares
    exactly one ``AT`` field before persisting it -- the same check
    :class:`CsvStreamImporter` makes, so a bad guess fails the same way a bad
    override would.
    """
    time_column = _first_time_like_column(header, sample)
    fields: list[FieldSpec] = []
    for column in header:
        key = _slugify(column)
        if column == time_column:
            fields.append(FieldSpec(key=key, label=column, role=FieldRole.AT, source_column=column))
            continue
        role = FieldRole.NUMBER if _all_numeric(column, sample) else FieldRole.TEXT
        fields.append(FieldSpec(key=key, label=column, role=role, source_column=column))
    return fields


def sniff_fields(path: Path, *, sample_rows: int = _SNIFF_SAMPLE_ROWS) -> list[FieldSpec]:
    """:func:`guess_fields` over :func:`read_sample` of ``path`` -- the
    common case of a caller that only wants the guess, not the raw sample."""
    header, sample = read_sample(path, sample_rows=sample_rows)
    return guess_fields(header, sample)


def _sampled_values(column: str, sample: list[dict[str, str | None]]) -> list[str]:
    return [text for row in sample if (text := (row.get(column) or "").strip())]


def _first_time_like_column(
    header: Sequence[str], sample: list[dict[str, str | None]]
) -> str | None:
    for column in header:
        values = _sampled_values(column, sample)
        if values and all(_looks_like_instant(v) for v in values):
            return column
    return None


def _looks_like_instant(value: str) -> bool:
    try:
        parse_instant(value, "value")
    except RowError:
        return False
    return True


def _all_numeric(column: str, sample: list[dict[str, str | None]]) -> bool:
    values = _sampled_values(column, sample)
    return bool(values) and all(_looks_like_float(v) for v in values)


def _looks_like_float(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _slugify(column: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in column.strip().lower())
    return slug.strip("_") or "field"
