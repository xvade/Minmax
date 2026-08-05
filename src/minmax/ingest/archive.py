"""Immutable archiving of raw source files.

Every file Minmax imports is copied byte-for-byte before it is parsed. This is
the difference between "the parser had a bug" being a re-import and being a
permanent loss: the original export may be long deleted off the phone by the
time the bug is found.

Archived files are never modified or overwritten, only added.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["ArchivedFile", "archive_file", "sha256_of"]

_CHUNK_BYTES = 1 << 20


def sha256_of(path: Path) -> str:
    """Content hash of a file, read in chunks so size is not a constraint."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    path: Path
    sha256: str
    original_name: str
    already_present: bool


def archive_file(source: Path, *, home: Path, stream_key: str) -> ArchivedFile:
    """Copy ``source`` into the archive under ``home``.

    The stored name embeds the import date and a hash prefix, so re-importing
    the same file is recognized as already archived (and skipped) while two
    genuinely different exports that happen to share a filename cannot collide.
    """
    digest = sha256_of(source)
    destination_dir = home / "archive" / stream_key
    destination_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(tz=UTC).strftime("%Y%m%d")
    destination = destination_dir / f"{stamp}-{digest[:12]}-{source.name}"

    if destination.exists():
        return ArchivedFile(destination, digest, source.name, already_present=True)

    # Copy to a temporary name and rename into place, so an interrupted copy
    # never leaves a truncated file looking like a complete archive entry.
    staging = destination.with_suffix(destination.suffix + ".partial")
    shutil.copy2(source, staging)
    staging.replace(destination)
    return ArchivedFile(destination, digest, source.name, already_present=False)
