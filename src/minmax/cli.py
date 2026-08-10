"""Command-line entry point.

This exists so the import/storage/retrieval feature is fully usable before the
UI is built, and so the pipeline can be driven in a way that is scriptable and
easy to test. It is a thin shell: every command is a few calls into
:class:`~minmax.storage.repository.Repository` or
:func:`~minmax.ingest.base.run_import` with formatting on top, and no logic
lives here that a UI would also need.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from pathlib import Path
from typing import TYPE_CHECKING

from minmax.api.format import format_duration
from minmax.core.columns import Granularity, Interval
from minmax.core.models import FieldRole, StreamKind
from minmax.core.timeref import Period
from minmax.ingest import importer_for, run_import, sniff_fields
from minmax.storage.database import default_home, open_database
from minmax.storage.repository import OverlapError, Repository
from minmax.storage.rollups import rebuild

if TYPE_CHECKING:
    # Only for type hints -- kept out of the runtime import graph so that
    # `minmax.api` (which pulls in `analysis`) is not loaded by commands that
    # never touch it. `cmd_correlate` imports the real thing lazily below.
    from minmax.api import CorrelationRow, CorrelationsView

__all__ = ["format_duration", "main"]


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from error


def _parse_interval(text: str) -> Interval:
    """``'week'`` or ``'week:2'`` -> an :class:`Interval`, for ``--interval``."""
    unit_text, sep, count_text = text.strip().partition(":")
    count = 1
    if sep:
        try:
            count = int(count_text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not UNIT or UNIT:COUNT") from None
    try:
        unit = Granularity(unit_text)
    except ValueError:
        known = ", ".join(g.value for g in Granularity if g is not Granularity.AUTO)
        raise argparse.ArgumentTypeError(
            f"unknown interval unit {unit_text!r} in {text!r}; known units: {known}"
        ) from None
    try:
        return Interval(unit, count)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _parse_sweep(text: str) -> list[Interval]:
    """``'day:3,week:1'`` -> the intervals to test, for ``--interval``."""
    return [_parse_interval(part) for part in text.split(",") if part.strip()]


def _local_bounds(first_day: date, last_day: date) -> tuple[int, int]:
    """Epoch bounds of ``[first_day 00:00, last_day+1 00:00)`` in machine-local time.

    The CLI runs where the user is, so the machine's timezone is the right
    reading of "Tuesday". Stored data keeps its own recorded offsets regardless.
    """
    start = datetime.combine(first_day, time_of_day.min).astimezone()
    end = datetime.combine(last_day + timedelta(days=1), time_of_day.min).astimezone()
    return int(start.timestamp()), int(end.timestamp())


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def cmd_import(args: argparse.Namespace, repository: Repository) -> int:
    try:
        importer = importer_for(repository, args.stream)
    except (LookupError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    source = Path(args.path).expanduser()
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2

    try:
        report = run_import(repository, importer, source, home=args.home, force=args.force)
    except OverlapError as error:
        print(f"import aborted, nothing was stored:\n  {error}", file=sys.stderr)
        return 1

    if report.skipped:
        print(f"skipped {report.source_name}: {report.notes[0]}")
        return 0

    print(
        f"imported {report.source_name} into {report.stream_key}: "
        f"{report.inserted} new, {report.duplicates} already present, "
        f"{report.rejected} rejected ({report.rows_read} rows read)"
    )
    print(f"  archived: {report.archived_path}")
    for note in report.notes:
        print(f"  note: {note}")
    for rejection in report.rejections[:10]:
        print(f"  rejected line {rejection.line}: {rejection.reason}", file=sys.stderr)
    if len(report.rejections) > 10:
        print(f"  ...and {len(report.rejections) - 10} more", file=sys.stderr)
    return 0


def _parse_role_overrides(role_args: Sequence[str]) -> dict[str, FieldRole]:
    overrides: dict[str, FieldRole] = {}
    for item in role_args:
        column, sep, role_text = item.partition("=")
        if not sep:
            raise ValueError(f"{item!r} is not COLUMN=ROLE")
        try:
            overrides[column] = FieldRole(role_text)
        except ValueError:
            known = ", ".join(role.value for role in FieldRole)
            raise ValueError(
                f"unknown role {role_text!r} for column {column!r}; known roles: {known}"
            ) from None
    return overrides


def cmd_stream_add(args: argparse.Namespace, repository: Repository) -> int:
    """Declare a new point-in-time stream and its column mapping.

    The mapping is guessed from ``--from``'s header and sample rows --
    confidently for the timestamp column, conservatively (number vs. text)
    for everything else -- then any ``--role`` overrides are applied on top.
    Nothing is guessed for category or source-id columns, since nothing about
    a column's values distinguishes "this identifies the row" from "this is
    just text"; those need an explicit ``--role``.
    """
    if args.kind != "event":
        print(
            "only --kind event is supported today; interval-shaped custom streams"
            " are not yet implemented (see TODO.md)",
            file=sys.stderr,
        )
        return 2
    if repository.get_stream(args.key) is not None:
        print(f"a stream named {args.key!r} already exists", file=sys.stderr)
        return 2

    try:
        overrides = _parse_role_overrides(args.role)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    fields = sniff_fields(args.from_csv)
    unknown_columns = sorted(set(overrides) - {f.source_column for f in fields})
    if unknown_columns:
        print(
            f"--role names column(s) not in the CSV: {', '.join(unknown_columns)}", file=sys.stderr
        )
        return 2
    fields = [
        replace(f, role=overrides[f.source_column]) if f.source_column in overrides else f
        for f in fields
    ]

    at_fields = [f for f in fields if f.role is FieldRole.AT]
    if len(at_fields) != 1:
        print(
            f"exactly one column must have role 'at' (found {len(at_fields)});"
            " override the guess with --role COLUMN=at",
            file=sys.stderr,
        )
        return 2

    stream = repository.create_stream(args.key, args.name, StreamKind.EVENT, fields=fields)
    print(f"created stream {stream.key!r} ({stream.kind}) with fields:")
    for declared in fields:
        unit = f" ({declared.unit})" if declared.unit else ""
        print(f"  {declared.source_column:<20} -> {declared.role.value}{unit}  [{declared.key}]")
    print(f"import into it with: minmax import <file> --stream {stream.key}")
    return 0


def cmd_streams(args: argparse.Namespace, repository: Repository) -> int:
    streams = repository.list_streams()
    if not streams:
        print("no streams yet; run `minmax stream add` or `minmax import <file>`")
        return 0

    for stream in streams:
        assert stream.id is not None
        bounds = repository.observed_range(stream.id)
        span = ""
        if bounds is not None:
            low, high = (
                datetime.fromtimestamp(bounds[0]).date(),
                datetime.fromtimestamp(bounds[1]).date(),
            )
            span = f"  {low} -> {high}"
        print(f"{stream.key}  ({stream.kind}{', exclusive' if stream.exclusive else ''}){span}")
        if stream.kind is StreamKind.EVENT:
            for declared in repository.stream_fields(stream.id):
                if declared.role is FieldRole.AT:
                    continue
                unit = f" ({declared.unit})" if declared.unit else ""
                print(f"    {declared.role.value:<10} {declared.label}{unit}")
    return 0


def cmd_status(args: argparse.Namespace, repository: Repository) -> int:
    streams = repository.list_streams()
    if not streams:
        print("no streams yet; run `minmax import <file>` to create one")
        return 0

    for stream in streams:
        assert stream.id is not None
        bounds = repository.observed_range(stream.id)
        categories = repository.list_categories(stream.id)
        imports = repository.list_imports(stream.id)
        print(f"{stream.key}  ({stream.kind}{', exclusive' if stream.exclusive else ''})")
        if bounds is None:
            print("  no observations")
        else:
            low, high = bounds
            covered = repository.totals_by_category(stream.id, low, high)
            total = sum(item.seconds for item in covered)
            records = sum(item.record_count for item in covered)
            print(
                f"  {records} records over "
                f"{datetime.fromtimestamp(low).date()} -> "
                f"{datetime.fromtimestamp(high).date()}"
            )
            print(f"  {len(categories)} categories, {format_duration(total)} tracked")
        for run in imports[-args.imports :]:
            state = "ok" if run.completed else "FAILED"
            print(
                f"  import {run.id} [{state}] {run.source_name}: "
                f"{run.rows_inserted} new, {run.rows_duplicate} dup, "
                f"{run.rows_rejected} rejected"
            )
            if run.notes:
                print(f"    {run.notes}")
    return 0


def cmd_totals(args: argparse.Namespace, repository: Repository) -> int:
    stream = repository.get_stream(args.stream)
    if stream is None or stream.id is None:
        print(f"no stream {args.stream!r}; import something first", file=sys.stderr)
        return 2
    if stream.kind is not StreamKind.INTERVAL:
        print(
            f"{args.stream!r} is a {stream.kind} stream; 'totals' only supports interval"
            " streams today -- try 'periods'",
            file=sys.stderr,
        )
        return 2

    bounds = repository.observed_range(stream.id)
    if bounds is None:
        print("no observations")
        return 0

    first = args.since or datetime.fromtimestamp(bounds[0]).date()
    last = args.until or datetime.fromtimestamp(bounds[1]).date()
    start, end = _local_bounds(first, last)

    totals = repository.totals_by_category(stream.id, start, end, path_prefix=args.prefix)
    grand = sum(item.seconds for item in totals)
    print(f"{stream.key}  {first} -> {last}" + (f"  under {args.prefix}" if args.prefix else ""))
    if grand == 0:
        print("  nothing tracked in this range")
        return 0
    for item in totals[: args.limit]:
        share = 100.0 * item.seconds / grand
        print(
            f"  {format_duration(item.seconds):>9}  {share:5.1f}%  "
            f"{item.category_path}  ({item.record_count})"
        )
    print(f"  {format_duration(grand):>9}  total")
    return 0


def cmd_day(args: argparse.Namespace, repository: Repository) -> int:
    stream = repository.get_stream(args.stream)
    if stream is None or stream.id is None:
        print(f"no stream {args.stream!r}; import something first", file=sys.stderr)
        return 2
    if stream.kind is not StreamKind.INTERVAL:
        print(
            f"{args.stream!r} is a {stream.kind} stream; 'day' only supports interval streams"
            " today",
            file=sys.stderr,
        )
        return 2

    start, end = _local_bounds(args.date, args.date)
    intervals = repository.intervals_in_range(stream.id, start, end)
    if not intervals:
        print(f"nothing tracked on {args.date}")
        return 0

    print(f"{stream.key}  {args.date}")
    for item in intervals:
        # A run may extend beyond the day it is shown under; mark the overhang
        # rather than silently clipping, matching how Timecard draws a
        # midnight-crossing run as one block.
        starts_before = item.start.epoch_seconds < start
        ends_after = item.end.epoch_seconds > end
        marker = "<" if starts_before else " "
        trailer = ">" if ends_after else ""
        print(
            f"  {marker}{item.start.local_datetime:%H:%M}-"
            f"{item.end.local_datetime:%H:%M}{trailer or ' '} "
            f"{format_duration(item.duration_seconds):>8}  {item.recorded_path}"
        )
    return 0


def cmd_periods(args: argparse.Namespace, repository: Repository) -> int:
    stream = repository.get_stream(args.stream)
    if stream is None or stream.id is None:
        print(f"no stream {args.stream!r}; import something first", file=sys.stderr)
        return 2

    period = Period.DAY if args.period == "day" else Period.HOUR
    totals = repository.totals_by_period(
        stream.id,
        period,
        start_bucket=args.since.isoformat() if args.since else None,
        end_bucket=(args.until.isoformat() + "T99")
        if args.until and period is Period.HOUR
        else (args.until.isoformat() if args.until else None),
        path_prefix=args.prefix,
    )
    if not totals:
        print("no rollup data in this range")
        return 0

    # An event stream's rollup rows carry seconds=0 -- there is no duration to
    # report -- so the bar and the figure are drawn from record_count instead.
    if stream.kind is StreamKind.EVENT:
        widest = max(item.record_count for item in totals)
        for item in totals:
            bar = "#" * max(1, round(40 * item.record_count / widest))
            print(f"  {item.bucket:<14} {item.record_count:>9}  {bar}")
        return 0

    widest = max(item.seconds for item in totals)
    for item in totals:
        bar = "#" * max(1, round(40 * item.seconds / widest))
        print(f"  {item.bucket:<14} {format_duration(item.seconds):>9}  {bar}")
    return 0


def cmd_ui(args: argparse.Namespace, repository: Repository) -> int:
    """Launch the desktop app.

    PySide6 is an optional dependency, so the import is deferred to here: every
    other command must keep working on an installation that never wanted a GUI.
    """
    try:
        from minmax.ui.app import run as run_ui
    except ImportError:
        print(
            "the desktop UI needs PySide6, which is not installed.\n"
            "  install it with:  uv sync --extra ui",
            file=sys.stderr,
        )
        return 2

    if repository.get_stream(args.stream) is None:
        print(f"no stream {args.stream!r}; import something first", file=sys.stderr)
        return 2

    from minmax.api import InProcessClient

    # The UI is not allowed to reach for the data directory itself, so the
    # location of its config is resolved out here and handed in.
    return run_ui(
        InProcessClient(repository, home=args.home),
        stream_key=args.stream,
        config_path=args.home / "shortcuts.json",
    )


def cmd_revert(args: argparse.Namespace, repository: Repository) -> int:
    deleted = repository.revert_import(args.import_run_id)
    print(f"reverted import {args.import_run_id}: {deleted} observations removed")
    return 0


def cmd_rebuild_rollups(args: argparse.Namespace, repository: Repository) -> int:
    rebuild(repository.connection)
    print("rollups rebuilt from observations")
    return 0


def cmd_correlate(args: argparse.Namespace, repository: Repository) -> int:
    """Sweep for correlations between streams across many period lengths.

    scipy is an optional dependency (see ``pyproject.toml``), checked here
    rather than left to fail deep inside the engine -- the same reason
    ``cmd_ui`` checks for PySide6 up front instead of importing ``ui.app``
    and letting the traceback speak for itself.
    """
    try:
        import scipy  # noqa: F401
    except ImportError:
        print(
            "the correlation engine needs scipy, which is not installed.\n"
            "  install it with:  uv sync --extra analysis",
            file=sys.stderr,
        )
        return 2

    for key in args.stream or ():
        if repository.get_stream(key) is None:
            print(f"no stream {key!r}; import something first", file=sys.stderr)
            return 2

    from minmax.api import InProcessClient

    client = InProcessClient(repository)
    view = client.correlations(
        stream_keys=args.stream,
        since=args.since,
        until=args.until,
        sweep=args.interval,
        min_periods=args.min_periods,
        min_r=args.min_r,
        max_q=args.max_q,
        top=None if args.top == 0 else args.top,
        per_category=args.per_category,
        include_fields=not args.no_fields,
        field_sum=args.field_sum,
        max_series=args.max_series,
        all_intervals=args.all_intervals,
    )

    if args.json:
        _print_correlations_json(view)
    else:
        _print_correlations(view)
    return 0


def _print_correlations(view: CorrelationsView) -> None:
    span = f"{view.first_day} -> {view.last_day}  " if view.first_day and view.last_day else ""
    print(f"{span}({view.series_count} series, {view.tests_performed} combinations tested)")
    if view.truncated_series:
        print(f"  ({view.truncated_series} series left out; raise --max-series to include them)")
    print()

    if not view.rows:
        print("  nothing cleared --min-r with the data available")
    for row in view.rows:
        flag = "*" if row.q_value <= 0.10 else " "
        print(
            f"  {row.r:+.2f}{flag} q={row.q_value:.2f}  n={row.n:<3} "
            f"{row.interval_label:<10} {row.x_label}  ~  {row.y_label}"
        )
        if row.stability < 1.0 or not row.consistent_sign:
            print(f"      consistent at {row.stability:.0%} of the periods tested")
        for caveat in row.caveats:
            print(f"      {caveat}")

    if view.skipped_notes:
        print()
        print("  not tested:")
        for note in view.skipped_notes:
            print(f"    {note}")

    print()
    print("  q is a Benjamini-Hochberg false-discovery rate over every combination tested.")
    print("  These are leads to test, not findings.")


def _print_correlations_json(view: CorrelationsView) -> None:
    import dataclasses
    import json

    def row_to_dict(row: CorrelationRow) -> dict[str, object]:
        data = dataclasses.asdict(row)
        data["first_period"] = row.first_period.isoformat()
        data["last_period"] = row.last_period.isoformat()
        return data

    payload = {
        "rows": [row_to_dict(row) for row in view.rows],
        "skipped_notes": view.skipped_notes,
        "tests_performed": view.tests_performed,
        "series_count": view.series_count,
        "truncated_series": view.truncated_series,
        "first_day": view.first_day.isoformat() if view.first_day else None,
        "last_day": view.last_day.isoformat() if view.last_day else None,
    }
    print(json.dumps(payload, indent=2))


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minmax", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--home",
        type=lambda value: Path(value).expanduser(),
        default=None,
        help="data directory (default: $MINMAX_HOME or ~/.minmax)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser("import", help="import a source file")
    importer.add_argument("path", help="file to import")
    importer.add_argument("--stream", default="timecard", help="stream key (default: timecard)")
    importer.add_argument(
        "--force", action="store_true", help="re-read a file whose content was already imported"
    )
    importer.set_defaults(handler=cmd_import)

    stream = subparsers.add_parser("stream", help="declare a new datastream")
    stream_commands = stream.add_subparsers(dest="stream_command", required=True)
    stream_add = stream_commands.add_parser(
        "add", help="declare a point-in-time stream and its column mapping"
    )
    stream_add.add_argument("--key", required=True, help="stable slug, e.g. 'heart_rate'")
    stream_add.add_argument("--name", required=True, help="human-facing label")
    stream_add.add_argument(
        "--kind", choices=("event",), default="event", help="stream kind (only 'event' today)"
    )
    stream_add.add_argument(
        "--from",
        dest="from_csv",
        required=True,
        type=lambda value: Path(value).expanduser(),
        help="a sample CSV to read the header and guess a column mapping from",
    )
    stream_add.add_argument(
        "--role",
        action="append",
        default=[],
        metavar="COLUMN=ROLE",
        help="override a guessed column role (at, category, source_id, number, text); repeatable",
    )
    stream_add.set_defaults(handler=cmd_stream_add)

    streams = subparsers.add_parser("streams", help="list every declared stream")
    streams.set_defaults(handler=cmd_streams)

    status = subparsers.add_parser("status", help="show streams, coverage, and recent imports")
    status.add_argument("--imports", type=int, default=3, help="recent imports to list")
    status.set_defaults(handler=cmd_status)

    totals = subparsers.add_parser("totals", help="time per category over a range")
    totals.add_argument("--stream", default="timecard")
    totals.add_argument("--since", type=_parse_date, help="first local day (YYYY-MM-DD)")
    totals.add_argument("--until", type=_parse_date, help="last local day (YYYY-MM-DD)")
    totals.add_argument("--prefix", help="restrict to a category and its descendants")
    totals.add_argument("--limit", type=int, default=25)
    totals.set_defaults(handler=cmd_totals)

    day = subparsers.add_parser("day", help="list a day's records")
    day.add_argument("date", type=_parse_date)
    day.add_argument("--stream", default="timecard")
    day.set_defaults(handler=cmd_day)

    periods = subparsers.add_parser("periods", help="totals per local hour or day, from rollups")
    periods.add_argument("--stream", default="timecard")
    periods.add_argument("--period", choices=("day", "hour"), default="day")
    periods.add_argument("--since", type=_parse_date)
    periods.add_argument("--until", type=_parse_date)
    periods.add_argument("--prefix")
    periods.set_defaults(handler=cmd_periods)

    ui = subparsers.add_parser("ui", help="open the desktop app")
    ui.add_argument("--stream", default="timecard")
    ui.set_defaults(handler=cmd_ui)

    revert = subparsers.add_parser("revert", help="undo an import by id")
    revert.add_argument("import_run_id", type=int)
    revert.set_defaults(handler=cmd_revert)

    rollups = subparsers.add_parser(
        "rebuild-rollups", help="recompute aggregates from observations"
    )
    rollups.set_defaults(handler=cmd_rebuild_rollups)

    correlate = subparsers.add_parser(
        "correlate", help="sweep for correlations between streams across many period lengths"
    )
    correlate.add_argument(
        "--stream", action="append", default=None, help="restrict to this stream (repeatable)"
    )
    correlate.add_argument("--since", type=_parse_date, help="first local day (YYYY-MM-DD)")
    correlate.add_argument("--until", type=_parse_date, help="last local day (YYYY-MM-DD)")
    correlate.add_argument(
        "--interval",
        dest="interval",
        type=_parse_sweep,
        default=None,
        metavar="UNIT[:COUNT][,UNIT[:COUNT]...]",
        help="period length(s) to test, e.g. 'day:3,week:1' "
        "(default: a built-in ladder from a day to a month)",
    )
    correlate.add_argument(
        "--min-periods",
        type=int,
        default=8,
        help="aligned periods required before a pair is even tested (default: 8)",
    )
    correlate.add_argument(
        "--min-r",
        type=float,
        default=0.5,
        help="only show pairs with |r| at least this (default: 0.5)",
    )
    correlate.add_argument(
        "--max-q", type=float, default=None, help="also require q at or below this"
    )
    correlate.add_argument(
        "--top", type=int, default=20, help="pairs to show (default: 20); 0 for no limit"
    )
    correlate.add_argument(
        "--per-category",
        action="store_true",
        help="also test each stream's top-level categories, not just its grand total",
    )
    correlate.add_argument(
        "--no-fields", action="store_true", help="skip numeric event-field measures"
    )
    correlate.add_argument(
        "--field-sum",
        action="store_true",
        help="also test a numeric field's sum, alongside its mean",
    )
    correlate.add_argument(
        "--all-intervals",
        action="store_true",
        help="show every period length tested per pair, not just its best",
    )
    correlate.add_argument(
        "--max-series", type=int, default=40, help="cap on candidate series (default: 40)"
    )
    correlate.add_argument("--json", action="store_true", help="machine-readable output")
    correlate.set_defaults(handler=cmd_correlate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home is None:
        args.home = default_home()

    connection = open_database(args.home / "minmax.db")
    try:
        return int(args.handler(args, Repository(connection)))
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
