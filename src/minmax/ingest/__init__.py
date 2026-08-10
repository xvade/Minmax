"""Importers and the shared import pipeline.

An importer parses one source format into ``core`` records. Archiving,
provenance, transactions, idempotency, and rollup maintenance are handled once
in :mod:`minmax.ingest.base`, so a new datastream needs only a parser.
"""

from minmax.core.models import StreamKind
from minmax.ingest.base import Importer, ImportReport, ParseResult, Rejection, run_import
from minmax.ingest.csvstream import (
    CsvStreamImporter,
    IntervalCsvImporter,
    guess_fields,
    read_sample,
    sniff_fields,
    validate_declaration,
)
from minmax.ingest.timecard import TimecardCsvImporter
from minmax.storage.repository import Repository

__all__ = [
    "CsvStreamImporter",
    "ImportReport",
    "Importer",
    "IntervalCsvImporter",
    "ParseResult",
    "Rejection",
    "TimecardCsvImporter",
    "guess_fields",
    "importer_for",
    "read_sample",
    "run_import",
    "sniff_fields",
    "validate_declaration",
]

#: Hardcoded importers by the stream key they always create, e.g. 'timecard'.
IMPORTERS: dict[str, type[Importer]] = {
    TimecardCsvImporter.stream_key: TimecardCsvImporter,
}


def importer_for(repository: Repository, stream_key: str) -> Importer:
    """The importer for one stream, whichever kind it is.

    A hardcoded importer (Timecard, today) is used when the key names one, so
    a first-ever import can still create that stream itself. Otherwise the
    stream must already exist -- created with its column mapping via
    :meth:`~minmax.storage.repository.Repository.create_stream` -- and a
    config-driven importer is built from that mapping. This is the one place
    ``cli.cmd_import`` and ``InProcessClient.import_file`` both look an
    importer up, replacing what used to be a duplicated ``IMPORTERS.get()``
    in each.
    """
    hardcoded = IMPORTERS.get(stream_key)
    if hardcoded is not None:
        return hardcoded()

    stream = repository.get_stream(stream_key)
    if stream is None:
        raise LookupError(f"no stream {stream_key!r}; create it first with 'minmax stream add'")
    assert stream.id is not None
    fields = repository.stream_fields(stream.id)
    if stream.kind is StreamKind.EVENT:
        return CsvStreamImporter(stream.key, stream.name, fields)
    if stream.kind is StreamKind.INTERVAL:
        return IntervalCsvImporter(stream.key, stream.name, fields, exclusive=stream.exclusive)
    raise ValueError(
        f"stream {stream_key!r} is a {stream.kind} stream; only point-in-time and interval"
        " streams can be imported into with the generic importer today"
    )
