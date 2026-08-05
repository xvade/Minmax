-- Schema v3: store a palette *slot* per group rather than a hex colour.
--
-- Two reasons the hex was wrong:
--
--   1. A dark surface needs its own colour steps selected against it, not an
--      automatic inversion of the light ones. One stored hex cannot serve both.
--   2. The original palette failed accessibility validation -- one pair sat at
--      ΔE 9.9 for normal vision (floor 15) and another at 5.2 under protanopia
--      (target 8). Re-stepping it with a hex column stored would have meant a
--      data migration every time the palette improved.
--
-- Storing the slot index makes the palette a presentation detail again, which
-- is where it belongs. See minmax.core.palette.
--
-- Existing assignments cannot be carried across, because the colours they refer
-- to no longer exist. Groups are re-assigned deterministically in alphabetical
-- order, so the result is stable and reproducible rather than dependent on the
-- order rows happened to be inserted.

CREATE TABLE group_color_v3 (
    stream_id   INTEGER NOT NULL REFERENCES stream (id) ON DELETE CASCADE,
    top_level   TEXT    NOT NULL,
    slot        INTEGER NOT NULL,  -- index into minmax.core.palette.PALETTE
    assigned_at INTEGER NOT NULL,
    PRIMARY KEY (stream_id, top_level)
) WITHOUT ROWID;

INSERT INTO group_color_v3 (stream_id, top_level, slot, assigned_at)
SELECT stream_id,
       top_level,
       (ROW_NUMBER() OVER (PARTITION BY stream_id ORDER BY top_level) - 1) % 8,
       assigned_at
FROM group_color;

DROP TABLE group_color;
ALTER TABLE group_color_v3 RENAME TO group_color;
