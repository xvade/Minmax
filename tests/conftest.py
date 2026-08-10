"""Shared fixtures.

Every test runs against a throwaway database under ``tmp_path``. Nothing here
ever touches the user's real ``~/.minmax``, and no test depends on the machine's
timezone -- all fixture timestamps carry explicit offsets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from minmax.storage.database import open_database
from minmax.storage.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A private Minmax data directory for one test."""
    path = tmp_path / "minmax-home"
    path.mkdir()
    return path


@pytest.fixture
def connection(home: Path) -> Iterator[sqlite3.Connection]:
    conn = open_database(home / "minmax.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def repository(connection: sqlite3.Connection) -> Repository:
    return Repository(connection)


@pytest.fixture
def sample_csv() -> Path:
    """A small Timecard export exercising the real data's quirks.

    Contains a run crossing local midnight, a row whose exported duration is one
    second off the timestamps, and one timer id appearing under two names.
    """
    return FIXTURES / "timecard_sample.csv"


@pytest.fixture
def malformed_csv() -> Path:
    """One usable row followed by six distinct ways of being unusable."""
    return FIXTURES / "timecard_malformed.csv"


@pytest.fixture
def pickups_csv() -> Path:
    """A point-in-time stream with no payload beyond the instant."""
    return FIXTURES / "pickups.csv"


@pytest.fixture
def heart_rate_csv() -> Path:
    """A point-in-time stream with one numeric field, plus one bad row."""
    return FIXTURES / "heart_rate.csv"


@pytest.fixture
def location_csv() -> Path:
    """A point-in-time stream with a category, a source id, two numbers, and
    an optional text field left blank on one row."""
    return FIXTURES / "location.csv"


def data_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Everything that must be identical after a redundant re-import.

    Excludes ``import_run``, which legitimately gains a row each time an import
    is attempted; the point of the idempotency invariant is that the *data* is
    unchanged, not that the audit trail is.
    """
    tables = {
        "category": "SELECT source_id, path, top_level, first_seen_at, last_seen_at"
        " FROM category ORDER BY path",
        "interval_record": "SELECT category_id, started_at, ended_at, duration_seconds,"
        " recorded_path FROM interval_record ORDER BY started_at, ended_at",
        "rollup": "SELECT category_id, period, bucket, seconds, record_count"
        " FROM rollup ORDER BY period, bucket, category_id",
        "event_record": "SELECT category_id, occurred_at, offset_seconds, recorded_path,"
        " payload_hash FROM event_record ORDER BY occurred_at, payload_hash",
        "event_value": "SELECT record_id, field_id, num, txt"
        " FROM event_value ORDER BY record_id, field_id",
        "field_rollup": "SELECT category_id, field_id, period, bucket, value_sum, value_count,"
        " value_min, value_max FROM field_rollup ORDER BY period, bucket, field_id, category_id",
    }
    return {
        name: [tuple(row) for row in conn.execute(query).fetchall()]
        for name, query in tables.items()
    }
