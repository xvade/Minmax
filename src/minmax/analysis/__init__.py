"""Statistics over stored data: the correlation engine.

Reads ``storage`` (rollups, not raw observation rows -- see
``docs/architecture.md`` §7 and design principle 6) and ``core``. Writes
nothing to the record of what happened; it may only ever add its own derived
tables if a future need arises. Imported by ``api/`` alone -- never by
``ui/``, which a test enforces.
"""

from __future__ import annotations

from minmax.analysis.correlate import correlate
from minmax.analysis.models import Correlation, CorrelationReport, PairResult, Skipped
from minmax.analysis.sweep import DEFAULT_SWEEP

__all__ = [
    "DEFAULT_SWEEP",
    "Correlation",
    "CorrelationReport",
    "PairResult",
    "Skipped",
    "correlate",
]
