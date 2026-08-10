-- Minmax schema v5: per-stream auto-fetch.
--
-- A stream can remember the filesystem path it was last pointed at and opt in
-- to Minmax re-importing that same path once a minute -- see
-- docs/architecture.md §6.2. Re-importing reuses the existing pipeline
-- unchanged: the natural-key constraint on each observation table is what
-- makes a periodic re-import of a growing file a merge rather than a
-- duplication, and archiving already skips unchanged content (run_import
-- hashes before archiving), so a file that has not grown since the last tick
-- costs one query, not one more archived copy.
--
-- Opt-in, not automatic for every stream: not every imported file is a live
-- feed worth polling, and a stream that has never been (re-)imported since
-- this shipped has no remembered path to resume from regardless -- only the
-- filename was ever stored before now (import_run.source_name), not the full
-- original path.

ALTER TABLE stream ADD COLUMN watch_path TEXT;
ALTER TABLE stream ADD COLUMN watch_enabled INTEGER NOT NULL DEFAULT 0 CHECK (watch_enabled IN (0, 1));
