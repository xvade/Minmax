"""Pruning superseded copies out of the raw-file archive.

The archive's rule is that files are only ever added. This module is the one
deliberate exception, for a specific situation: a feed that grows between
imports archives a complete new copy each time, and each copy is the previous
one plus a few rows. Before the throttle in :func:`~minmax.ingest.base.run_import`
existed, auto-fetch made one per minute and the archive reached 99 GB.

Nothing is deleted unless it is provably redundant. A copy is removed only when
**all** of these hold:

* it belongs to an import run the database knows about, and lives under
  ``<home>/archive`` (a file Minmax cannot account for is never touched);
* a later copy of the same source file from the same UTC day exists, and is kept;
* its bytes are a *prefix* of that later copy -- so everything it held is still
  held. A source that was rewritten rather than appended to fails this test, and
  its copies are kept.

The newest copy per (stream, source name, day) always survives, and so does the
newest copy overall.

Planning (:func:`plan_prune`) reads only; :func:`apply_prune` does the deleting.
The CLI runs the plan first so the user sees what would go.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from minmax.core.models import ArchivedImport
from minmax.storage.repository import Repository

__all__ = ["PrunePlan", "PruneResult", "apply_prune", "is_prefix_of", "plan_prune"]

_CHUNK_BYTES = 1 << 20


def is_prefix_of(shorter: Path, longer: Path) -> bool:
    """Whether ``longer`` begins with exactly the bytes of ``shorter``."""
    if shorter.stat().st_size > longer.stat().st_size:
        return False
    with shorter.open("rb") as first, longer.open("rb") as second:
        while chunk := first.read(_CHUNK_BYTES):
            if second.read(len(chunk)) != chunk:
                return False
    return True


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """What :func:`apply_prune` would do."""

    #: Files to delete, with the size each frees.
    delete: dict[str, int] = field(default_factory=dict)
    #: Referenced files that are already gone; only the reference is cleared.
    missing: tuple[str, ...] = ()
    #: Copies left alone because they are not a prefix of the one kept.
    retained_unverified: int = 0
    #: Copies left alone because they sit outside the archive directory.
    retained_foreign: int = 0
    #: Files deleted, per stream key.
    per_stream: dict[str, int] = field(default_factory=dict)

    @property
    def bytes_freed(self) -> int:
        return sum(self.delete.values())


@dataclass(frozen=True, slots=True)
class PruneResult:
    deleted: int
    bytes_freed: int
    runs_updated: int
    errors: tuple[str, ...] = ()


def _day(item: ArchivedImport) -> str:
    return datetime.fromtimestamp(item.started_at, tz=UTC).strftime("%Y-%m-%d")


def plan_prune(
    repository: Repository,
    home: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> PrunePlan:
    """Decide which archived copies are redundant, without touching anything."""
    archive_root = (home / "archive").resolve()
    groups: dict[tuple[str, str, str], list[ArchivedImport]] = defaultdict(list)
    for item in repository.archived_imports():
        groups[(item.stream_key, item.source_name, _day(item))].append(item)

    # The newest run of each group keeps its file; anything else is a candidate.
    keepers: dict[tuple[str, str, str], str] = {}
    protected: set[str] = set()
    for key, items in groups.items():
        keepers[key] = items[-1].path  # archived_imports() is ordered by run id
        protected.add(items[-1].path)

    candidates: list[tuple[tuple[str, str, str], str]] = []
    seen: set[str] = set()
    for key, items in groups.items():
        for item in items[:-1]:
            if item.path in protected or item.path in seen:
                continue
            seen.add(item.path)
            candidates.append((key, item.path))

    delete: dict[str, int] = {}
    missing: list[str] = []
    per_stream: dict[str, int] = defaultdict(int)
    unverified = 0
    foreign = 0
    for done, (key, raw_path) in enumerate(candidates, start=1):
        if progress is not None and done % 1000 == 0:
            progress(done, len(candidates))
        path = Path(raw_path)
        if not path.resolve().is_relative_to(archive_root):
            foreign += 1
            continue
        if not path.is_file():
            missing.append(raw_path)
            continue
        keeper = Path(keepers[key])
        if not keeper.is_file():
            unverified += 1  # nothing to prove redundancy against
            continue
        if is_prefix_of(path, keeper):
            delete[raw_path] = path.stat().st_size
            per_stream[key[0]] += 1
        else:
            unverified += 1

    return PrunePlan(
        delete=delete,
        missing=tuple(missing),
        retained_unverified=unverified,
        retained_foreign=foreign,
        per_stream=dict(per_stream),
    )


def apply_prune(repository: Repository, plan: PrunePlan) -> PruneResult:
    """Delete the planned files, then forget them in the database.

    Files go first: if this is interrupted, the database still names files that
    no longer exist, which the next plan simply reports as ``missing`` and
    clears. The reverse order could leave files nobody knows about.
    """
    gone: list[str] = list(plan.missing)
    freed = 0
    errors: list[str] = []
    for raw_path, size in plan.delete.items():
        try:
            Path(raw_path).unlink()
        except FileNotFoundError:
            gone.append(raw_path)
        except OSError as error:
            errors.append(f"{raw_path}: {error}")
        else:
            gone.append(raw_path)
            freed += size
    updated = repository.clear_archived_paths(gone) if gone else 0
    return PruneResult(
        deleted=len(plan.delete) - len(errors),
        bytes_freed=freed,
        runs_updated=updated,
        errors=tuple(errors),
    )
