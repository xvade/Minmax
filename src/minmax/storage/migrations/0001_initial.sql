-- Minmax schema v1: streams, categories, interval observations, import
-- provenance, and rollups.
--
-- Conventions used throughout:
--   * Instants are stored as UTC epoch seconds PLUS the UTC offset in effect at
--     that moment. See minmax.core.timeref for why both are required.
--   * Every observation carries the import_run_id that produced it, which is
--     what makes an import reversible.
--   * Table names avoid SQL keywords (interval_record, not interval).

CREATE TABLE stream (
    id          INTEGER PRIMARY KEY,
    key         TEXT    NOT NULL UNIQUE,   -- stable slug, e.g. 'timecard'
    name        TEXT    NOT NULL,          -- human-facing label
    kind        TEXT    NOT NULL CHECK (kind IN ('interval', 'event', 'sample')),
    -- 1 when at most one observation can be true at a time, so overlaps are a
    -- data error. Enforced by the repository inside the import transaction;
    -- SQLite has no exclusion constraints.
    exclusive   INTEGER NOT NULL DEFAULT 0 CHECK (exclusive IN (0, 1)),
    created_at  INTEGER NOT NULL
);

CREATE TABLE category (
    id            INTEGER PRIMARY KEY,
    stream_id     INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    -- The source's own stable id where it has one (Timecard's timer_id).
    -- This, not the path, is the category's identity: a timer that is renamed
    -- or moved keeps its id, so its history stays whole.
    source_id     TEXT,
    path          TEXT    NOT NULL,        -- most recently seen path
    top_level     TEXT    NOT NULL,        -- first path component; the UI colors by this
    first_seen_at INTEGER NOT NULL,        -- epoch seconds of earliest observation
    last_seen_at  INTEGER NOT NULL         -- epoch seconds of latest observation
);

-- Identity is (stream, source_id) when the source supplies one, and
-- (stream, path) when it does not. Two categories may share a path -- a timer
-- renamed away from a name another timer later takes -- so path is not unique
-- on its own.
CREATE UNIQUE INDEX category_by_source
    ON category (stream_id, source_id) WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX category_by_path
    ON category (stream_id, path) WHERE source_id IS NULL;
CREATE INDEX category_path_lookup ON category (stream_id, path);

CREATE TABLE import_run (
    id             INTEGER PRIMARY KEY,
    stream_id      INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    importer       TEXT    NOT NULL,       -- importer name, e.g. 'timecard-csv'
    source_name    TEXT    NOT NULL,       -- original filename
    source_sha256  TEXT    NOT NULL,       -- content hash of the raw file
    archived_path  TEXT,                   -- where the raw bytes were archived
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER,
    rows_read      INTEGER NOT NULL DEFAULT 0,
    rows_inserted  INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_rejected  INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);

CREATE INDEX import_run_by_hash ON import_run (stream_id, source_sha256);

CREATE TABLE interval_record (
    id                   INTEGER PRIMARY KEY,
    stream_id            INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_id          INTEGER NOT NULL REFERENCES category (id) ON DELETE CASCADE,
    import_run_id        INTEGER NOT NULL REFERENCES import_run (id),
    started_at           INTEGER NOT NULL, -- UTC epoch seconds
    ended_at             INTEGER NOT NULL,
    start_offset_seconds INTEGER NOT NULL, -- UTC offset in effect at the start
    end_offset_seconds   INTEGER NOT NULL, -- ...and at the end
    duration_seconds     INTEGER NOT NULL, -- derived; never taken from the source
    -- The category path as recorded at capture time, which may differ from
    -- category.path if the category was later renamed or moved.
    recorded_path        TEXT    NOT NULL,
    CHECK (ended_at >= started_at),
    CHECK (duration_seconds = ended_at - started_at)
);

-- Idempotency is enforced by the database, not by importers: re-importing a
-- file inserts nothing, because every row's natural key already exists.
CREATE UNIQUE INDEX interval_natural_key
    ON interval_record (stream_id, category_id, started_at, ended_at);
CREATE INDEX interval_by_time ON interval_record (stream_id, started_at, ended_at);
CREATE INDEX interval_by_import ON interval_record (import_run_id);

-- Precomputed local-time aggregates. Analysis reads these rather than the
-- observation tables: three years across twenty streams is ~10^5 rollup rows,
-- small enough to scan exhaustively however large the raw tables get.
--
-- An observation crossing a bucket boundary is split proportionally across the
-- buckets it touches (29 of the 338 runs in the first Timecard export cross
-- local midnight), while the observation itself stays a single row.
CREATE TABLE rollup (
    stream_id    INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL REFERENCES category (id) ON DELETE CASCADE,
    period       TEXT    NOT NULL CHECK (period IN ('hour', 'day')),
    -- Local bucket key: 'YYYY-MM-DD' for day, 'YYYY-MM-DDTHH' for hour. These
    -- sort lexicographically in chronological order, so range queries over
    -- rollups are plain string comparisons.
    bucket       TEXT    NOT NULL,
    seconds      INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stream_id, category_id, period, bucket)
) WITHOUT ROWID;

CREATE INDEX rollup_by_bucket ON rollup (stream_id, period, bucket);
