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

- [ ] Complete the second goal in `AGENTS.md` "Minmax will include a convenient user interface for viewing timetracking data". Use Timecard as a model for how to display timetracking data.
  - [x] Define the `MinmaxClient` protocol operations the UI needs; implement `InProcessClient`.
  - [x] PySide6 app shell: window, keyboard-shortcut registry, light/dark following system appearance.
  - [x] Day-timeline view modelled on Timecard's history screen — hour grid, blocks sized to real duration, coloured by top-level category, labels shown when tall enough, midnight-crossing runs marked rather than clipped.
  - [x] Zoom of hour density, anchored under the pointer on Ctrl/Cmd+scroll; fit-day on open.
  - [x] Summary panel: totals by category for the shown day, with share bars.
  - [x] Keyboard-first navigation (day movement, jump to date, zoom, fit, shortcut help).
  - [x] Persisted per-group colours (`group_color` table, migration 0002) so a category's colour is stable and distinct.
  - [x] Tests: timeline arithmetic, the client facade, and headless UI wiring; a test that fails if `ui/` ever imports the backend.
  - [x] Document the UI in `README.md`.
  - [ ] Category filter (`/` to restrict the timeline and totals to a path prefix). The backend already supports prefixes end to end — `Repository.totals_by_category(path_prefix=...)` and `MinmaxClient.range_totals(prefix=...)` — so this is UI work only.
  - [ ] Scroll past the day boundary to load adjacent days, the way Timecard's history scrolls continuously, instead of stepping day by day.
  - [x] Trends screen: side-scrolling stacked area over days, zoomable, with the visible date span and each group's share of it always on screen.
  - [x] Re-step the category palette on validated colours (light and dark), storing palette slots rather than hex so it can change without a data migration (migration 0003).
  - [x] Drill-down: focus a group on the trends screen to split it into its sub-categories.
  - [ ] Click a block to inspect it (full path, exact times, which import it came from).
  - [ ] Remappable shortcuts persisted to a config file; the `DAY_SHORTCUTS` / `TRENDS_SHORTCUTS` / `GLOBAL_SHORTCUTS` tables in `ui/main_window.py` are already the single source they would be read into.
  - [ ] Trends: a range picker, so the chart can be pinned to a fixed window rather than always covering all history.
  - [ ] Trends: weekly and monthly aggregation, so multi-year spans do not rely on one column per day.
  - [ ] Confirm dark mode on a real display. It has been verified offscreen with a forced dark palette (chrome follows, category colours hold up), but never seen under the actual macOS dark appearance.

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

- Only `interval` observations have storage. `event_record` / `sample_record`
  get added by a migration when the first such stream lands.
- `analysis/` does not exist yet — no correlation engine. Everything it will
  need is already in the daily rollups.
- `totals_by_category` reads observations directly (exact for arbitrary ranges);
  `totals_by_period` and `daily_totals_by_category` use rollups. If category
  totals ever get slow, that is the place to add a rollup-backed path.
- The trends chart plots one column per day. Past a few years that is a lot of
  columns for the pixels available; weekly/monthly aggregation is the fix, and
  the rollup table already stores what it would need.
- Category colours are stored as palette *slots*. If the palette is ever
  re-stepped again, no migration is needed — but re-run the dataviz validator
  before changing `core/palette.py`, since the current steps are chosen to pass
  it in both light and dark.
