"""Persistence: SQLite schema, migrations, the repository, and rollups.

This is the only package that knows SQL exists. If a statement appears outside
it, that is a bug -- the repository is the seam that lets the storage engine
change without touching callers.
"""
