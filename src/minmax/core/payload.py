"""Canonical hashing of a point-in-time record's payload.

An interval's natural key is its category and its span; nothing else about it
can vary independently. A point-in-time record has no span to key on, and its
payload is a plain instant, so two distinct readings at the same instant --
two GPS fixes a millisecond apart, both truncated to the same second -- must
both survive import while a re-import of the same file is still a no-op. The
natural key therefore includes a digest of the record's own field values; see
``docs/architecture.md`` §6.1.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

__all__ = ["payload_hash"]


def payload_hash(values: Mapping[str, float | str]) -> str:
    """A stable digest of ``values``, keyed by field slug.

    Order-independent (fields are hashed in sorted-key order), so callers may
    pass any mapping. Numbers and text hash differently even when their
    reprs would collide (``1`` the number vs. ``"1"`` the string), because
    each entry is tagged with its kind before hashing.

    A record with no declared fields hashes to a constant -- documented, not
    hidden: two field-less events at the same instant collapse into one under
    the natural key. If that ever matters for a real stream, the fix is to
    declare a field that distinguishes them, not to change this function.
    """
    digest = hashlib.sha256()
    for key in sorted(values):
        value = values[key]
        tag = "t" if isinstance(value, str) else "n"
        digest.update(f"{key}\0{tag}\0{value!r}\0".encode())
    return digest.hexdigest()
