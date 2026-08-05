"""Minmax -- a personal data hub for time-bound life data.

Layering (see ``docs/architecture.md``); dependencies point one direction only:

    core -> storage -> ingest / analysis -> api -> ui

- ``minmax.core``    domain model; pure, no I/O
- ``minmax.storage`` SQLite schema, repository, rollups; the only layer with SQL
- ``minmax.ingest``  importers and the shared import pipeline
- ``minmax.cli``     command-line entry point
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
