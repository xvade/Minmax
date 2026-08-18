-- Minmax schema v7: a saved view -- a restriction of every screen to the
-- times one category was active in one interval stream. See
-- docs/architecture.md's decision D9.
--
-- category_path is nullable: NULL means "any activity in the source stream
-- at all," the same convention core.measures.Measure's category_path
-- already uses. It is stored as plain text rather than a category_id FK,
-- because a path outlives any one Category row (renaming a Timecard timer
-- re-paths it, same as everywhere else path matching is done by string
-- prefix rather than by joining to a specific row -- see
-- storage/repository.py's _prefix_clause).
--
-- ON DELETE CASCADE on source_stream_id: deleting a stream deletes the
-- views built on it, the same choice already made for every other table
-- that references stream (category, interval_record, rollup, ...). The UI
-- is responsible for falling back to "All data" if the active view
-- disappears out from under it.

CREATE TABLE view (
    id               INTEGER PRIMARY KEY,
    key              TEXT    NOT NULL UNIQUE,
    name             TEXT    NOT NULL,
    source_stream_id INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    category_path    TEXT,
    created_at       INTEGER NOT NULL
);

CREATE INDEX view_by_stream ON view (source_stream_id);
