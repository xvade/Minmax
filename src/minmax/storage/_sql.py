"""Tiny SQL-string helpers shared by :mod:`repository` and :mod:`masked`.

Both modules join a raw or rolled-up table to ``category`` and need the same
prefix-matching clause; pulled out here once so a masked read and a rollup
read cannot silently drift into matching a category prefix two different ways.
"""

from __future__ import annotations

from minmax.core import paths

__all__ = ["escape_like", "prefix_clause"]


def prefix_clause(path_prefix: str | None, *, alias: str = "c") -> tuple[str, tuple[str, ...]]:
    """Restrict to a category and its descendants.

    Matching is ``= prefix OR LIKE prefix || '/%'`` rather than a bare
    ``LIKE prefix || '%'``, so ``media`` does not also match ``mediation``.
    """
    if path_prefix is None:
        return "", ()
    normalized = paths.normalize(path_prefix)
    return f" AND ({alias}.path = ? OR {alias}.path LIKE ? ESCAPE '\\')", (
        normalized,
        escape_like(normalized) + "/%",
    )


def escape_like(value: str) -> str:
    """Neutralize LIKE wildcards in a category path used as a literal prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
