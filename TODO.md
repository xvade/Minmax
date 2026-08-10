# TODO

Top-level tasks are human-authored. Subtasks (indented) are added by agents and
must be kept current — an unchecked subtask is a promise to whoever picks this up
next.

---

- [x] Begin Minmax's documentation by writing an overview of the code structure needed to eventually accomplish the goals outlined in `AGENTS.md`.
  - [x] Write `docs/architecture.md` — layers, dependency rules, domain model, module map, storage design, UI boundary, testing strategy.
  - [x] Record the design decisions with rationale and revisit triggers (`docs/architecture.md` § Key decisions).
  - [x] Write `README.md` as the project front door.

- [x] Decide on a method for storing data, and a programming language to write in. Run these by the user before proceeding.
  - [x] Language: **Python**. Approved by user 2026-08-05. (D1)
  - [x] Storage: **SQLite** as store of record, with rollup tables for analysis and DuckDB reserved as a later read-only analytics engine. Approved by user 2026-08-05. (D2)
  - [x] UI toolkit: **PySide6/Qt** native desktop app. Approved by user 2026-08-05. (D3)
  - [x] UI boundary: **`MinmaxClient` facade**, in-process now, HTTP implementation added when a web UI arrives. Approved by user 2026-08-05. (D4)

- [x] Complete the first goal in `AGENTS.md` "Minmax will support efficient import, storage, and retrieval for timetracking data". Timetracking data will be imported from Timecard (`/Users/xvade/dev/Misc/Timecard/Data`).
  - [x] Project scaffolding: `pyproject.toml`, `src/minmax/` package layout, pytest + ruff + mypy configuration.
  - [x] `core/` — `StreamKind`, `Stream`, `Category`, `IntervalRecord`; category path parsing; offset-aware time handling and local-period bucketing.
  - [x] `storage/` — numbered migrations, connection/PRAGMA setup, migration runner, `Repository` read/write API.
  - [x] `storage/rollups.py` — hourly and daily aggregate maintenance, applied incrementally inside the import transaction, plus a full `rebuild` repair path.
  - [x] `ingest/` — `Importer` protocol, raw-file archive with SHA-256 content hashing, `import_run` provenance, reversible imports.
  - [x] `ingest/timecard.py` — Timecard CSV parser (RFC 4180, ISO 8601 with offset) → validated `IntervalRecord`s.
  - [x] Retrieval queries: records in a time range, totals by category, totals by category prefix (`media/*`), hourly/daily breakdowns from rollups.
  - [x] Tests: 122 covering core rules, storage round-trips and constraint *rejection*, importer golden-file and malformed-input cases, idempotency, and the real Timecard export.
  - [x] CLI entry point (`import`, `status`, `totals`, `day`, `periods`, `revert`, `rebuild-rollups`).
  - [x] Document the import/retrieval feature in `README.md`.

- [x] Complete the second goal in `AGENTS.md` "Minmax will include a convenient user interface for viewing timetracking data". Use Timecard as a model for how to display timetracking data.
      *Everything planned under this goal is built. The unchecked items below are
      follow-ups found while building it, not gaps in the goal — except the dark-mode
      check, which cannot be done without a display.*
  - [x] Define the `MinmaxClient` protocol operations the UI needs; implement `InProcessClient`.
  - [x] PySide6 app shell: window, keyboard-shortcut registry, light/dark following system appearance.
  - [x] Day-timeline view modelled on Timecard's history screen — hour grid, blocks sized to real duration, coloured by top-level category, labels shown when tall enough, midnight-crossing runs marked rather than clipped.
  - [x] Zoom of hour density, anchored under the pointer on Ctrl/Cmd+scroll; fit-day on open.
  - [x] Summary panel: totals by category for the shown day, with share bars.
  - [x] Keyboard-first navigation (day movement, jump to date, zoom, fit, shortcut help).
  - [x] Persisted per-group colours (`group_color` table, migration 0002) so a category's colour is stable and distinct.
  - [x] Tests: timeline arithmetic, the client facade, and headless UI wiring; a test that fails if `ui/` ever imports the backend.
  - [x] Document the UI in `README.md`.
  - [x] Category filter (`/` to restrict the timeline and totals to a path prefix), cleared with `Escape`.
  - [x] Scroll past the day boundary to load adjacent days, the way Timecard's history scrolls continuously, instead of stepping day by day. `MinmaxClient.window()` returns a run of days; the pane extends it at either edge and anchors the scroll position so growing it is invisible.
  - [x] Trends screen: side-scrolling stacked area over days, zoomable, with the visible date span and each group's share of it always on screen.
  - [x] Re-step the category palette on validated colours (light and dark), storing palette slots rather than hex so it can change without a data migration (migration 0003).
  - [x] Drill-down: focus a group on the trends screen to split it into its sub-categories.
  - [x] Click a block to inspect it (full path, exact times, which import it came from).
  - [x] Remappable shortcuts persisted to a config file (`~/.minmax/shortcuts.json`), edited in the Shortcuts dialog. `ui/shortcuts.py` is the single source the actions, the dialog, and the file all come from.
  - [x] Trends: a range picker, so the chart can be pinned to a fixed window rather than always covering all history.
  - [x] Trends: weekly and monthly aggregation, so multi-year spans do not rely on one column per day. Columns are calendar-aligned (`core/columns.py`) and plotted as a daily average so the y axis means the same thing at every granularity.
  - [x] Make the interval a trends column covers adjustable, rather than only day/week/month. Requested by user 2026-08-05.
    - [x] `core/columns.py`: an `Interval` of a unit and a count (`3 days`, `2 weeks`, `3 months`), anchored to a fixed epoch so multi-period columns stay put when the range changes.
    - [x] Thread it through `MinmaxClient.trends()` and `TrendsView`, and label a multi-period column as the span it covers rather than as its first period.
    - [x] An interval dialog (`Shift`+`D`) on the trends screen, alongside the `D` cycle of the common presets.
    - [x] Tests for the anchoring, and for the invariant that every day still lands in exactly one column at any count.
    - [x] `fit_all` was capped by `MAX_PIXELS_PER_COLUMN` and left a three-column chart stranded against the left edge. `fit_column_zoom` is floored but not capped; the ceiling now bounds manual zoom only.
    - [x] `trends.cycle_granularity` became `trends.cycle_interval`. `shortcuts._RENAMED` carries a saved remap across the rename instead of reporting it as an unknown action.
  - [x] Add file import to the desktop app -- a file dialog and drag-and-drop, so a Timecard export does not require dropping to the CLI. Requested by user 2026-08-06.
    - [x] `MinmaxClient.import_file()` / `InProcessClient.import_file()`, thin over `ingest.run_import` -- same archiving, provenance, and idempotency as the CLI's `import` command, returning an `ImportOutcome` instead of printing one. `InProcessClient` takes an optional `home` (`cmd_ui` now passes `args.home`); a client built without one refuses to import rather than guessing a directory.
    - [x] `Ctrl+I` / "Import…" (global scope, so it works from either screen) opens a file dialog; dropping a file onto the window does the same. Both funnel through `MainWindow._import_path`, the one place that talks to the client, reports the outcome, and offers to force a re-import of already-seen content.
    - [x] A successful import with new rows refreshes the day screen's loaded window (anchored, so scroll position holds) and reloads the trends screen; a duplicate-only re-import does not, since nothing changed.
    - [x] Tests: `import_file` against the protocol (fresh import, skip-and-force, malformed file, unknown stream, overlap conflict), plus UI wiring tests including a real `QDropEvent` (not a mock) end to end.
  - [ ] Confirm dark mode on a real display. It has been verified offscreen with a forced dark palette (chrome, day rules, date chips, and the inspector all follow; category colours hold up), but never seen under the actual macOS dark appearance. **This one needs a human at a screen.**
  - [ ] Unify the two ways of narrowing what is shown: the day screen has a path-prefix filter, the trends screen has a drill-down focus stack. They mean the same thing and should probably be one concept that survives switching screens.
  - [ ] The Shortcuts dialog captures single key sequences only (`setMaximumSequenceLength(1)`). Chords such as `Ctrl+K, Ctrl+S` are rejected rather than stored; decide whether they are worth supporting before the action list grows.
  - [ ] A clipped end column on the trends chart is flagged in the data (`TrendColumn.clipped`) and named honestly in its tooltip, but nothing on the chart itself says "this week is only three days long".
- [x] Add the ability to add new datastreams (both in the backend and as part of the GUI). For now, a datastream should consist of either a single time at which the datapoint was collected (along with the data itself), or a period of time (same as what Timecard outputs). Imports for all data should be in csv, with time data being the last 1 or 3 columns (depending on the type of the stream).
      *Clarified with the user 2026-08-06: a point-in-time row's payload is
      arbitrary -- nothing, one number, several numbers, or free text -- so
      column **roles** (category / source id / value / time) are configured
      per stream from the GUI (it reads the CSV header and offers each column
      a role), not inferred from position; that mapping is stored on the
      stream and reused for every later import. `StreamKind.EVENT` already
      exists in `core/models.py`; `StreamKind.SAMPLE` (10^6-10^8 row/year data
      needing a different physical layout) is out of scope here -- an event
      stream covers everything asked for at its actual scale.*
      *The full loop the user asked for -- create a stream, import into it,
      switch between streams, and see it rendered on both the day timeline
      and the trends chart -- is built and verified end to end (a real
      offscreen `MainWindow` run, not just unit tests). The unchecked items
      below are follow-ups found while building it or deliberate scope cuts,
      not gaps in what was asked for -- see each subtask for which.*
  - [x] Storage: `stream_field` (the declared column-role schema, per stream)
    and `event_record` / `event_value` tables (migration `0004`), an EAV
    shape rather than a JSON payload column so the database still rejects
    malformed values instead of trusting producers. Natural key is
    `(stream, category, occurred_at, payload_hash)`, hashing the row's field
    values so two distinct readings at the same instant both survive.
    `Repository.observed_range` and `revert_import` were interval-only and
    now cover event streams too; `rollups.rebuild` no longer excludes
    non-interval streams via its old `kind = 'interval'` filter (a latent bug
    independent of this feature, fixed alongside it). `insert_records`
    dispatches by the *stream's* kind and raises on a mismatch rather than
    silently storing nothing.
  - [x] `field_rollup`: per-field sum/count/min/max per bucket, alongside
    reusing the existing `rollup` table for "how many events" (`record_count`
    is already summable across buckets for a stream with no duration). Mean
    is always derived (`value_sum/value_count`), never stored, so a
    multi-day column's mean is correctly weighted rather than a mean of
    daily means. Tests: rebuild equals incremental for an event stream
    (mirroring the existing interval oracle test), the payload-hash
    collision is explicit and named, `event_value`'s CHECK, FK cascade on
    delete, `create_stream` rejects a duplicate key.
  - [x] A generic, config-driven CSV importer (`ingest/csvstream.py`) for any
    user-created stream, driven by its stored `stream_field` mapping.
    `TimecardCsvImporter` keeps its own hand-written class -- its duration
    drift tolerance is knowledge about one source, not a column mapping, and
    it is the only stream with golden tests against the real export. Row
    parsing helpers common to both (`RowError`, timestamp/int parsing, row
    rendering) moved to `ingest/rows.py`. `ingest.importer_for(repository,
    stream_key)` is now the one place that resolves an importer -- a
    hardcoded one (Timecard) for a key that names it, otherwise a
    `CsvStreamImporter` built from the stream's stored fields -- replacing a
    duplicated `IMPORTERS.get()` lookup that used to live in both `cli.py`
    and `api/client.py`.
    - **Scope cut, logged rather than silently dropped:** only point-in-time
      (`event`) custom streams go through the generic importer. A second
      Timecard-*shaped* custom stream (interval, with a start and an end
      column) is not implemented -- `stream_field`/EAV storage exists
      specifically for the arbitrary point-in-time payload the user asked
      about, and `interval_record` has no per-row payload to map. `cli.py`'s
      `totals`/`day` commands now refuse a non-interval stream with a clear
      message (they read `interval_record` directly) rather than reporting
      a misleading "no observations"; `periods` was fixed to read
      `record_count` instead of `seconds` for an event stream, since its bar
      chart divided by zero otherwise (a real bug, not just a gap).
  - [x] CLI: `minmax stream add --key --name --from <csv> [--role
    COLUMN=ROLE ...]` (column roles are guessed from the CSV's header and
    sample rows -- confidently for the timestamp, conservatively [number vs.
    text] for everything else, via `ingest.sniff_fields`, the same guess the
    GUI's new-stream dialog will offer) and `minmax streams` (list, mirroring
    `status`'s enumeration, plus each event stream's declared fields).
    `minmax periods --measure` is still open, pending the `Measure` concept.
  - [x] `api`: `create_stream` and `inspect_csv` (reads a CSV's header and
    guesses a mapping through `ingest.read_sample`/`guess_fields`, so `ui/`
    never has to reach past the facade). `day`/`window`/`range_totals`/
    `trends` now dispatch on the stream's kind: an event stream's records
    come back as `TimelineRecord(is_instant=True, ...)` with declared field
    values as formatted `(label, value)` pairs, and every view that used to
    carry only seconds now also carries `stream_kind`, since a point-in-time
    stream has counts where an interval one has duration.
    - **Scope cut, logged rather than silently dropped: no `Measure`
      abstraction (sum vs. mean vs. min vs. max of a numeric field) yet.**
      `field_rollup` (Phase 1) already stores what that needs, untouched by
      this cut. What shipped instead is narrower and reuses the *existing*
      seconds-shaped machinery: an event stream's "how much" is its
      **count**, which is summable exactly like seconds is (`rollups.
      apply_events` already writes `record_count` for this), so
      `trends()`/`range_totals()`/day totals now sum `record_count` instead
      of `seconds` for an event stream, through the same stacking, the same
      daily-average axis, the same share-of-window legend math -- no new
      concept, no unstacked-line rendering, no "can this be divided by
      day-count" question, because a count answers yes to everything a
      duration did. The field names (`TrendsView.total_seconds`,
      `TrendSeries.values`, `DayView.tracked_seconds`, ...) still say
      "seconds" and now dual-purpose as a count when `stream_kind` is
      `EVENT` -- documented on each field rather than renamed, to keep this
      change's diff to "which column gets summed," not "rename every reader
      of these fields." A real `Measure` type (needed the moment someone
      wants a *numeric field's* mean/min/max on the chart, e.g. average
      heart rate per day) is future work with no half-finished scaffolding
      left behind to work around.
  - [x] GUI: a stream picker (`Ctrl+K` opens it) above the day/trends screens,
    populated from `client.streams()`, and a "New stream…" dialog (`Ctrl+N`)
    that reads a picked CSV's header through `client.inspect_csv`, guesses
    each column's role, and lets the reader override any of them before
    creating the stream. Switching streams resets everything scoped to the
    old one -- the day screen's filter and selection, the trends screen's
    drill-down/pinned range/interval -- since a category path or date range
    from one stream means nothing in another.
    - Creating a stream from the dialog imports the same CSV immediately
      (reusing `MainWindow._import_path`, force-reimport offer included) and
      only *then* switches to it. Doing it in the other order was tried and
      is wrong: switching to a stream with no data yet anchors the day
      screen's window on today's date, and the refresh after importing
      reloads that same now-stale range instead of the one the data landed
      in -- caught by an offscreen run with a real `MainWindow`, not by the
      first pass of unit tests, and now pinned down by one that checks the
      day screen's actually-loaded window rather than just the client's data.
    - A real PySide6 gotcha hit while wiring the role dropdowns:
      `QComboBox.currentData()` silently coerces a `StrEnum` (`FieldRole`)
      back to a plain `str` on the way out of `QVariant`, which broke every
      `is FieldRole.X` check downstream despite the value comparing equal.
      Fixed by reading the role back by combo index into `list(FieldRole)`
      instead of trusting the round-trip.
  - [x] GUI rendering for a point-in-time stream. `lay_out_blocks` gives a
    zero-duration record a fixed-height marker centered on its instant
    (`LaidOutBlock.is_marker`) instead of a duration-sized block; the day
    timeline paints it as a small dot with its category and declared fields
    beside it, still hit-testable across the full row height. The inspector
    collapses started/ended/duration into one "at" row plus the declared
    fields, in order. The summary panel, the day header, and the trends
    axis/tooltip/legend all read `record_count` and format it as "N events"
    (`api.format.format_measure`, the one place that branches) instead of a
    duration, exactly the reuse the Phase 3 scope cut above described --
    no `Measure` type, no unstacked-line chart, since a count stacks and
    daily-averages the same way a duration does.
    - `core/trends.py` needed its own axis ceiling, `nice_count_ceiling`:
      `nice_seconds_ceiling` intentionally tops out at 24h (a day cannot hold
      more hours), which would have silently flattened an event stream's
      chart the moment its daily count passed 24.
    - Two rendering bugs, both caught by actually rendering the chart rather
      than by the first pass of tests (screenshotted via an offscreen
      `MainWindow`, same verification method used for the earlier adjustable-
      interval work): a marker exactly on a window's start boundary was
      dropped by the span-overlap check (correct for a span, wrong for a
      point -- half-open containment needed its own branch); and the y-axis
      gridline text rounded a small count ceiling's half-integer gridlines
      (0, 0.5, 1, 1.5, 2) to `round()`'s nearest, which collapsed 1.5 and 2.0
      to the same displayed "2" via round-half-to-even. Both are pinned down
      by tests now (`TestMarkers`, `TestAxisLabel`), not just the screenshot.
  - [ ] Deliberately deferred, logged rather than silently dropped: editing a
    stream's column mapping after creation (would invalidate every stored
    `payload_hash`); deleting a stream; a measure picker on the day screen (it
    shows records, not aggregates); de-overlapping markers that land close
    together on the timeline (mirrors the existing decision not to de-overlap
    interval blocks either); aggregating a text field (mode, distinct count).
- [x] Make efficiency updates to reduce input lag in GUI (input lag should be not be noticable by a human user).
  - [x] Found by reasoning about what each interaction actually recomputes, then
    measuring rather than guessing: both fixes below were confirmed with a
    standalone benchmark (not committed -- the fix and its regression tests are
    what stays) before being made, since "should be faster" is not the same
    claim as "is faster".
  - [x] `ui/timeline_view.py`, `Timeline.record_at` and `.paintEvent`: both
    scanned *every* block loaded in the (up to 61-day) window on every mouse
    move and every paint, not just the ones near the pointer or inside the
    dirty rect. Harmless for the sample Timecard data (a few hundred blocks),
    but a busy point-in-time stream -- exactly what the datastreams feature
    above just enabled (heart rate every few seconds, say) -- can load hundreds
    of thousands of markers into one window. Measured: hit-testing one such
    window the old way cost ~26ms per mouse-move event, an order of magnitude
    over a frame budget and enough to visibly freeze the pointer while
    hovering. Fixed by keeping `Timeline._blocks` sorted by `top` (true for
    free, since records already arrive ordered by start time -- see
    `_relayout`) and bisecting to the candidate range in
    `_visible_block_range` instead of scanning from the start every time.
    Measured after: ~0.003ms per call. Regression tests in `tests/test_ui.py`
    (`TestTimelineHitTesting`) build a synthetic 500-block window specifically
    because the sample fixture is too small to exercise the narrowed range at
    all.
  - [x] `ui/trends_view.py`, `TrendsChart`: `stack_bands` (and the per-column
    daily-average list it is built from) was recomputed from scratch in every
    `paintEvent`, including ones triggered by nothing more than the hover
    crosshair moving to a new column -- so dragging the pointer across the
    chart re-stacked the *entire* history on every pixel, not just the columns
    on screen. Measured: ~1ms per recompute for three years of daily columns
    across eight series, which is small once but adds up across the many
    repaints one continuous mouse move produces. Fixed by caching the result
    (`TrendsChart._bands`) and only rebuilding it in `set_view`/
    `set_normalised`, the two places that can actually change it. Regression
    test: `TestTrendsScreen.test_stacked_bands_stay_in_sync_with_the_view`
    checks the cache is rebuilt on both paths, since a caching bug here would
    be a chart silently showing a stale shape rather than a crash.
  - [x] Not changed, and why: `SummaryPanel`, `RecordInspector`, and
    `TrendsLegend` already only recompute on real state changes (a selection,
    a day, a visible-range change), not per frame, so there was nothing to
    cache there. `repository.py`'s range queries are all covered by an index
    matching their `WHERE` clause (see `storage/database.py`'s migrations),
    so a scrolled-triggered `extend_earlier`/`extend_later` load was not
    profiled further -- if a future stream's queries ever *are* the
    bottleneck, that is a database-side question, not a GUI one, and belongs
    in the "Known gaps" section below, not here.
- [x] Build correlation/inference engine. First slice: detect "when X
      increases in a period, Y tends to also increase," swept over many
      period lengths rather than one fixed one, as a `minmax correlate` CLI
      command. Requested by user 2026-08-08; planned and reviewed with the
      user before building (three clarifying rounds: multi-period sweep vs.
      one fixed period; durations/counts only vs. also numeric event fields;
      level correlation vs. period-over-period change; scipy vs. a hand-rolled
      t-distribution; CLI-only vs. also a GUI screen this pass).
  - [x] `storage/repository.py`: `daily_field_totals_by_category`, mirroring
    `daily_totals_by_category`. Closes a real gap found while planning:
    `field_rollup` had been write-only since migration `0004` -- nothing
    anywhere in the codebase had ever read it.
  - [x] `core/measures.py`: `Measure`/`MeasureKind`/`Aggregate`/`DailyValue`.
    This is `docs/architecture.md` decision D6's sketched-and-deliberately-
    unbuilt abstraction, finally built against a real second caller (this
    engine) rather than a guessed one -- see D7. `Measure.reduce` is the one
    place a multi-day value is ever formed, specifically so a mean is always
    value-weighted (`sum(value_sum) / sum(value_count)`) and never the
    mean-of-daily-means bug the schema's own comments warn about.
  - [x] `pyproject.toml`: `scipy` as a new **optional** `analysis` extra
    (`uv sync --extra analysis`), not a base dependency -- matching how
    PySide6 is already handled, and for the same reason (`minmax import`/
    `totals`/etc. must keep working on an install that never asked for
    correlation). `scipy-stubs` added to `dev` so `mypy --strict` still
    passes; `analysis/stats.py` is the only module that imports scipy, and
    even there only lazily inside `pearson()`.
  - [x] `analysis/series.py` (`SeriesLoader`): a `Measure` -> per-day values,
    reading each stream's rollups at most twice regardless of how many
    measures it yields (cached, not one query per measure).
  - [x] `analysis/sweep.py`: the period-length ladder (day, 2/3/5/10 days,
    week, fortnight, 3 weeks, month -- within `core/columns.py`'s
    `MAX_INTERVAL_COUNT`), `periodic_values` (bucketing + alignment), and
    `align`. Reuses `core/columns.py`'s calendar-anchored `columns_for`
    completely unchanged -- its anchoring guarantee (two series bucketed
    independently land on identical period boundaries) is the entire
    mechanism that makes pairing two series possible. `Interval(DAY, 7)` is
    deliberately excluded from the ladder (identical to `Interval(WEEK, 1)`
    by construction) via `deduplicate_intervals`, which compares actual
    boundaries rather than special-casing the one pair, so a user's own
    `--interval` list gets the same protection.
    - Two real bugs found by the integration test, not by review: (1) a
      sparse measure's own date range (not the stream's full active range)
      was originally used to compute columns, which clipped edge periods for
      any measure narrower than its stream's full history; (2) a `SUM`
      field-measure with zero readings in a whole period returned `0.0`
      instead of being treated as missing, since `Measure.reduce([])` is
      correctly `0.0` for `SUM` but the *period* had no data at all --
      fixed by skipping `reduce` entirely when nothing was recorded, not by
      changing `reduce`'s own (correct) semantics.
    - A **stream-blackout rule** (a period where the stream recorded nothing
      at all is *missing*, not zero) deliberately differs from the trends
      chart's choice for the identical kind of gap (an honest zero, so "I
      stopped tracking" isn't compressed into "these days were adjacent").
      Both are correct for what each is for: display vs. avoiding a spurious
      correlation manufactured from two unrelated trackers both being off
      at once. Documented in `docs/architecture.md` §7.1 precisely so this
      doesn't read as an unexplained inconsistency later.
  - [x] `analysis/catalog.py`: candidate `Measure`s at **grand total + top-level
    category only** (not every leaf -- matches the existing `top_level`
    grouping precedent and avoids correlating two mostly-zero vectors), and
    pair exclusion (a measure against its own stream's total; the same field
    under two aggregates). Two categories of the same *exclusive* stream are
    kept but carry a caveat (they mechanically compete for one time budget).
    `--max-series` truncation keeps grand totals first, then the most-used
    categories -- not an arbitrary/alphabetical subset.
  - [x] `analysis/stats.py`: `pearson` (rejects constant input and n<2 itself,
    on top of the sweep's own gates) and `benjamini_hochberg`, hand-rolled in
    ~10 lines rather than adding `statsmodels`.
  - [x] `analysis/correlate.py` + `models.py`: orchestrates enumerate -> load
    -> sweep -> gate (`n >= 8` aligned periods, >= 3 distinct values per
    side) -> test -> Benjamini-Hochberg (over *every* test performed, before
    any filtering) -> rank. A pair's `stability` (fraction of tested
    intervals agreeing in sign and clearing `--min-r`) is treated as more
    trustworthy than any single interval's q-value, since the sweep is
    structurally a multiple-testing machine and BH is reported, not
    enforced by default -- a strict q filter would return an empty table on
    the amount of data most streams have today, and this is hypothesis
    generation for one person, not a publication.
  - [x] `api/client.py`: `CorrelationRow`/`CorrelationsView` (flat,
    serializable) and `MinmaxClient.correlations()` on the protocol and
    `InProcessClient`. Skipped combinations are aggregated into a handful of
    human-readable notes (by interval), not one line per skipped pair.
  - [x] `cli.py`: `minmax correlate`, following `cmd_ui`'s exact precedent for
    an optional dependency -- checks `import scipy` up front and prints an
    install hint on failure, rather than a traceback from deep inside the
    engine. `--json` for machine-readable output.
  - [x] Verified against the real ~6-month `~/.minmax` database (read-only):
    found a real, plausible lead (several Timecard categories co-moving at
    3-week granularity, correctly flagged with the exclusive-stream caveat)
    and correctly, transparently reported a very new, sparse stream's pairs
    as untestable (its 2-day window doesn't overlap the older stream's range
    at all) rather than fabricating or crashing. No default needed tuning
    after seeing real output.
  - [x] Tests: hand-computed fixtures for `Measure.reduce` (the
    weighted-mean-vs-mean-of-means trap), Pearson's r and its p-value
    (checked against an independent closed-form, not just "scipy agrees with
    itself"), and Benjamini-Hochberg; the anchoring/alignment guarantee
    (two series over deliberately offset date ranges still land on the same
    period boundaries) as its own direct test; a synthetic multi-stream
    integration test built through the real `insert_intervals`/
    `insert_events` path; a layering test (`analysis/` never imports `api`
    or `ui`, no `SELECT` string outside `storage/`); CLI tests including the
    missing-scipy path.
  - [ ] Deliberately deferred, logged rather than silently dropped: any GUI
    screen (CLI-only this pass); lagged correlation (X this week vs. Y next
    week); period-over-period delta correlation (levels only, this pass);
    leaf-category drill-down (`--depth`, only grand total + top-level today);
    non-Pearson methods (Spearman, partial correlation, day-of-week/season
    deconfounding); a cached/derived results table (a full sweep is
    milliseconds against rollup-sized data; caching would buy nothing but a
    staleness bug); the "increase/decrease X -> advice" query interface and
    the DL forecasting model (both distant `AGENTS.md` end-goals this does
    not attempt); retrofitting `api/client.trends()`/`ui/` onto `Measure`
    (D7 -- the chart's existing count-reuse hack is untouched and still
    correct for what it does).

---

## Notes for whoever picks this up

Read [`docs/architecture.md`](docs/architecture.md) first — it has the layering
rules and the reasoning behind every decision, including what would make each
one worth revisiting.

On Timecard as a source:

- Its export format and design notes are documented in
  `/Users/xvade/dev/Misc/Timecard/README.md` — read the "Export format" and
  "Design notes & extensibility" sections before touching the importer.
- `timer_id` is the stable key across renames and folder moves; `timer_name` is
  only the path at the moment a run was recorded. Both are preserved.
- Only *completed* runs appear in a Timecard export; an in-progress run has no
  stop time and is absent.

Measured facts about the real data (338 rows, 2026-06-05 to 2026-08-05), each of
which drove a design choice:

- `duration_seconds` differs from `stopped_at - started_at` on 92 of 338 rows,
  always by exactly ±1s (sub-second truncation). Durations are therefore derived
  from the timestamps; the exported figure is kept only for auditing.
- 29 runs cross local midnight. They stay one row and are *split* when rolled up
  into daily aggregates.
- Zero overlaps, and 325 of 337 adjacent runs share an exact boundary. Hence the
  `exclusive` stream flag — but note this is a property of Timecard, not of
  interval streams generally (a calendar stream would overlap freely).
- One `timer_id` appears under two names (`social`, later `social/friends`),
  which is why category identity is the source id rather than the path.
- All rows carry a single UTC offset (-07:00) so far, so no DST transition
  appears in real data yet. The offset-change paths are covered by synthetic
  tests in `tests/test_timeref.py` — keep them.

Known gaps, deliberately left:

- `sample` streams (10^6-10^8 rows/year, needing a different physical layout
  than a per-row EAV table) have no storage yet. `interval` and `event` both
  do, as of migration `0004` — see `docs/architecture.md` §6.1. The
  correlation engine (`analysis/`) therefore cannot build a measure over one
  either -- `rebuild()` also silently no-ops for a `sample`-kind stream.
- `totals_by_category` reads observations directly (exact for arbitrary ranges);
  `totals_by_period` and `daily_totals_by_category` use rollups. If category
  totals ever get slow, that is the place to add a rollup-backed path.
- Trends columns are summed from the *daily* rollup at every interval, including
  a year. That is a few thousand rows for a decade, which is fine; if it ever is
  not, an hourly-style rollup keyed by week would be the fix, and the daily rows
  already hold everything it would need.
- Category colours are stored as palette *slots*. If the palette is ever
  re-stepped again, no migration is needed — but re-run the dataviz validator
  before changing `core/palette.py`, since the current steps are chosen to pass
  it in both light and dark.
