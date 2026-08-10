-- Minmax schema v4: point-in-time (event) streams -- a per-stream declared
-- column mapping (stream_field), the observations themselves (event_record),
-- and their payload (event_value). See docs/architecture.md §6.1 for the
-- reasoning: an interval stream's payload is fixed-shape, a point-in-time
-- stream's is not (nothing, one number, several numbers, or text), so the
-- payload is stored as attribute rows rather than a JSON column, which keeps
-- the database able to reject a malformed value instead of trusting the
-- producer.

-- The column-role mapping a user declares once for a point-in-time stream,
-- reused for every later import into it (the CSV's header names change
-- source_column, not this table).
CREATE TABLE stream_field (
    id            INTEGER PRIMARY KEY,
    stream_id     INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    key           TEXT    NOT NULL,   -- slug identifying the field within the stream
    label         TEXT    NOT NULL,   -- human-facing label (the inspector, the dialog)
    role          TEXT    NOT NULL CHECK (role IN ('at', 'category', 'source_id', 'number', 'text')),
    unit          TEXT,               -- e.g. 'bpm'; NULL for non-numeric fields
    position      INTEGER NOT NULL,   -- display and import order
    source_column TEXT    NOT NULL    -- the CSV header this field is read from
);

CREATE UNIQUE INDEX stream_field_by_key ON stream_field (stream_id, key);

CREATE TABLE event_record (
    id             INTEGER PRIMARY KEY,
    stream_id      INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_id    INTEGER NOT NULL REFERENCES category (id) ON DELETE CASCADE,
    import_run_id  INTEGER NOT NULL REFERENCES import_run (id),
    occurred_at    INTEGER NOT NULL, -- UTC epoch seconds
    offset_seconds INTEGER NOT NULL, -- UTC offset in effect at that moment
    -- The category path as recorded at capture time -- see interval_record.
    recorded_path  TEXT    NOT NULL,
    -- Digest of this record's field values (core.payload.payload_hash), so two
    -- distinct readings at the same instant both survive while a re-import of
    -- the same file is still a no-op. See docs/architecture.md §6.1.
    payload_hash   TEXT    NOT NULL
);

-- Idempotency, as for interval_record: a re-import inserts nothing because
-- every row's natural key already exists. payload_hash is part of the key
-- (not just stream/category/instant) so two distinct readings at the same
-- instant both survive.
CREATE UNIQUE INDEX event_natural_key
    ON event_record (stream_id, category_id, occurred_at, payload_hash);
CREATE INDEX event_by_time   ON event_record (stream_id, occurred_at);
CREATE INDEX event_by_import ON event_record (import_run_id);

CREATE TABLE event_value (
    record_id INTEGER NOT NULL REFERENCES event_record (id) ON DELETE CASCADE,
    field_id  INTEGER NOT NULL REFERENCES stream_field (id) ON DELETE CASCADE,
    num       REAL,
    txt       TEXT,
    CHECK ((num IS NULL) <> (txt IS NULL)),
    PRIMARY KEY (record_id, field_id)
) WITHOUT ROWID;

CREATE INDEX event_value_by_field ON event_value (field_id);

-- Numeric aggregates per field, mirroring `rollup` but keyed additionally by
-- field since one event stream may declare several numeric fields (a
-- location stream's latitude and longitude, say). A mean is never stored --
-- only value_sum and value_count are, and mean is derived as
-- value_sum / value_count at read time, so a multi-day column's mean is the
-- correct value-weighted mean rather than a mean of daily means.
CREATE TABLE field_rollup (
    stream_id    INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL REFERENCES category (id) ON DELETE CASCADE,
    field_id     INTEGER NOT NULL REFERENCES stream_field (id) ON DELETE CASCADE,
    period       TEXT    NOT NULL CHECK (period IN ('hour', 'day')),
    bucket       TEXT    NOT NULL,
    value_sum    REAL    NOT NULL DEFAULT 0,
    value_count  INTEGER NOT NULL DEFAULT 0,
    value_min    REAL,
    value_max    REAL,
    PRIMARY KEY (stream_id, category_id, field_id, period, bucket)
) WITHOUT ROWID;

CREATE INDEX field_rollup_by_bucket ON field_rollup (stream_id, field_id, period, bucket);
