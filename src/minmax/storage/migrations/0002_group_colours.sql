-- Schema v2: persisted colour per top-level category group.
--
-- Timecard assigns colour to a top-level folder rather than to each timer, so a
-- whole branch reads as one colour. Its CSV export carries no colours, so
-- Minmax assigns its own -- and stores them, because a colour derived on the
-- fly would either collide between groups (if hashed) or shift when a new group
-- appeared (if taken from a sorted index). Neither is acceptable for something
-- the user learns to recognize at a glance.

CREATE TABLE group_color (
    stream_id   INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    -- First component of a category path: 'media' for 'media/gaming/chess'.
    top_level   TEXT    NOT NULL,
    color       TEXT    NOT NULL,  -- '#RRGGBB'
    assigned_at INTEGER NOT NULL,
    PRIMARY KEY (stream_id, top_level)
) WITHOUT ROWID;
