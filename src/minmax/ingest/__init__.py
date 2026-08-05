"""Importers and the shared import pipeline.

An importer parses one source format into ``core`` records. Archiving,
provenance, transactions, idempotency, and rollup maintenance are handled once
in :mod:`minmax.ingest.base`, so a new datastream needs only a parser.
"""

from minmax.ingest.base import Importer, ImportReport, ParseResult, Rejection, run_import
from minmax.ingest.timecard import TimecardCsvImporter

__all__ = [
    "ImportReport",
    "Importer",
    "ParseResult",
    "Rejection",
    "TimecardCsvImporter",
    "run_import",
]

#: Importers by the name used on the command line.
IMPORTERS: dict[str, type] = {
    TimecardCsvImporter.stream_key: TimecardCsvImporter,
}
