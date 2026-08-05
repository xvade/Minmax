# Minmax

A personal data hub. Minmax imports time-bound data about your life from many
sources, stores it durably, lets you look at it, and — eventually — tells you
which things in your life move together.

The long-term goals are in [`AGENTS.md`](AGENTS.md); the code structure that
gets there is in [`docs/architecture.md`](docs/architecture.md); what is being
worked on next is in [`TODO.md`](TODO.md).

## Status

| Capability | State |
|---|---|
| Import, store, and retrieve timetracking data | **working** |
| Command-line access to all of it | **working** |
| Desktop UI: day timeline and totals | **working** |
| Desktop UI: trends chart over days | **working** |
| Correlation engine | not started |
| Streams beyond timetracking | not started |

Today Minmax ingests [Timecard](/Users/xvade/dev/Misc/Timecard) CSV exports.
Because the storage layer models *stream shapes* rather than sources, adding
another interval-shaped stream later means writing a parser and nothing else.

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra ui                        # create the environment (with the desktop app)
uv run minmax import ~/Downloads/Timecard-20260805-112422.csv
uv run minmax ui                          # open the day timeline
```

Leave off `--extra ui` for a headless install; every command except `minmax ui`
works without it.

Data lives in `~/.minmax` by default — a SQLite database plus an archive of
every file ever imported. Override with `$MINMAX_HOME` or `--home`.

To back Minmax up, copy that directory. That is the whole procedure, and it is
one of the reasons SQLite was chosen.

## Commands

### `minmax import <file>`

Imports a source file. Archives the raw bytes first, parses, then stores
everything in a single transaction.

```
$ minmax import Timecard-20260805-112422.csv
imported Timecard-20260805-112422.csv into timecard: 338 new, 0 already present, 0 rejected (338 rows read)
  archived: ~/.minmax/archive/timecard/20260805-2097b06a5ee7-Timecard-20260805-112422.csv
  note: 92 of 338 rows had an exported duration differing from the timestamps (-1s x45, +1s x47); durations were derived from the timestamps
```

- `--stream KEY` — which importer to use (default `timecard`).
- `--force` — re-read a file whose content was already imported.

**Importing is idempotent.** Re-importing the same export inserts nothing, and
overlapping exports contribute only their new rows. You can export from Timecard
as often as you like and import all of them without thinking about it.

**A malformed row is dropped, never guessed at.** The row and the reason are
reported; the rest of the file still imports.

### `minmax status`

Streams, how much they cover, and recent imports.

```
timecard  (interval, exclusive)
  338 records over 2026-06-05 -> 2026-08-05
  35 categories, 683h 02m tracked
  import 1 [ok] Timecard-20260805-112422.csv: 338 new, 0 dup, 0 rejected
```

### `minmax totals`

Time per category over a range, largest first.

```
$ minmax totals --prefix media
timecard  2026-06-05 -> 2026-08-05  under media
    15h 55m   41.9%  media/anime  (13)
     6h 07m   16.1%  media/webtoon  (12)
         5h   13.2%  media/gaming/chess  (13)
    ...
    37h 57m  total
```

- `--since` / `--until` — local `YYYY-MM-DD` bounds (default: everything).
- `--prefix` — restrict to a category and its descendants. Matching is
  component-wise, so `media` does not also match `mediation`.
- `--limit` — rows to show.

Totals are computed from the observations themselves and clipped exactly to the
requested range, so an arbitrary window is exact rather than rounded to whole
days.

### `minmax day <YYYY-MM-DD>`

Every record on one local day, in order.

```
$ minmax day 2026-08-04
timecard  2026-08-04
  <22:12-06:27    8h 14m  sleep/night
   06:27-06:44       16m  maintain/bootup
   06:44-07:23       39m  research/lit review
   ...
   23:46-00:24>      38m  social/friends
```

A run that crosses midnight stays a single record and is marked `<` or `>`
rather than being split or clipped — the same way Timecard draws it as one
block with the day boundary through it.

### `minmax periods`

Totals per local hour or day, read from the precomputed rollup table.

- `--period day|hour`, `--since`, `--until`, `--prefix`.

### `minmax ui`

Opens the desktop app on the most recent day with data.

The main view is a day timeline modelled on Timecard's history screen: blocks
sized to their real duration so they tile without gaps, coloured by their
top-level category so a whole branch reads as one colour, labelled once tall
enough and carrying their length once there is room for both. A run that crosses
midnight is marked with a dashed edge rather than clipped silently, so an
overnight sleep run reads as continuing rather than as having ended at 00:00.
Beside it, the day's categories with their share of the day.

The window opens with the whole day fitted to it. Zooming in is for inspecting
short runs — at maximum zoom a one-minute run is comfortably selectable.

Press `2` for the **trends** screen: a side-scrolling stacked area, one column
per day, stacked by top-level category. It answers "what has the shape of my
time been" rather than "what happened on Tuesday".

- The header always names the **first and last day on screen** and how many days
  that is, and both endpoints are labelled on the axis whatever the tick spacing
  works out to.
- The legend gives each group's **share of the visible days**, updating live as
  you scroll and zoom — so the percentages always describe what you are looking
  at, not the whole dataset.
- `N` switches between hours per day and each day's composition as a percentage.
- **Double-click a band or a legend row to focus that group**, splitting it into
  its own sub-categories (`media` becomes `media/anime`, `media/gaming`, …).
  `Esc` backs out. This is how per-timer detail is reached without ever putting
  more than eight colours on screen at once.
- Days with no tracking stay empty rather than being skipped, so a gap in your
  history reads as a gap.

**Keyboard shortcuts** (press `?` in the app for this list):

| Key | Action |
|---|---|
| `←` / `→` (or `H` / `L`) | Previous / next day |
| `T` | Today |
| `Home` / `End` | Earliest / latest day with data |
| `G` | Go to a date |
| `+` / `-` | Zoom in / out |
| `F` | Fit the whole day to the window |
| `0` | Reset zoom to the default |
| `1` / `2` | Day view / trends view |
| `?` | Show the shortcut list |

On the trends screen `←`/`→` scroll through time, `+`/`-` change how many days
are shown, `F` fits the entire recorded span, `N` toggles the percentage view,
and `Esc` leaves a focused group. The same key can mean different things on each
screen because actions are scoped to the screen that owns them.

`Ctrl`/`Cmd` + scroll zooms around the pointer rather than the centre.

The app follows the system light/dark appearance, since everything but the
category colours is drawn from semantic system colours.

- `--stream KEY` — which stream to show (default `timecard`).

### `minmax revert <import-id>`

Undoes one import completely: every observation it produced is removed and
aggregates are rebuilt from what remains. Other imports are untouched.

Use this when an export contained edited run times that conflict with what is
already stored — Minmax refuses such an import rather than storing both versions
(see *Conflicting edits* below).

### `minmax rebuild-rollups`

Recomputes all aggregates from the observations. A repair tool; not needed in
normal use.

## How the data is stored

A single SQLite file, opened with `foreign_keys=ON`, WAL journalling, and
`synchronous=FULL`. Writes are transactional, so a failed import stores nothing.

Four properties are worth knowing about, because they shape what you can ask:

**Timestamps keep their UTC offset.** Every instant is stored as a UTC epoch
second *plus* the offset in effect there. Almost every question you would ask a
life tracker is about local wall-clock time, and UTC alone silently reassigns
records to the wrong local day across a DST change or a flight.

**Categories are paths, and identity is the source's id.** `media/gaming/chess`
is stored whole and the hierarchy derived from it. A timer that gets renamed or
moved keeps its identity, so its history stays in one piece — the first real
export contains exactly this case, one timer recorded under both `social` and,
later, `social/friends`. Each record also remembers the path it was recorded
under.

**Every row knows where it came from.** Each observation carries the id of the
import that produced it, and each import records the SHA-256 of its source file.
That is what makes `revert` exact, and what makes a re-downloaded export
recognizable as the same data under a different filename.

**Raw files are kept forever.** Imports are archived byte-for-byte before
parsing. If a parser turns out to be wrong, the fix is to revert and re-import,
never to hand-edit the database.

**Category colours are assigned once and stored.** Colour belongs to a
top-level group, as in Timecard. What is stored is a *palette slot*, not a hex
value, so each slot can carry its own step for light and dark surfaces and the
palette can be re-stepped without a data migration. Assignments are persisted
rather than derived from the name, so a group's colour never shifts because a
new group appeared, and two groups never collide while the palette lasts.

The eight palette steps are validated rather than eyeballed — they clear the
lightness band, chroma floor, colour-vision-deficiency separation, and
normal-vision floor against both surfaces. That is also why charts cap at eight
categorical hues and fold the rest into a neutral "Other": past eight, adjacent
hues stop being reliably distinguishable.

See [`docs/architecture.md`](docs/architecture.md) for the schema, the layering
rules, and the reasoning behind each decision.

## Timecard specifics

Two quirks of real Timecard exports are handled explicitly, both measured
against the first export rather than assumed:

**Durations are derived, not trusted.** `duration_seconds` disagrees with
`stopped_at - started_at` on about a quarter of rows, always by exactly one
second, because the export truncates sub-second components independently.
Minmax computes duration from the timestamps so that a day's totals reconcile
with the time the timeline visibly covers, keeps the exported figure for
auditing, and rejects any row where the two differ by more than two seconds —
that is corruption, not rounding.

**Conflicting edits are refused, not merged.** Timecard lets you edit a
recorded run's times. If a later export contains a run that overlaps a stored
one without being identical to it, Minmax aborts the whole import and reports
the conflict, because a stream where one timer runs at a time cannot hold both
versions without corrupting every total covering that moment. Resolve it by
reverting the earlier import and re-importing.

## Development

```sh
uv run --extra ui pytest   # tests (UI tests skip without the extra)
uv run ruff check .        # lint
uv run ruff format .       # format
uv run --extra ui mypy     # types (strict)
```

The test suite covers the domain rules, the schema's constraints (including that
it *rejects* bad data), the parser against golden and malformed files, the
import pipeline's idempotency, timeline layout arithmetic, and the client
facade. UI tests run headless via Qt's offscreen platform, so they need no
display. Where a real Timecard export is present on this machine,
`tests/test_real_export.py` runs the pipeline against it too, with shape-based
assertions so that re-exporting cannot break the build.

## Layout

```
src/minmax/
├── core/       domain model, timeline and chart arithmetic — pure, no I/O, no Qt
├── storage/    SQLite schema, migrations, repository, rollups
├── ingest/     importers and the shared import pipeline
├── api/        the facade the UI talks to
├── ui/         PySide6 desktop app
└── cli.py      command-line entry point
docs/           architecture and design decisions
tests/          unit, integration, UI, and real-data tests
```

Dependencies point one direction only: `core → storage → ingest → api → ui`. The
UI imports `api` and nothing else, which is what makes it replaceable without
touching the backend — a test enforces it. PySide6 is the only runtime
dependency and it is optional; the standard library covers import, storage, and
retrieval entirely.
