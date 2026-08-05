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
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from pathlib import Path

from minmax.api.format import format_duration
from minmax.core.timeref import Period
from minmax.ingest import IMPORTERS, run_import
from minmax.storage.database import default_home, open_database
from minmax.storage.repository import OverlapError, Repository
from minmax.storage.rollups import rebuild

__all__ = ["format_duration", "main"]


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from error


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
    importer_class = IMPORTERS.get(args.stream)
    if importer_class is None:
        known = ", ".join(sorted(IMPORTERS))
        print(f"unknown stream {args.stream!r}; known streams: {known}", file=sys.stderr)
        return 2

    source = Path(args.path).expanduser()
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2

    try:
        report = run_import(repository, importer_class(), source, home=args.home, force=args.force)
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

    return run_ui(InProcessClient(repository), stream_key=args.stream)


def cmd_revert(args: argparse.Namespace, repository: Repository) -> int:
    deleted = repository.revert_import(args.import_run_id)
    print(f"reverted import {args.import_run_id}: {deleted} observations removed")
    return 0


def cmd_rebuild_rollups(args: argparse.Namespace, repository: Repository) -> int:
    rebuild(repository.connection)
    print("rollups rebuilt from observations")
    return 0


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
