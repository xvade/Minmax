"""Shared fixtures.

Every test runs against a throwaway database under ``tmp_path``. Nothing here
ever touches the user's real ``~/.minmax``, and no test depends on the machine's
timezone -- all fixture timestamps carry explicit offsets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from minmax.core.models import EventRecord, FieldRole, FieldSpec, IntervalRecord, Stream, StreamKind
from minmax.core.timeref import Instant
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


@pytest.fixture
def sessions_csv() -> Path:
    """A generic interval stream: start, end, a category-ish column, and a
    junk column -- plus one inverted row and one blank-end row for the
    rejection paths."""
    return FIXTURES / "sessions.csv"


@pytest.fixture
def readings_stream(repository: Repository) -> Stream:
    """A numeric-field event stream shaped to catch a mean-of-means bug: two
    categories, several readings per day, over several days, with unequal
    reading counts per day and one day where a category has no reading at
    all.

    Every existing numeric-field fixture is too degenerate for this:
    ``heart_rate.csv`` has no category column, and ``location.csv`` has one
    reading per category on a single day. Neither can catch a multi-day
    average computed as the mean of daily means instead of the correct
    value-weighted one, or exercise a per-category line with a real gap in
    it (see ``minmax.ui.trends_view.TrendsChart._line_values``).
    """
    stream = repository.create_stream(
        "readings",
        "Readings",
        StreamKind.EVENT,
        fields=[
            FieldSpec(
                key="value",
                label="Reading",
                role=FieldRole.NUMBER,
                source_column="value",
                unit="pt",
            )
        ],
    )
    assert stream.id is not None
    run = repository.begin_import(
        stream.id,
        importer="test",
        source_name="readings.csv",
        source_sha256="a" * 64,
        archived_path=None,
    )
    records = [
        # phone: day 1 has two readings, day 2 has one, day 3 has none at all.
        EventRecord(None, "phone", Instant.from_iso("2026-07-01T08:00:00-07:00"), {"value": 10.0}),
        EventRecord(None, "phone", Instant.from_iso("2026-07-01T09:00:00-07:00"), {"value": 20.0}),
        EventRecord(None, "phone", Instant.from_iso("2026-07-02T08:00:00-07:00"), {"value": 100.0}),
        # watch: a reading every day, so its line has no gap to contrast with
        # phone's, and its daily reading count never matches phone's.
        EventRecord(None, "watch", Instant.from_iso("2026-07-01T08:00:00-07:00"), {"value": 50.0}),
        EventRecord(None, "watch", Instant.from_iso("2026-07-02T08:00:00-07:00"), {"value": 10.0}),
        EventRecord(None, "watch", Instant.from_iso("2026-07-02T09:00:00-07:00"), {"value": 10.0}),
        EventRecord(None, "watch", Instant.from_iso("2026-07-02T10:00:00-07:00"), {"value": 10.0}),
        EventRecord(None, "watch", Instant.from_iso("2026-07-03T08:00:00-07:00"), {"value": 5.0}),
    ]
    repository.insert_events(stream, run, records)
    return stream


@dataclass(frozen=True, slots=True)
class ViewFixture:
    """Handles for :func:`viewed_streams`'s data -- a test reads better
    asking for "the view" or "the app usage stream" than indexing into a
    tuple."""

    timecard: Stream
    heart_rate: Stream
    app_usage: Stream
    view_key: str
    day: date


@pytest.fixture
def viewed_streams(repository: Repository) -> ViewFixture:
    """A view's source stream, plus two more streams shaped to catch a
    clipping bug when read through it -- every existing fixture is
    degenerate for this, since none has a category active in more than one
    window on a single day.

    ``timecard`` (exclusive) has its ``work`` category active in two
    separate windows, 08:00-10:00 and 13:00-15:00, with ``rest`` filling the
    gap between them. A single-window mask cannot distinguish "clip to a
    range" from "clip to a set of windows with a gap in the middle" -- this
    can.

    ``heart_rate`` has a reading inside each work window, one exactly on a
    window's closing edge (half-open, so it must be excluded, the same
    convention :meth:`~minmax.storage.repository.Repository.events_in_range`
    already uses), and one in the gap.

    ``app_usage`` (a second, unrelated interval stream) has one run spanning
    both work windows and the gap between them -- 09:00-14:00 -- to exercise
    a masked record splitting into two pieces (one per window it touches)
    that share one id.
    """
    day = date(2026, 7, 1)

    timecard = repository.create_stream("timecard", "Timecard", StreamKind.INTERVAL, exclusive=True)
    assert timecard.id is not None
    run = repository.begin_import(
        timecard.id,
        importer="test",
        source_name="t.csv",
        source_sha256="1" * 64,
        archived_path=None,
    )
    repository.insert_intervals(
        timecard,
        run,
        [
            IntervalRecord(
                None,
                "work",
                Instant.from_iso("2026-07-01T08:00:00-07:00"),
                Instant.from_iso("2026-07-01T10:00:00-07:00"),
            ),
            IntervalRecord(
                None,
                "rest",
                Instant.from_iso("2026-07-01T10:00:00-07:00"),
                Instant.from_iso("2026-07-01T13:00:00-07:00"),
            ),
            IntervalRecord(
                None,
                "work",
                Instant.from_iso("2026-07-01T13:00:00-07:00"),
                Instant.from_iso("2026-07-01T15:00:00-07:00"),
            ),
        ],
    )

    heart_rate = repository.create_stream(
        "heart_rate",
        "Heart Rate",
        StreamKind.EVENT,
        fields=[
            FieldSpec(
                key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
            )
        ],
    )
    assert heart_rate.id is not None
    hr_run = repository.begin_import(
        heart_rate.id,
        importer="test",
        source_name="hr.csv",
        source_sha256="2" * 64,
        archived_path=None,
    )
    repository.insert_events(
        heart_rate,
        hr_run,
        [
            # Inside the first work window.
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-07-01T09:00:00-07:00"), {"bpm": 70.0}
            ),
            # Exactly on the first window's closing edge -- excluded (half-open).
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-07-01T10:00:00-07:00"), {"bpm": 999.0}
            ),
            # In the gap between the two work windows -- excluded.
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-07-01T11:00:00-07:00"), {"bpm": 60.0}
            ),
            # Inside the second work window.
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-07-01T14:00:00-07:00"), {"bpm": 90.0}
            ),
        ],
    )

    app_usage = repository.create_stream("app_usage", "App Usage", StreamKind.INTERVAL)
    assert app_usage.id is not None
    au_run = repository.begin_import(
        app_usage.id,
        importer="test",
        source_name="a.csv",
        source_sha256="3" * 64,
        archived_path=None,
    )
    repository.insert_intervals(
        app_usage,
        au_run,
        [
            IntervalRecord(
                None,
                "coding",
                Instant.from_iso("2026-07-01T09:00:00-07:00"),
                Instant.from_iso("2026-07-01T14:00:00-07:00"),
            ),
        ],
    )

    view = repository.create_view("work-view", "Work", "timecard", category_path="work")

    return ViewFixture(
        timecard=timecard, heart_rate=heart_rate, app_usage=app_usage, view_key=view.key, day=day
    )


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
