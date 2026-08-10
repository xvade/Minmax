"""Config-driven CSV importers for user-declared streams, either kind.

Where :mod:`minmax.ingest.timecard` is a hand-written parser for one known
export shape, these are driven entirely by a stream's stored
:class:`~minmax.core.models.StreamField` mapping -- built once, in the
new-stream dialog or ``minmax stream add``, from the CSV's own header -- so a
user-declared stream needs no code of its own. See ``docs/architecture.md``
§6.1.

:class:`CsvStreamImporter` handles a point-in-time (event) stream: exactly
one instant per row, plus whatever ``NUMBER``/``TEXT`` fields it declared.
:class:`IntervalCsvImporter` handles a span (interval) stream: a start and an
end column, and no value fields at all -- ``interval_record`` has no
per-row payload table, unlike ``event_record`` (see the module docstring's
reasoning reflected in ``docs/architecture.md`` §6.1), so any other column of
an interval CSV must be declared ``IGNORED`` rather than ``NUMBER``/``TEXT``.
Both share the guessing helpers below so the two guesses cannot drift apart.
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

__all__ = [
    "CsvStreamImporter",
    "IntervalCsvImporter",
    "guess_fields",
    "read_sample",
    "sniff_fields",
    "validate_declaration",
]

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


class IntervalCsvImporter:
    """Parses a CSV into interval records using one stream's declared field mapping.

    The interval sibling of :class:`CsvStreamImporter`: built per stream from
    its stored column mapping rather than existing as a single hardcoded
    class. Unlike an event stream, an interval stream declares no
    ``NUMBER``/``TEXT`` fields -- ``__init__`` rejects them, since there is
    nowhere in ``interval_record`` to store them.
    """

    stream_kind = StreamKind.INTERVAL

    def __init__(
        self,
        stream_key: str,
        stream_name: str,
        fields: Sequence[StreamField],
        *,
        exclusive: bool = False,
    ) -> None:
        self.name = f"csv-stream:{stream_key}"
        self.stream_key = stream_key
        self.stream_name = stream_name
        self.stream_exclusive = exclusive
        self._fields = fields

        error = validate_declaration(fields, StreamKind.INTERVAL)
        if error is not None:
            raise ValueError(f"stream {stream_key!r}: {error}")

        self._start_field = next(f for f in fields if f.role is FieldRole.START)
        self._end_field = next(f for f in fields if f.role is FieldRole.END)
        self._category_field = next((f for f in fields if f.role is FieldRole.CATEGORY), None)
        self._source_id_field = next((f for f in fields if f.role is FieldRole.SOURCE_ID), None)

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

    def _record_from_row(self, row: dict[str, str | None]) -> IntervalRecord:
        start = parse_instant(row.get(self._start_field.source_column), self._start_field.label)
        end = parse_instant(row.get(self._end_field.source_column), self._end_field.label)
        # IntervalRecord.__post_init__ raises a bare ValueError for an
        # inverted span, which parse() above does not catch -- checked here
        # first so a single bad row becomes a Rejection instead of aborting
        # the whole import, exactly as TimecardCsvImporter._record_from_row
        # already does.
        if end.epoch_seconds < start.epoch_seconds:
            raise RowError(f"{self._end_field.label} is before {self._start_field.label}")

        if self._category_field is not None:
            category_path = (row.get(self._category_field.source_column) or "").strip()
            if not category_path:
                raise RowError(f"{self._category_field.label} is empty")
        else:
            category_path = implicit_category_path(self.stream_key)

        source_id = None
        if self._source_id_field is not None:
            source_id = (row.get(self._source_id_field.source_column) or "").strip() or None

        return IntervalRecord(
            category_source_id=source_id,
            category_path=category_path,
            start=start,
            end=end,
        )


def validate_declaration(
    fields: Sequence[FieldSpec] | Sequence[StreamField], kind: StreamKind
) -> str | None:
    """Whether ``fields`` is a legal column mapping for a stream of ``kind``.

    Shared by ``minmax stream add`` and the new-stream dialog so the CLI and
    the GUI can never disagree about what a legal declaration looks like.
    Returns ``None`` if legal, else a message in the voice already used by
    both callers' error output.
    """
    if kind is StreamKind.EVENT:
        at_fields = [f for f in fields if f.role is FieldRole.AT]
        if len(at_fields) != 1:
            return (
                f"exactly one column must have role 'at' (found {len(at_fields)});"
                " override the guess with --role COLUMN=at"
            )
        return None

    if kind is StreamKind.INTERVAL:
        start_fields = [f for f in fields if f.role is FieldRole.START]
        if len(start_fields) != 1:
            return (
                f"exactly one column must have role 'start' (found {len(start_fields)});"
                " override the guess with --role COLUMN=start"
            )
        end_fields = [f for f in fields if f.role is FieldRole.END]
        if len(end_fields) != 1:
            return (
                f"exactly one column must have role 'end' (found {len(end_fields)});"
                " override the guess with --role COLUMN=end"
            )
        bad_roles = (FieldRole.AT, FieldRole.NUMBER, FieldRole.TEXT)
        unstorable = [f for f in fields if f.role in bad_roles]
        if unstorable:
            names = ", ".join(f.source_column for f in unstorable)
            return (
                f"an interval stream cannot store value columns: {names}; set their role to"
                " 'ignored', 'category', or 'source_id'"
            )
        return None

    return f"streams of kind {kind!r} are not declared through this path"


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


def guess_fields(
    header: Sequence[str],
    sample: list[dict[str, str | None]],
    *,
    kind: StreamKind = StreamKind.EVENT,
) -> list[FieldSpec]:
    """Guess a column mapping from a header and a sample of rows.

    For an event stream (the default), the only role guessed with any
    confidence is the timestamp: a column where every sampled value parses
    as an ISO 8601 instant with a UTC offset is assumed to be it, and the
    first such column wins if more than one qualifies. Every other column
    defaults to ``NUMBER`` if every sampled value parses as a float, else
    ``TEXT``.

    For an interval stream, the first two time-like columns (in header
    order) are guessed as ``START`` and ``END`` respectively -- Timecard's
    own export orders ``started_at`` before ``stopped_at``, and there is no
    other signal to prefer one order over the other. Every other column
    defaults to ``IGNORED``, since an interval stream has nowhere to store a
    ``NUMBER``/``TEXT`` value (see the module docstring).

    In both kinds, ``CATEGORY`` and ``SOURCE_ID`` are never guessed, since
    nothing about a column's *values* distinguishes "this identifies the
    row" from "this is just data"; a caller overrides those explicitly (the
    new-stream dialog's role dropdown, or ``minmax stream add --role``).

    Raises nothing on an empty, all-blank, or ambiguous column; it simply
    falls back to a default role. The caller is responsible for checking the
    result is a legal declaration before persisting it -- see
    :func:`validate_declaration` -- so a bad guess fails the same way a bad
    override would.
    """
    if kind is StreamKind.INTERVAL:
        time_columns = _time_like_columns(header, sample)
        start_column = time_columns[0] if len(time_columns) >= 1 else None
        end_column = time_columns[1] if len(time_columns) >= 2 else None
        fields = []
        for column in header:
            key = _slugify(column)
            if column == start_column:
                role = FieldRole.START
            elif column == end_column:
                role = FieldRole.END
            else:
                role = FieldRole.IGNORED
            fields.append(FieldSpec(key=key, label=column, role=role, source_column=column))
        return fields

    time_column = next(iter(_time_like_columns(header, sample)), None)
    fields = []
    for column in header:
        key = _slugify(column)
        if column == time_column:
            fields.append(FieldSpec(key=key, label=column, role=FieldRole.AT, source_column=column))
            continue
        role = FieldRole.NUMBER if _all_numeric(column, sample) else FieldRole.TEXT
        fields.append(FieldSpec(key=key, label=column, role=role, source_column=column))
    return fields


def sniff_fields(
    path: Path, *, kind: StreamKind = StreamKind.EVENT, sample_rows: int = _SNIFF_SAMPLE_ROWS
) -> list[FieldSpec]:
    """:func:`guess_fields` over :func:`read_sample` of ``path`` -- the
    common case of a caller that only wants the guess, not the raw sample."""
    header, sample = read_sample(path, sample_rows=sample_rows)
    return guess_fields(header, sample, kind=kind)


def _sampled_values(column: str, sample: list[dict[str, str | None]]) -> list[str]:
    return [text for row in sample if (text := (row.get(column) or "").strip())]


def _time_like_columns(header: Sequence[str], sample: list[dict[str, str | None]]) -> list[str]:
    """Every column, in header order, whose sampled values all parse as an instant."""
    result = []
    for column in header:
        values = _sampled_values(column, sample)
        if values and all(_looks_like_instant(v) for v in values):
            result.append(column)
    return result


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
