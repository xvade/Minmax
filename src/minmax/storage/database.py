"""SQLite connection setup and schema migration.

Minmax's store of record is a single SQLite file. That choice buys ACID writes,
constraint enforcement, and a backup story that is "copy one file" -- all of
which the data-safety invariant in ``AGENTS.md`` demands and none of which a
directory of flat files provides.

This module owns three things and nothing else: where the database lives, the
PRAGMA settings it is opened with, and the migration runner. No queries.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "ENV_HOME",
    "connect",
    "default_database_path",
    "default_home",
    "migrate",
    "open_database",
    "transaction",
]

ENV_HOME = "MINMAX_HOME"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def default_home() -> Path:
    """The directory holding the database and the raw-import archive.

    Overridable via ``$MINMAX_HOME``, which is what the tests use to stay well
    away from real data.
    """
    override = os.environ.get(ENV_HOME)
    return Path(override).expanduser() if override else Path.home() / ".minmax"


def default_database_path() -> Path:
    return default_home() / "minmax.db"


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with Minmax's durability settings applied."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None disables the driver's implicit transaction handling
    # so that transaction boundaries are explicit and visible -- see
    # transaction() below.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # Referential integrity is off by default in SQLite and is per-connection.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader (the UI) run concurrently with a writer (an import).
    conn.execute("PRAGMA journal_mode = WAL")
    # FULL, not the WAL default of NORMAL: NORMAL can lose the most recent
    # transactions on power loss. This data is irreplaceable and writes are
    # rare, so the durability is worth far more than the throughput.
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction, rolling back on any exception.

    ``BEGIN IMMEDIATE`` takes the write lock up front rather than on first
    write, so two concurrent imports fail fast instead of one dying partway
    through with a busy error.

    Nesting is a no-op: an inner call joins the transaction already in progress
    so that a repository method is atomic on its own but composes into a larger
    atomic import.
    """
    if conn.in_transaction:
        yield conn
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migration_files() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_PATTERN.match(path.name)
        if match is None:
            raise RuntimeError(f"migration {path.name!r} does not match NNNN_lower_snake.sql")
        found.append((int(match.group(1)), path))

    for index, (version, path) in enumerate(found, start=1):
        if version != index:
            raise RuntimeError(
                f"migration versions must be consecutive from 0001; got {version} "
                f"({path.name}) where {index} was expected"
            )
    return found


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations the database has not seen, returning its version.

    The applied version is tracked in SQLite's own ``user_version`` pragma, so
    no bookkeeping table is needed and a fresh file needs no bootstrapping.
    Each migration runs in its own transaction: a failure leaves the database at
    the last version that fully applied, never halfway through one.
    """
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]

    for version, path in _migration_files():
        if version <= current:
            continue
        # The BEGIN/COMMIT are inside the script rather than around it because
        # executescript() commits any transaction open when it starts. Wrapping
        # the version bump in the same script is what makes "schema applied" and
        # "version recorded" a single atomic fact.
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + path.read_text(encoding="utf-8")
            + f"\nPRAGMA user_version = {version};\nCOMMIT;\n"
        )
        current = version

    return current


def open_database(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database and bring its schema up to date."""
    conn = connect(path if path is not None else default_database_path())
    migrate(conn)
    return conn
