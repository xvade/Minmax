-- Minmax schema v8: colours the user picked by hand.
--
-- group_color (0002/0003) stores the *automatic* slot every top-level group is
-- born with. This table stores an override on top of it, and no row means
-- "Auto": the series falls back to its group_color slot (see
-- minmax.core.palette.resolve_auto). Keeping the two apart is what makes
-- "back to Auto" a DELETE rather than a lost assignment.
--
-- category_path is the full path of the series as the trends chart names it --
-- 'media' at the top level, 'media/gaming' inside a focus -- stored as text
-- rather than a category_id for the same reason view.category_path is: a path
-- outlives any one category row.
--
-- A choice is a palette slot OR a custom hex, never both and never neither.
-- Slots rather than the slot's hex so a re-stepped palette still re-colours
-- the choice correctly (the reason 0003 stores slots at all).

CREATE TABLE series_colour (
    stream_id     INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_path TEXT    NOT NULL,
    slot          INTEGER,
    hex           TEXT,
    chosen_at     INTEGER NOT NULL,
    PRIMARY KEY (stream_id, category_path),
    CHECK ((slot IS NULL) <> (hex IS NULL)),
    CHECK (slot IS NULL OR slot >= 0),
    CHECK (hex IS NULL OR (length(hex) = 7 AND substr(hex, 1, 1) = '#'))
) WITHOUT ROWID;
