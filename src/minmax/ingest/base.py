"""The importer contract and the shared import pipeline.

An importer's only job is to turn a foreign file into validated ``core``
objects. It does not decide storage layout, does not detect duplicates, and does
not touch the database -- :func:`run_import` handles all of that identically for
every source, so adding a stream later means writing a parser and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from minmax.core.models import IntervalRecord, StreamKind
from minmax.ingest.archive import archive_file, sha256_of
from minmax.storage.repository import Repository

__all__ = ["ImportReport", "Importer", "ParseResult", "Rejection", "run_import"]


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
    records: list[IntervalRecord] = field(default_factory=list)
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
) -> ImportReport:
    """Archive, parse, and store one source file.

    Ordering matters and is deliberate:

    1. Hash the file and, unless ``force``, skip it if this exact content has
       already been imported. This is only a shortcut -- correctness comes from
       the natural-key constraint, so a forced re-import is still a no-op rather
       than a duplication.
    2. Archive the raw bytes *before* parsing, so a parser that crashes still
       leaves the source recoverable.
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

    archived = archive_file(source, home=home, stream_key=stream.key)
    parsed = importer.parse(source)

    import_run_id = repository.begin_import(
        stream.id,
        importer=importer.name,
        source_name=source.name,
        source_sha256=digest,
        archived_path=str(archived.path),
    )

    try:
        outcome = repository.insert_intervals(stream, import_run_id, parsed.records)
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
        archived_path=archived.path,
        rows_read=parsed.rows_read,
        inserted=outcome.inserted,
        duplicates=outcome.duplicates,
        rejected=len(parsed.rejections),
        rejections=parsed.rejections,
        notes=parsed.notes,
        skipped=False,
    )
