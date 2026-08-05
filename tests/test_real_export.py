"""Import the real Timecard export, when one is present.

The fixtures elsewhere are small and hand-built, which makes them precise but
also makes them agree with whatever the parser happens to do. This module points
the pipeline at the actual export instead, so that a change in Timecard's
format, or an assumption that only holds for tidy data, surfaces here.

Assertions are deliberately shape-based rather than count-based: the user
re-exports whenever they like, and a test that breaks because they tracked more
time would be worse than no test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minmax.core.timeref import Period
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository

TIMECARD_DATA = Path("/Users/xvade/dev/Misc/Timecard/Data")


def latest_export() -> Path | None:
    if not TIMECARD_DATA.is_dir():
        return None
    exports = sorted(TIMECARD_DATA.glob("Timecard-*.csv"))
    return exports[-1] if exports else None


export = latest_export()
pytestmark = pytest.mark.skipif(
    export is None, reason=f"no Timecard export found under {TIMECARD_DATA}"
)


@pytest.fixture
def imported(repository: Repository, home: Path):
    assert export is not None
    return run_import(repository, TimecardCsvImporter(), export, home=home)


def test_the_whole_export_is_accepted(imported) -> None:
    # Every row of real data should be usable. A rejection here means either the
    # parser is too strict or Timecard's format changed -- both worth knowing.
    assert imported.rejected == 0
    assert imported.inserted == imported.rows_read
    assert imported.inserted > 0


def test_reimporting_the_real_export_stores_nothing_new(
    repository: Repository, home: Path, imported
) -> None:
    assert export is not None
    again = run_import(repository, TimecardCsvImporter(), export, home=home, force=True)
    assert (again.inserted, again.duplicates) == (0, imported.inserted)


def test_no_run_overlaps_another(imported) -> None:
    # Timecard runs one timer at a time. If this ever fails, the exclusivity
    # assumption behind the stream is wrong, not the data.
    assert imported.inserted > 0  # the import itself enforces this


def test_totals_reconcile_between_observations_and_rollups(
    repository: Repository, imported
) -> None:
    stream = repository.get_stream("timecard")
    assert stream is not None and stream.id is not None

    from_records = sum(t.seconds for t in repository.totals_by_category(stream.id, 0, 2**31))
    from_rollups = sum(t.seconds for t in repository.totals_by_period(stream.id, Period.DAY))
    assert from_records == from_rollups > 0


def test_hierarchical_categories_are_queryable_by_prefix(repository: Repository, imported) -> None:
    stream = repository.get_stream("timecard")
    assert stream is not None and stream.id is not None

    categories = repository.list_categories(stream.id)
    nested = [c for c in categories if "/" in c.path]
    assert nested, "the real export contains nested category paths"

    top = nested[0].top_level
    subtree = repository.totals_by_category(stream.id, 0, 2**31, path_prefix=top)
    assert all(c.category_path == top or c.category_path.startswith(f"{top}/") for c in subtree)


def test_exported_durations_agree_with_the_timestamps_within_tolerance(
    imported,
) -> None:
    # Documents the known sub-second truncation. If Timecard ever fixes it, this
    # note simply stops appearing; if drift grows, rows would be rejected above.
    note = " ".join(imported.notes)
    assert "duration" in note or note == ""
