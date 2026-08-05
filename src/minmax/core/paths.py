"""Hierarchical category paths.

A category is identified to the user by a slash-delimited path such as
``media/gaming/chess``. Minmax stores the path whole and *derives* the hierarchy
from it, rather than storing a parent-pointer tree.

This mirrors Timecard's own model and earns the same properties: adding a level
needs no migration, there are no empty-folder edge cases, and rolling up by
prefix (``media/*``) is a string operation rather than a recursive query. The
cost is that renaming a branch re-paths its descendants -- acceptable, and rare.
"""

from __future__ import annotations

__all__ = [
    "SEPARATOR",
    "ancestors",
    "components",
    "is_descendant",
    "leaf",
    "normalize",
    "top_level",
]

SEPARATOR = "/"


def normalize(raw: str) -> str:
    """Canonicalize a raw path string.

    Trims whitespace around each component and drops empty components, so
    ``" media / gaming //chess "`` and ``"media/gaming/chess"`` are the same
    category. Without this, a stray space in a source file would silently create
    a second, near-identical category and split that activity's history in two.
    """
    parts = [part.strip() for part in raw.split(SEPARATOR)]
    kept = [part for part in parts if part]
    if not kept:
        raise ValueError(f"path {raw!r} has no non-empty components")
    return SEPARATOR.join(kept)


def components(path: str) -> list[str]:
    """The path's components, outermost first."""
    return normalize(path).split(SEPARATOR)


def leaf(path: str) -> str:
    """The display name: the last component (``chess`` of ``media/gaming/chess``)."""
    return components(path)[-1]


def top_level(path: str) -> str:
    """The outermost component (``media``).

    This is the grouping Timecard assigns color by, so the UI uses it to give a
    whole branch one color at a glance.
    """
    return components(path)[0]


def ancestors(path: str, *, include_self: bool = True) -> list[str]:
    """Every prefix path, outermost first.

    ``media/gaming/chess`` yields ``['media', 'media/gaming', 'media/gaming/chess']``.
    Used to attribute one record to each level of the hierarchy it belongs to.
    """
    parts = components(path)
    stop = len(parts) if include_self else len(parts) - 1
    return [SEPARATOR.join(parts[: i + 1]) for i in range(stop)]


def is_descendant(path: str, prefix: str, *, include_self: bool = True) -> bool:
    """Whether ``path`` sits under ``prefix``.

    Compares component-wise, so ``media`` does not match ``mediation`` the way a
    naive ``startswith`` would.
    """
    path_parts = components(path)
    prefix_parts = components(prefix)
    if len(path_parts) < len(prefix_parts):
        return False
    if not include_self and len(path_parts) == len(prefix_parts):
        return False
    return path_parts[: len(prefix_parts)] == prefix_parts
