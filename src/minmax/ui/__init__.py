"""The PySide6 desktop app.

Everything in this package imports from :mod:`minmax.api` and nothing else --
never ``storage``, ``ingest``, or ``analysis``. That is the invariant from
``AGENTS.md`` that redoing the UI must not affect the underlying systems, and it
is what lets a future web client reuse the same facade unchanged.

Logic worth unit-testing lives one layer down: the timeline arithmetic is in
:mod:`minmax.core.timeline` and the presentation values every client must agree
on are in :mod:`minmax.api`. What remains here is paint and input handling.

PySide6 is an optional dependency (``uv sync --extra ui``); importing this
package without it raises, which :mod:`minmax.cli` turns into a readable message.
"""
