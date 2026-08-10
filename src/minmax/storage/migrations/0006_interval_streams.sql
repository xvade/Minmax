-- Minmax schema v6: let stream_field describe an interval stream's column
-- mapping too, not only an event stream's -- start/end timestamps and a
-- generic "not stored" role. See docs/architecture.md §6.1: an interval's
-- payload is fixed-shape (a category and a span), so this only widens the
-- *mapping* table's CHECK; interval_record still has no EAV value table,
-- and NUMBER/TEXT stay invalid for an interval-kind declaration (enforced
-- in Python, since a role's legality depends on the owning stream's kind,
-- which SQL has no convenient way to check here).
--
-- SQLite cannot ALTER a CHECK constraint in place, so this is the same
-- create-copy-drop-rename rebuild 0003_colour_slots.sql used -- but with a
-- hazard 0003 didn't have and that got this migration wrong on the first
-- attempt (caught by a round-trip test before this ever touched a real
-- database): event_value.field_id and field_rollup.field_id both reference
-- stream_field(id) ON DELETE CASCADE, and foreign keys are ON for every
-- connection (storage/database.py). ALTER TABLE ... RENAME rewrites every
-- *other* table's REFERENCES clause to follow the new name -- so simply
-- dropping the old stream_field while a same-named new one already existed
-- would fire the cascade against whichever table currently has a live FK
-- reading "stream_field" by name, deleting a stream's payload and rollups
-- even though that table itself was never touched. PRAGMA foreign_keys
-- cannot be toggled mid-migration either (a documented no-op inside a
-- transaction, and the runner wraps this whole script in one).
--
-- The safe order: rename all three old tables out of the way first (their
-- own FK text follows automatically), build the three new tables under the
-- final names -- so anything created from here on binds to the *new*
-- stream_field -- copy the data across, then drop the "_old" tables leaves
-- first. At no point does a live table's FK still name something about to
-- be dropped.

ALTER TABLE stream_field RENAME TO stream_field_old;
ALTER TABLE event_value RENAME TO event_value_old;
ALTER TABLE field_rollup RENAME TO field_rollup_old;

CREATE TABLE stream_field (
    id            INTEGER PRIMARY KEY,
    stream_id     INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    key           TEXT    NOT NULL,
    label         TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (
        role IN ('at', 'start', 'end', 'category', 'source_id', 'number', 'text', 'ignored')
    ),
    unit          TEXT,
    position      INTEGER NOT NULL,
    source_column TEXT    NOT NULL
);

-- ids preserved explicitly: event_value_old/field_rollup_old's field_id
-- values must still resolve once they're copied into the new tables below.
INSERT INTO stream_field (id, stream_id, key, label, role, unit, position, source_column)
SELECT id, stream_id, key, label, role, unit, position, source_column FROM stream_field_old;

CREATE TABLE event_value (
    record_id INTEGER NOT NULL REFERENCES event_record (id) ON DELETE CASCADE,
    field_id  INTEGER NOT NULL REFERENCES stream_field (id) ON DELETE CASCADE,
    num       REAL,
    txt       TEXT,
    CHECK ((num IS NULL) <> (txt IS NULL)),
    PRIMARY KEY (record_id, field_id)
) WITHOUT ROWID;

INSERT INTO event_value (record_id, field_id, num, txt)
SELECT record_id, field_id, num, txt FROM event_value_old;

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

INSERT INTO field_rollup (
    stream_id, category_id, field_id, period, bucket, value_sum, value_count, value_min, value_max
)
SELECT stream_id, category_id, field_id, period, bucket, value_sum, value_count, value_min, value_max
FROM field_rollup_old;

-- Leaves first: by now nothing live references these names.
DROP TABLE event_value_old;
DROP TABLE field_rollup_old;
DROP TABLE stream_field_old;

CREATE UNIQUE INDEX stream_field_by_key ON stream_field (stream_id, key);
CREATE INDEX event_value_by_field ON event_value (field_id);
CREATE INDEX field_rollup_by_bucket ON field_rollup (stream_id, field_id, period, bucket);
