"""The importer contract and the shared import pipeline.

An importer's only job is to turn a foreign file into validated ``core``
objects. It does not decide storage layout, does not detect duplicates, and does
not touch the database -- :func:`run_import` handles all of that identically for
every source, so adding a stream later means writing a parser and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from minmax.core.models import EventRecord, IntervalRecord, StreamKind
from minmax.ingest.archive import ArchivedFile, archive_file, sha256_of
from minmax.storage.repository import Repository

__all__ = [
    "UNATTENDED_ARCHIVE_INTERVAL",
    "ImportReport",
    "Importer",
    "ParseResult",
    "Rejection",
    "run_import",
]


#: How often an *unattended* import (auto-fetch) may add a full copy of a stream's
#: source to the archive. Manual imports and the CLI always archive. A feed that
#: grows between imports hashes differently every time, so without this cap a
#: once-a-minute auto-fetch writes a complete new copy every minute -- tens of
#: gigabytes within weeks. Six hours bounds a 1.2 MB feed to ~5 MB a day while
#: leaving the live source file as the record in between.
UNATTENDED_ARCHIVE_INTERVAL = timedelta(hours=6)


def _hours(span: timedelta) -> str:
    """``span`` for a note: minutes under an hour, otherwise hours."""
    minutes = round(span.total_seconds() / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes / 60:.1f} h"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One source row that could not be trusted, and why.

    Rejections are reported rather than raised: one malformed row in a
    thousand-row export should not cost the user the other 999. A row is only
    ever dropped, never guessed at.
    """

    line: int
    reason: str
    raw: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    records: list[IntervalRecord | EventRecord] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rows_read(self) -> int:
        return len(self.records) + len(self.rejections)


class Importer(Protocol):
    """Turns one source file into records. Stateless and side-effect free."""

    name: str
    stream_key: str
    stream_name: str
    stream_kind: StreamKind
    stream_exclusive: bool

    def parse(self, path: Path) -> ParseResult: ...


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an import did, suitable for showing to the user verbatim."""

    stream_key: str
    source_name: str
    source_sha256: str
    import_run_id: int | None
    archived_path: Path | None
    rows_read: int
    inserted: int
    duplicates: int
    rejected: int
    rejections: Sequence[Rejection]
    notes: Sequence[str]
    skipped: bool

    @property
    def changed_anything(self) -> bool:
        return self.inserted > 0


def run_import(
    repository: Repository,
    importer: Importer,
    source: Path,
    *,
    home: Path,
    force: bool = False,
    archive_every: timedelta | None = None,
    now: int | None = None,
) -> ImportReport:
    """Archive, parse, and store one source file.

    Ordering matters and is deliberate:

    1. Hash the file and, unless ``force``, skip it if this exact content has
       already been imported. This is only a shortcut -- correctness comes from
       the natural-key constraint, so a forced re-import is still a no-op rather
       than a duplication.
    2. Archive the raw bytes *before* parsing, so a parser that crashes still
       leaves the source recoverable. With ``archive_every`` set (unattended
       imports), the copy is skipped when this stream was archived more recently
       than that: a feed re-imported every minute would otherwise archive a
       full copy of a growing file every minute. The run then records no
       archived file (``archived_path`` is ``NULL``) and the source file itself
       remains the record until the next copy is due. ``now`` is the current
       time in epoch seconds, injectable for tests.
    3. Parse. A parse failure creates no import run at all, leaving no
       half-finished provenance behind.
    4. Insert inside one transaction. If it fails, nothing is stored and the run
       is left unfinished (``finished_at IS NULL``), which is what marks it as
       failed and keeps a later retry from being skipped as already-done.
    """
    stream = repository.ensure_stream(
        importer.stream_key,
        importer.stream_name,
        importer.stream_kind,
        exclusive=importer.stream_exclusive,
    )
    assert stream.id is not None  # ensure_stream always returns a persisted stream

    digest = sha256_of(source)
    if not force:
        previous = repository.find_import_by_hash(stream.id, digest)
        if previous is not None:
            return ImportReport(
                stream_key=stream.key,
                source_name=source.name,
                source_sha256=digest,
                import_run_id=previous.id,
                archived_path=Path(previous.archived_path) if previous.archived_path else None,
                rows_read=previous.rows_read,
                inserted=0,
                duplicates=previous.rows_inserted,
                rejected=previous.rows_rejected,
                rejections=(),
                notes=[
                    f"identical content already imported as run {previous.id}; pass force to re-run"
                ],
                skipped=True,
            )

    archived: ArchivedFile | None = None
    throttled_note: str | None = None
    if archive_every is not None:
        last = repository.last_archived_at(stream.id)
        moment = int(time.time()) if now is None else now
        if last is not None and moment - last < archive_every.total_seconds():
            age = _hours(timedelta(seconds=moment - last))
            throttled_note = (
                f"raw copy skipped: this stream was archived {age} ago "
                f"(unattended imports archive at most every {_hours(archive_every)})"
            )
    if throttled_note is None:
        archived = archive_file(source, home=home, stream_key=stream.key)
    parsed = importer.parse(source)

    import_run_id = repository.begin_import(
        stream.id,
        importer=importer.name,
        source_name=source.name,
        source_sha256=digest,
        archived_path=str(archived.path) if archived is not None else None,
    )

    try:
        outcome = repository.insert_records(stream, import_run_id, parsed.records)
    except Exception as error:
        repository.fail_import(import_run_id, str(error))
        raise

    repository.finish_import(
        import_run_id,
        rows_read=parsed.rows_read,
        rows_inserted=outcome.inserted,
        rows_duplicate=outcome.duplicates,
        rows_rejected=len(parsed.rejections),
        notes="; ".join(parsed.notes) or None,
    )

    return ImportReport(
        stream_key=stream.key,
        source_name=source.name,
        source_sha256=digest,
        import_run_id=import_run_id,
        archived_path=archived.path if archived is not None else None,
        rows_read=parsed.rows_read,
        inserted=outcome.inserted,
        duplicates=outcome.duplicates,
        rejected=len(parsed.rejections),
        rejections=parsed.rejections,
        notes=[*parsed.notes, throttled_note] if throttled_note else parsed.notes,
        skipped=False,
    )
