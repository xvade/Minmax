"""Running imports off the UI thread.

An import hashes, archives, parses and stores a whole file. On the data this
app is used with -- CSVs of tens of thousands of rows, re-imported every minute
by auto-fetch -- that is seconds of work, and done on the UI thread it is a
frozen window and a spinning beachball.

:class:`ImportRunner` runs :meth:`MinmaxClient.import_file` on a single
background thread and hands the result back on the UI thread, where dialogs and
screen refreshes belong. One thread, not a pool of them: imports write to one
database, so running two at once only makes them wait on each other, and a queue
preserves the order the user asked for them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, Signal

from minmax.api import ImportOutcome, MinmaxClient

__all__ = ["ImportJob", "ImportKind", "ImportResult", "ImportRunner"]


class ImportKind(Enum):
    """Where a job came from, which decides what to do when it finishes."""

    MANUAL = "manual"
    """A file chosen or dropped by the reader."""

    AUTO = "auto"
    """A once-a-minute auto-fetch tick."""

    AUTO_RETRY = "auto_retry"
    """Auto-fetch retrying with a replacement file the reader just chose. A second
    failure pauses auto-fetch rather than asking again."""


@dataclass(frozen=True, slots=True)
class ImportJob:
    path: Path
    stream_key: str
    force: bool = False
    kind: ImportKind = ImportKind.MANUAL
    name: str = ""
    """The stream's display name, for auto-fetch messages."""

    switch_after: bool = False
    """Switch the window to ``stream_key`` once the import has been reported --
    for a stream just created *by* this import, which has no data to show until
    it lands."""

    @property
    def description(self) -> str:
        verb = "auto-fetching" if self.kind is not ImportKind.MANUAL else "importing"
        return f"{verb} {self.path.name}"


@dataclass(frozen=True, slots=True)
class ImportResult:
    job: ImportJob
    outcome: ImportOutcome | None = None
    error: Exception | None = None
    """Set when the client raised rather than returning an outcome. The client
    reports a bad file as an ``ImportOutcome``; an exception is something else
    (no data directory, a bug), and is shown as-is."""


class _Relay(QObject):
    """Carries a result from the worker thread to the UI thread.

    A signal emitted from a non-Qt thread is delivered to its receivers on *their*
    thread, which is what makes the hand-back safe.
    """

    done = Signal(object)


class _Task(QRunnable):
    def __init__(self, client: MinmaxClient, job: ImportJob, relay: _Relay) -> None:
        super().__init__()
        self._client = client
        self._job = job
        self._relay = relay

    def run(self) -> None:
        try:
            outcome = self._client.import_file(
                self._job.path,
                stream_key=self._job.stream_key,
                force=self._job.force,
                # Auto-fetch re-imports a growing file every minute; only the
                # reader's own imports are always archived.
                unattended=self._job.kind is not ImportKind.MANUAL,
            )
            result = ImportResult(self._job, outcome=outcome)
        except Exception as error:
            # Whatever happened, the UI must hear about it: a worker that dies
            # silently would leave the busy indicator on forever.
            result = ImportResult(self._job, error=error)
        self._relay.done.emit(result)


class ImportRunner(QObject):
    """Queue imports, run them one at a time, report each on the UI thread."""

    finished = Signal(object)
    """Emitted on the UI thread with an :class:`ImportResult`."""

    activityChanged = Signal(object)
    """Emitted when the running/queued set changes: the job now at the front, or
    ``None`` once nothing is left. Drives the busy indicator."""

    def __init__(self, client: MinmaxClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._relay = _Relay(self)
        self._relay.done.connect(self._on_done)
        self._outstanding: list[ImportJob] = []

    @property
    def busy(self) -> bool:
        return bool(self._outstanding)

    def is_importing(self, stream_key: str) -> bool:
        """Whether a job for this stream is queued or running."""
        return any(job.stream_key == stream_key for job in self._outstanding)

    def submit(self, job: ImportJob) -> None:
        self._outstanding.append(job)
        self._pool.start(_Task(self._client, job, self._relay))
        self.activityChanged.emit(self._outstanding[0])

    def wait(self, timeout_ms: int = 60_000) -> bool:
        """Block until every submitted job has finished *and been reported*.

        For tests and for quitting. Not for ordinary use: the point of this class
        is that nothing waits. Returns whether everything finished in time.
        """
        waited = 0
        while self._outstanding and waited < timeout_ms:
            self._pool.waitForDone(20)
            # Results arrive as queued signals; they are only delivered by
            # spinning the event loop.
            QCoreApplication.processEvents()
            waited += 20
        return not self._outstanding

    def _on_done(self, result: ImportResult) -> None:
        if result.job in self._outstanding:
            self._outstanding.remove(result.job)
        self.activityChanged.emit(self._outstanding[0] if self._outstanding else None)
        self.finished.emit(result)
