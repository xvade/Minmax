"""The raw-file archive: how fast it grows, and how it is cleaned up.

A feed that grows between imports hashes differently each time, so every import
of it archives a full new copy. These tests cover the two defences: unattended
imports archive a stream at most once per interval, and ``archive prune``
deletes copies that a later copy fully contains -- and only those.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from minmax.cli import main
from minmax.ingest import (
    UNATTENDED_ARCHIVE_INTERVAL,
    TimecardCsvImporter,
    apply_prune,
    plan_prune,
    run_import,
)
from minmax.ingest.prune import is_prefix_of
from minmax.storage.repository import Repository

HEADER = "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"


def row(index: int) -> str:
    """One distinct, non-overlapping one-hour run per index."""
    day, hour = divmod(index, 20)
    stamp = f"2026-07-{day + 1:02d}T{hour:02d}:00:00-07:00"
    stop = f"2026-07-{day + 1:02d}T{hour:02d}:59:59-07:00"
    return f"{index:08d}-0000-0000-0000-000000000001,work,{stamp},{stop},3599\n"


def feed(path: Path, rows: int) -> Path:
    """A Timecard export with ``rows`` runs -- call again with more to 'grow' it."""
    path.write_text(HEADER + "".join(row(i) for i in range(rows)))
    return path


def archived_files(home: Path) -> list[Path]:
    return sorted((home / "archive" / "timecard").glob("*.csv"))


def count_observations(repository: Repository) -> int:
    value = repository.connection.execute("SELECT COUNT(*) FROM interval_record").fetchone()[0]
    return int(value)


def referenced_paths(repository: Repository) -> list[str | None]:
    rows = repository.connection.execute("SELECT archived_path FROM import_run ORDER BY id")
    return [r[0] for r in rows.fetchall()]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return tmp_path / "feed" / "Timecard.csv"


class TestUnattendedImportsAreThrottled:
    def test_the_first_unattended_import_is_archived(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        source.parent.mkdir()
        report = run_import(
            repository,
            TimecardCsvImporter(),
            feed(source, 2),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )

        assert report.archived_path is not None
        assert len(archived_files(home)) == 1

    def test_a_grown_feed_is_not_archived_again_within_the_interval(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        first = run_import(
            repository,
            importer,
            feed(source, 2),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )
        second = run_import(
            repository,
            importer,
            feed(source, 3),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )

        # The new row is still imported; only the raw copy is skipped.
        assert second.inserted == 1
        assert count_observations(repository) == 3
        assert second.archived_path is None
        assert any("raw copy skipped" in note for note in second.notes)
        assert archived_files(home) == [first.archived_path]
        assert referenced_paths(repository) == [str(first.archived_path), None]

    def test_it_is_archived_again_once_the_interval_has_passed(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        run_import(
            repository,
            importer,
            feed(source, 2),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )
        started = repository.last_archived_at(1)
        assert started is not None
        later = started + int(UNATTENDED_ARCHIVE_INTERVAL.total_seconds()) + 1

        # Just inside the interval: still throttled. Just past it: archived.
        inside = run_import(
            repository,
            importer,
            feed(source, 3),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
            now=later - 2,
        )
        past = run_import(
            repository,
            importer,
            feed(source, 4),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
            now=later,
        )

        assert inside.archived_path is None
        assert past.archived_path is not None
        assert len(archived_files(home)) == 2

    def test_skipped_runs_do_not_push_the_next_copy_further_away(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        # If a throttled run counted as "the last archive", a feed imported every
        # minute would never be archived a second time.
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        run_import(
            repository,
            importer,
            feed(source, 1),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )
        started = repository.last_archived_at(1)
        assert started is not None

        for size in range(2, 8):
            run_import(
                repository,
                importer,
                feed(source, size),
                home=home,
                archive_every=UNATTENDED_ARCHIVE_INTERVAL,
                now=started + 60 * size,
            )

        assert repository.last_archived_at(1) == started

    def test_a_readers_own_import_is_always_archived(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        run_import(
            repository,
            importer,
            feed(source, 2),
            home=home,
            archive_every=UNATTENDED_ARCHIVE_INTERVAL,
        )
        manual = run_import(repository, importer, feed(source, 3), home=home)

        assert manual.archived_path is not None
        assert len(archived_files(home)) == 2


class TestIsPrefixOf:
    def test_a_file_is_a_prefix_of_one_that_extends_it(self, tmp_path: Path) -> None:
        short, long = tmp_path / "a", tmp_path / "b"
        short.write_bytes(b"abc")
        long.write_bytes(b"abcdef")
        assert is_prefix_of(short, long)

    def test_an_identical_file_counts(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(b"same")
        b.write_bytes(b"same")
        assert is_prefix_of(a, b)

    def test_a_longer_file_is_not_a_prefix_of_a_shorter_one(self, tmp_path: Path) -> None:
        short, long = tmp_path / "a", tmp_path / "b"
        short.write_bytes(b"abc")
        long.write_bytes(b"abcdef")
        assert not is_prefix_of(long, short)

    def test_a_changed_byte_anywhere_breaks_it(self, tmp_path: Path) -> None:
        # Past the first read chunk, so the comparison has to keep going.
        body = b"x" * (3 * (1 << 20))
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(body)
        b.write_bytes(body[:-5] + b"y" + body[-4:] + b"more")
        assert not is_prefix_of(a, b)


def grown_archive(repository: Repository, home: Path, source: Path, copies: int) -> list[Path]:
    """``copies`` full archived copies of a feed growing by one row each time."""
    source.parent.mkdir(exist_ok=True)
    importer = TimecardCsvImporter()
    paths = []
    for size in range(1, copies + 1):
        report = run_import(repository, importer, feed(source, size), home=home)
        assert report.archived_path is not None
        paths.append(report.archived_path)
    return paths


def backdate(connection: sqlite3.Connection, run_ids: list[int], *, days: int) -> None:
    marks = ",".join("?" * len(run_ids))
    connection.execute(
        f"UPDATE import_run SET started_at = started_at - ? WHERE id IN ({marks})",
        (days * 86400, *run_ids),
    )
    connection.commit()


class TestPrune:
    def test_older_copies_of_a_growing_file_are_deleted_and_the_newest_kept(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        paths = grown_archive(repository, home, source, 5)
        before = count_observations(repository)

        plan = plan_prune(repository, home)
        assert set(plan.delete) == {str(p) for p in paths[:-1]}
        assert plan.bytes_freed == sum(p.stat().st_size for p in paths[:-1])

        result = apply_prune(repository, plan)

        assert result.deleted == 4
        assert archived_files(home) == [paths[-1]]
        # The data itself is untouched; only the references to deleted files go.
        assert count_observations(repository) == before
        assert referenced_paths(repository) == [None, None, None, None, str(paths[-1])]
        assert result.runs_updated == 4

    def test_the_newest_copy_of_each_day_is_kept(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        paths = grown_archive(repository, home, source, 6)
        # Runs 1-3 happened two days ago, 4-6 today.
        backdate(repository.connection, [1, 2, 3], days=2)

        apply_prune(repository, plan_prune(repository, home))

        assert archived_files(home) == sorted([paths[2], paths[5]])

    def test_a_copy_the_later_one_does_not_contain_is_kept(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        first = run_import(repository, importer, feed(source, 3), home=home)
        # A *rewritten* export, not an extension: a different first row.
        source.write_text(HEADER + row(7) + "".join(row(i) for i in range(10, 12)))
        second = run_import(repository, importer, source, home=home)
        assert first.archived_path and second.archived_path

        plan = plan_prune(repository, home)

        assert plan.delete == {}
        assert plan.retained_unverified == 1
        apply_prune(repository, plan)
        assert archived_files(home) == sorted([first.archived_path, second.archived_path])

    def test_a_file_outside_the_archive_is_never_touched(
        self, repository: Repository, home: Path, source: Path, tmp_path: Path
    ) -> None:
        paths = grown_archive(repository, home, source, 3)
        elsewhere = tmp_path / "precious.csv"
        elsewhere.write_bytes(paths[0].read_bytes())
        repository.connection.execute(
            "UPDATE import_run SET archived_path = ? WHERE id = 1", (str(elsewhere),)
        )
        repository.connection.commit()

        plan = plan_prune(repository, home)
        apply_prune(repository, plan)

        assert elsewhere.exists()
        assert plan.retained_foreign == 1

    def test_a_reference_to_a_file_that_is_already_gone_is_cleared(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        paths = grown_archive(repository, home, source, 3)
        paths[0].unlink()

        plan = plan_prune(repository, home)
        assert plan.missing == (str(paths[0]),)
        apply_prune(repository, plan)

        assert referenced_paths(repository)[0] is None

    def test_pruning_twice_does_nothing_the_second_time(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        grown_archive(repository, home, source, 4)
        apply_prune(repository, plan_prune(repository, home))
        remaining = archived_files(home)

        again = plan_prune(repository, home)

        assert again.delete == {}
        assert again.missing == ()
        assert archived_files(home) == remaining

    def test_a_file_shared_by_several_runs_survives_if_any_run_keeps_it(
        self, repository: Repository, home: Path, source: Path
    ) -> None:
        # A forced re-import of identical content on the same day shares one
        # archived file between two runs. The file is the newest copy, so it stays.
        source.parent.mkdir()
        importer = TimecardCsvImporter()
        run_import(repository, importer, feed(source, 2), home=home)
        run_import(repository, importer, feed(source, 3), home=home)
        run_import(repository, importer, source, home=home, force=True)

        apply_prune(repository, plan_prune(repository, home))

        newest = referenced_paths(repository)[-1]
        assert newest is not None
        assert Path(newest).exists()
        assert referenced_paths(repository)[1] == newest

    def test_nothing_is_planned_for_an_empty_archive(
        self, repository: Repository, home: Path
    ) -> None:
        plan = plan_prune(repository, home)
        assert plan.delete == {}
        assert plan.bytes_freed == 0


class TestPruneCommand:
    def test_dry_run_deletes_nothing(
        self,
        repository: Repository,
        home: Path,
        source: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        grown_archive(repository, home, source, 4)
        before = archived_files(home)
        repository.connection.close()

        assert main(["--home", str(home), "archive", "prune", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "3 redundant copies" in out
        assert "dry run: nothing deleted" in out
        assert archived_files(home) == before

    def test_it_deletes_and_reports(
        self,
        repository: Repository,
        home: Path,
        source: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        paths = grown_archive(repository, home, source, 4)
        repository.connection.close()

        assert main(["--home", str(home), "archive", "prune"]) == 0

        assert "deleted 3 files" in capsys.readouterr().out
        assert archived_files(home) == [paths[-1]]


def test_the_interval_is_hours_not_minutes() -> None:
    # The whole point is to stop a once-a-minute feed archiving once a minute.
    assert timedelta(hours=1) <= UNATTENDED_ARCHIVE_INTERVAL
