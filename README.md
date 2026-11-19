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
| Desktop UI: continuous timeline, totals, and record inspection | **working** |
| Desktop UI: trends chart by day, week, or month | **working** |
| User-defined point-in-time streams (create, import, view) | **working** |
| Auto-fetch: re-import a stream from a remembered path once a minute | **working** |
| Correlation engine: level correlations across many period lengths (CLI + desktop screen) | **working** |

Minmax ingests [Timecard](/Users/xvade/dev/Misc/Timecard) CSV exports out of
the box, and any other CSV you point it at: a period-of-time stream needs a
hand-written parser (Timecard is the only one so far, since the storage layer
models *stream shapes* rather than sources), but a point-in-time stream —
something that happened at an instant, with any payload from nothing to
several numbers and a label — is declared entirely from the GUI or the CLI,
no code required. See [Defining your own datastreams](#defining-your-own-datastreams).

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra ui --extra analysis       # create the environment (desktop app + correlate)
uv run minmax import ~/Downloads/Timecard-20260805-112422.csv
uv run minmax ui                          # open the day timeline
```

Both extras are optional and independent: leave off `--extra ui` for a
headless install, or `--extra analysis` if you don't need `minmax correlate`.
Every other command works without either.

Data lives in `~/.minmax` by default — a SQLite database, an archive of every
file ever imported, and `shortcuts.json` if you have remapped any keys. Override
with `$MINMAX_HOME` or `--home`.

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

- `--stream KEY` — which stream to import into (default `timecard`). `timecard`
  is built in; any other key must already exist — see `minmax stream add`.
- `--force` — re-read a file whose content was already imported.

**Importing is idempotent.** Re-importing the same export inserts nothing, and
overlapping exports contribute only their new rows. You can export from Timecard
as often as you like and import all of them without thinking about it.

**A malformed row is dropped, never guessed at.** The row and the reason are
reported; the rest of the file still imports.

### `minmax stream add`

Declares a new stream and its column mapping, guessed from a sample CSV's
header and overridden where the guess is wrong — either kind, no code:

- `--kind event` (the default) — one instant per row, with any payload from
  nothing to several numbers and a label.
- `--kind interval` — a start and an end per row (a second Timecard-shaped
  source: a calendar export, a sleep log, a session log).

```
$ minmax stream add --key heart_rate --name "Heart Rate" --from readings.csv
created stream 'heart_rate' (event) with fields:
  measured_at          -> at      [measured_at]
  bpm                  -> number  [bpm]  (bpm)
import into it with: minmax import <file> --stream heart_rate

$ minmax stream add --key sessions --name Sessions --kind interval \
    --from sessions.csv --role project=category
created stream 'sessions' (interval) with fields:
  session_start        -> start  [session_start]
  session_end          -> end  [session_end]
  project              -> category  [project]
  note                 -> ignored  [note]
import into it with: minmax import <file> --stream sessions
```

- `--key` / `--name` — the stream's stable slug and display name.
- `--kind event|interval` — which shape the source is.
- `--from <csv>` — a sample file to read the header and a few rows from.
- `--role COLUMN=ROLE` — override a guessed role; repeatable. Legal roles
  depend on the kind: `event` uses `at`, `category`, `source_id`, `number`,
  `text`; `interval` uses `start`, `end`, `category`, `source_id`. Either
  kind accepts `ignored` for a column that exists in the source but should
  not be stored.
- `--exclusive` — `interval` only: reject an import that would create an
  overlapping span (Timecard's own single-tasking-timer guarantee, not the
  norm for most interval sources — a calendar overlaps freely, so this
  defaults off).

The timestamp column(s) are guessed with confidence — every sampled value
must parse as an ISO 8601 instant with a UTC offset. For `event`, the first
such column becomes `at`; everything else defaults to `number` only if every
sampled value parses as one, else `text`. For `interval`, the first two such
columns (in header order) become `start` and `end`; everything else defaults
to `ignored`, since an interval stream has no payload to put a `number`/`text`
column in. A column identifying the row (`category`, `source_id`) is never
guessed for either kind, since nothing about a column's *values* says "this
is an identifier" — override it explicitly.

A stream with no `category` column files every record under one implicit
category named after the stream itself. An interval stream's duration is
always derived from its start and end, never trusted from a source column —
declare a source duration column `ignored` rather than mapping it anywhere.

### `minmax stream delete`

Permanently deletes a stream and everything recorded in it — categories,
records, values, rollups, import history.

```
$ minmax stream delete --key heart_rate
deleted stream 'heart_rate' (event)
```

There is no interactive confirmation and no undo — `--key` is the whole
command, and typing it is the confirmation, the same as `minmax revert`.
Archived raw files under the stream's key stay on disk (Minmax's archive is
never modified or overwritten, only added to), so re-declaring a stream under
the same key can still re-import the same source without hunting it down
again.

### `minmax streams`

Lists every declared stream and, for a point-in-time one, its fields.

```
$ minmax streams
timecard  (interval, exclusive)  2026-06-05 -> 2026-08-05
heart_rate  (event)  2026-08-01 -> 2026-08-05
    number     BPM (bpm)
```

### `minmax view`

A view is a saved restriction of every other command (and every desktop
screen) to the times a category was active in an interval stream — "only my
heart rate readings while Timecard's `work` category was running." Unlike
`--prefix`, which narrows which categories of *one* stream are shown, a view
narrows *when*, across every stream.

```
$ minmax view add --key work --name Work --stream timecard --category work
created view 'work': timecard/work
$ minmax view list
work  (timecard/work)  'Work'
$ minmax totals --stream heart_rate --view work
heart_rate  2026-06-05 -> 2026-08-05  view work
    12h 04m   100.0%  heart_rate  (1,340)
    12h 04m  total
```

`--category` is optional — omitted, the view means any activity at all in
`--stream`. `minmax view delete --key work` removes it permanently, no
confirmation beyond typing the key, the same as `stream delete`.

A view masks time, it does not compress it: real dates and clock times stay
where they are, and the excluded stretches are simply empty — a day the view
was never active on is still a real, navigable day. `--view KEY` is
available on `totals`, `day`, `periods` (day granularity only), `correlate`,
and `ui`.

### `minmax status`

Streams, how much they cover, and recent imports.

```
timecard  (interval, exclusive)
  338 records over 2026-06-05 -> 2026-08-05
  35 categories, 683h 02m tracked
  import 1 [ok] Timecard-20260805-112422.csv: 338 new, 0 dup, 0 rejected
```

### `minmax totals`

Time per category over a range, largest first. **Interval streams only** — a
point-in-time stream has no duration to total; use `periods` instead.

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
- `--view KEY` — restrict to the times a saved view was active; see
  `minmax view`.

Totals are computed from the observations themselves and clipped exactly to the
requested range, so an arbitrary window is exact rather than rounded to whole
days.

### `minmax day <YYYY-MM-DD>`

Every record on one local day, in order. **Interval streams only**, for the
same reason as `totals`.

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
block with the day boundary through it. Under `--view`, a record straddling
a gap in the view's mask is split into the pieces that survive it, each
printed on its own line.

- `--view KEY` — restrict to the times a saved view was active.

### `minmax periods`

Totals per local hour or day, read from the precomputed rollup table. Works
for both stream kinds: an interval stream's bar is a duration, a point-in-time
stream's is a plain count (its rollup rows carry no duration to show).

- `--period day|hour`, `--since`, `--until`, `--prefix`.
- `--view KEY` — restrict to the times a saved view was active. Only
  `--period day` supports this today; `--period hour` with `--view` is
  refused.

### `minmax correlate`

Sweeps for "when X increases in a period, does Y tend to also increase" —
across every declared stream, tested at a ladder of period lengths (a day,
a few days, a week, a fortnight, a month) rather than one fixed one, since
the interesting period for any given pair isn't known ahead of time.

```
$ minmax correlate --per-category
2026-02-07 -> 2026-08-05  (16 series, 440 combinations tested)

  +0.93* q=0.03  n=8   3 weeks    maintain duration  ~  sleep duration
      consistent at 62% of the periods tested
      'timecard' is an exclusive stream (at most one category is ever active at
      once), so these two categories' durations mechanically compete for one
      shared time budget.

  not tested:
    day: 52 combination(s) skipped (best case had 2 usable period(s))
    ...

  q is a Benjamini-Hochberg false-discovery rate over every combination tested.
  These are leads to test, not findings.
```

Each row is one pair's *best*-fitting period length (`--all-intervals` shows
every length tested instead); `*` marks `q <= 0.10`. Read `n` before the `r`:
eight aligned periods is the floor before a correlation means anything at
all, and the tool will tell you plainly when a period length simply doesn't
have enough data yet rather than pretend otherwise.

By default only a stream's grand total is tested. `--per-category` also
tests each stream's top-level categories against everything else — this is
usually what you want once you're past a first look, since "gaming" and
"sleep" moving together is a more specific, more actionable claim than
"timecard as a whole" and "sleep" moving together. A point-in-time stream's
declared numeric fields (heart rate, say) are tested by their daily mean and
their daily sum — the same two the desktop trends chart's metric dropdown
offers, plus the count every point-in-time stream already gets. `--no-field-sum`
drops the sum if you only want the mean.

A pair is not filtered out for a low `q` by default — with the amount of
data most streams have today, a strict filter would return nothing on the
very first run. `--min-r` (default `0.5`) is what actually narrows the list;
`--max-q` is there if you want the stricter view. **These are leads to test
against your own judgment, not findings** — the tool does not know about
shared trends, day-of-week effects, or which of two correlated things (if
either) causes the other.

- `--stream KEY` (repeatable), `--since`, `--until` — restrict what's compared and over what range.
- `--interval UNIT[:COUNT][,...]` — test specific period lengths (e.g. `week:2,month`) instead of the built-in ladder.
- `--min-periods` (default `8`), `--min-r` (default `0.5`), `--max-q`, `--top` (default `20`, `0` for no limit).
- `--per-category`, `--no-fields`, `--no-field-sum`, `--all-intervals`, `--max-series` (default `40`).
- `--view KEY` — restrict every series to the times a saved view was active.
- `--json` for machine-readable output.

Needs the `analysis` extra (`uv sync --extra analysis`) — scipy is optional,
like the desktop UI's PySide6, so installing Minmax to only import and query
data does not pull it in. Every other command works without it.

### `minmax ui`

Opens the desktop app on the most recent day with data.

#### Defining your own datastreams

A picker above the day and trends screens (`Ctrl`+`K`) lists every declared
stream; switching resets whatever was specific to the one you left (a filter,
a drill-down, a pinned range) since none of it means anything in the new one.

`Ctrl`+`N` opens "New stream…": pick a CSV, choose its kind (event or
interval), and Minmax reads the header and guesses a role for each column —
confidently for the timestamp column(s), more cautiously (number vs. text)
for an event stream's other columns. Every guess is an ordinary dropdown you
can change before anything is created; a category or source-id role is never
guessed, since nothing about a column's *values* says "this identifies the
row" rather than "this is text." An interval stream also gets an "Exclusive"
checkbox (off by default) for a single-tasking source like Timecard that
should reject overlapping spans. Creating the stream imports the same file
immediately and switches to it, so the loop is one dialog: pick a file, check
the roles, see it on screen.

This is the same thing `minmax stream add` does from the terminal; see that
section for the CLI form, its `--kind`/`--exclusive` flags, and `--role`
overrides.

`Ctrl`+`Backspace` deletes whichever stream the picker currently shows —
permanently, with a confirmation dialog first, and no undo. Switch to the one
you want gone before pressing it. Disabled when it's the only stream left,
since every screen but Correlations needs a current stream to show. Archived
raw files stay on disk either way; see `minmax stream delete`.

#### Importing

A new export does not need the CLI: press `Ctrl`+`I` (or "Import…") for a file
dialog, or drag a file onto the window. Either way it goes through the same
archiving, provenance, and idempotency as `minmax import` — re-importing an
export you already have offers to force it rather than silently doing nothing,
and a file the importer does not recognise (or one that overlaps stored data)
is reported rather than crashing the app. New rows refresh whatever is on
screen; a duplicate-only re-import leaves it untouched.

**Imports run in the background.** The window stays responsive while a large
file is read and stored: a status-bar indicator ("importing apm.csv…") shows
while one is in flight, and the result is reported when it lands. Imports run
one at a time, in the order you asked for them. Closing the window waits for the
one in progress to finish rather than abandoning it halfway.

#### Auto-fetch

The "Auto-fetch every minute" checkbox beside the stream picker re-imports the
current stream from a remembered CSV path once a minute — for a source that
keeps growing (a phone's location log appended to continuously, say), rather
than a one-off export. It is opt-in per stream: switching it on for a stream
with no remembered path yet asks you to pick the file, tries it immediately
(so a bad choice is reported now, not up to a minute later), and remembers it
from then on. Re-importing a growing file merges rather than duplicates, and
backs it up, the same way any import does — see
[How the data is stored](#how-the-data-is-stored) — so nothing new was built
for either; the timer just calls the same path a manual import would, with one
difference: an auto-fetch archives a raw copy of the file at most once every six
hours per stream, where your own imports always archive. (A growing file differs
every minute, so archiving each fetch would write a full copy a minute.) The data
is still merged every fetch; only the extra backup copy is spaced out. Each
fetch runs in the background like any other import; if one is still running
when the next minute comes round, that stream is skipped for the tick rather
than queued behind it.

If the file goes missing, or the merge itself fails (say it now conflicts
with something already stored), you are asked once for a replacement; cancel
and that stream's auto-fetch pauses rather than asking again every minute.
This is a desktop-app feature only — it has no CLI equivalent today.

#### Views

A second picker, "View", sits beside the stream picker and lists "All data"
plus every saved view (`minmax view`). Choosing one restricts the day
timeline, the trends chart, and the correlations sweep all at once to the
times the view's category was active — heart rate readings only while
Timecard's `work` was running, say. `V` opens the picker, `Ctrl`+`Shift`+`N`
declares a new view (name it, pick its source interval stream, and
optionally a category — omitted, the view means any activity in that
stream), and `Ctrl`+`Shift`+`Backspace` permanently deletes whichever view
is currently active.

Unlike the stream picker, a view is **not** reset by switching streams — it
restricts *when*, which means the same thing regardless of which stream is
on screen. On the day timeline, the stretches outside the view's active
windows are shaded, faintly: those moments really did happen, they just have
no data, so the shading is a reading aid rather than a claim that time was
removed. A record straddling the edge of a view's window is drawn as the
piece that survives it, same as `minmax day --view` does from the terminal.

Deleting a stream that a view was built from deletes the view along with it;
if that view was the active one, the window falls back to "All data" rather
than being left pointing at something that no longer exists.

#### The timeline

Modelled on Timecard's history screen: blocks sized to their real duration so
they tile without gaps, coloured by their top-level category so a whole branch
reads as one colour, labelled once tall enough and carrying their length once
there is room for both. Beside it, the day's categories with their share of the
day.

**The timeline scrolls continuously through midnight**, rather than stepping
one day at a time. Days are ruled and named where they begin, and more are
loaded as you approach either end. A run that crosses midnight is therefore
drawn as the single run it is, with the day boundary through it — the same way
Timecard draws it. The dashed continuation edge still exists, but it now means
"there is more of this run than has been loaded yet", which is only ever true at
the two outer edges.

The window opens with the whole day fitted to it. Zooming in is for inspecting
short runs — at maximum zoom a one-minute run is comfortably selectable.

- **`/` filters to a category and everything under it.** Matching is
  component-wise, so `media` does not also catch `mediation`. The header says
  what is being filtered; `Esc` clears it.
- **Click a block to inspect it.** The panel shows both category paths (they
  differ when a timer has been renamed), the exact start and end with seconds
  and UTC offset, and which import the row came from — by id *and* filename, so
  a suspicious block is traceable without opening the database.

A point-in-time stream draws each record as a small dot at its instant instead
of a block, with its category and any declared fields (a number and its unit,
a text value) written beside it. Clicking one shows a single "at" reading in
the inspector rather than a started/ended/duration triple, since there is only
an instant to report.

#### The trends screen

Press `2`: a side-scrolling stacked area, one column per period, stacked by
top-level category. It answers "what has the shape of my time been" rather than
"what happened on Tuesday".

- **A column covers whatever interval you want.** `D` cycles the common ones —
  automatic, daily, weekly, monthly — and `Shift`+`D` opens a dialog for the
  rest: five days, a fortnight, a quarter, a year. Automatic is the default and
  picks from the span, so opening ten years of history does not require knowing
  that monthly columns exist.
- **Columns are anchored to the calendar, not to your range.** Weeks start on
  Monday, months on the 1st, quarters in January; a fortnight or a five-day
  column counts from a fixed epoch. A given Tuesday therefore lands in the same
  column whatever range you asked for, and two charts of the same interval are
  comparable. The one at each end is clipped to your range rather than reaching
  outside it, and its tooltip names the days it really covers.
- **The y axis is always hours per day**, whatever the interval, because a column
  is plotted as its daily average rather than its total. Widening the interval
  therefore changes the resolution and nothing else; if totals were plotted, a
  weekly view would rescale the axis by seven and the same data would look like a
  different life.
- **`R` pins the chart to a fixed range** (last 30/90/180 days, last year, or
  custom dates); `Shift`+`R` returns to all history. Presets count back from the
  last day with data rather than from today, so "last 30 days" means thirty days
  of data even if an import is overdue.
- The header always names the **first and last day on screen** and how many days
  that is, and both endpoints are labelled on the axis whatever the tick spacing
  works out to.
- The legend gives each group's **share of the visible columns**, updating live
  as you scroll and zoom — so the percentages always describe what you are
  looking at, not the whole dataset.
- `N` switches between hours per day and each column's composition as a
  percentage.
- **Double-click a band or a legend row to focus that group**, splitting it into
  its own sub-categories (`media` becomes `media/anime`, `media/gaming`, …).
  `Esc` backs out. This is how per-timer detail is reached without ever putting
  more than eight colours on screen at once.
- Periods with no tracking stay empty rather than being skipped, so a gap in
  your history reads as a gap.
- **Click a legend swatch (or right-click a row) to choose that series' colour:**
  *Auto*, one of the eight palette hues, or *Custom…* for any colour. The chart
  repaints at once, and the choice is remembered. A top-level group's colour is
  shared with the day timeline and its totals; a sub-category colour picked
  while focused applies to that sub-category on the trends screen. "Other"
  has no colour to choose — it is a fold of several categories, not one.

  **Auto steps around what you picked.** An auto series keeps the colour it was
  born with unless a colour you chose *on screen at the same time* is too close
  to it; only then does it move, to whichever unused hue is visibly furthest from
  everything else shown. Colours you picked for series that are not on screen
  constrain nothing. Choosing Auto again gives the series its original colour
  back. Because there are only eight validated hues, a chart showing eight
  series plus a couple of hand-picked colours near palette hues cannot keep
  everything apart — focus a group to see fewer at once.

For a point-in-time stream the same chart plots **how many, per day**, instead
of hours per day — stacked and daily-averaged exactly the same way, since a
count sums over a set the same way a duration does. The axis, the tooltip, and
the legend all read as counts ("14 events") rather than durations; nothing
about the interactions above changes.

**A stream that declares a numeric field** (heart rate, say) gets a dropdown
next to the title — Count, `<field>` total, `<field>` average — and `M` cycles
through it. Count and total stack like any other point-in-time chart; average
does not (two categories' averages don't add up to anything), so it draws one
line per category instead, with a real gap wherever a category recorded
nothing that day rather than a misleading dip to zero. `N`'s percentage view
only applies to a stacking metric and does nothing on an average. Switching
streams always resets the choice back to Count.

#### The correlations screen

Press `3`: the same sweep `minmax correlate` runs on the command line (see
that section above for what the numbers mean), as a sortable table instead of
text. Unlike the day and trends screens, this one is **not** scoped to
whichever stream the picker has selected — comparing across streams is the
entire point, so it always considers every declared stream.

- **Per-category** also tests each stream's top-level categories, not just
  its grand total — usually what you want once you're past a first look.
- **min |r|** narrows the table to pairs at least that strongly related.
- Click a column header to sort by it — `r`, `q`, `n`, or **Stability**
  (what share of the tested period lengths agree with this row; blank means
  all of them did) are all sorted numerically, not alphabetically.
- Hover a row for its caveats, if it has any (e.g. two categories competing
  for one exclusive stream's time budget).
- `R` re-runs the sweep on demand. It otherwise only runs the first time you
  open this screen, and again if it's already open when new data arrives —
  not on every launch or every stream switch, since a cross-stream sweep is
  heavier than either other screen's queries.
- Needs the `analysis` extra (`uv sync --extra analysis`); without it, the
  screen says so instead of crashing.

Same honesty as the CLI: **these are leads to test, not findings** — nothing
here knows about shared trends, day-of-week effects, or which of two
correlated things (if either) causes the other.

#### Keyboard shortcuts

Press `?` (or `Ctrl`+`,`) for the full list, which is also where you change
them.

| Key | Day view | Trends view |
|---|---|---|
| `←` / `→` | Previous / next day | Scroll earlier / later |
| `Home` / `End` | Earliest / latest day with data | Start / end of the range |
| `+` / `-` | Zoom in / out | Fewer / more columns |
| `F` | Fit the whole day to the window | Fit the whole range |
| `Esc` | Close the inspector, then clear the filter | Leave the focused group |
| `T` | Today | — |
| `G` | Go to a date | — |
| `0` | Reset zoom to the default | — |
| `/` | Filter to a category | — |
| `N` | — | Percentage view |
| `M` | — | Cycle the metric (numeric-field streams only) |
| `D` / `Shift`+`D` | — | Cycle the interval / set any interval |
| `R` / `Shift`+`R` | — | Pin / unpin the range |

`1`, `2`, and `3` switch screens, `?` shows the list, `Ctrl`+`I` imports a
file, `Ctrl`+`K` switches streams, and `Ctrl`+`N` declares a new one — all
anywhere. `V` switches views, `Ctrl`+`Shift`+`N` declares a new one, and
`Ctrl`+`Shift`+`Backspace` deletes whichever is active; see
[Views](#views). The same key means different things on each screen because
actions are scoped to the screen that owns them: `R` re-runs the sweep on the
correlations screen, the same key that pins the trends range.

`Ctrl`/`Cmd` + scroll zooms around the pointer rather than the centre.

**Every binding is remappable.** The Shortcuts dialog edits them and saves the
ones you changed to `~/.minmax/shortcuts.json`, keyed by action rather than by
key, so a file written today still means the same thing after a default
changes. A conflicting, unknown, or unparseable binding is reported and skipped
rather than applied — losing one customization is a far better failure than
losing every shortcut in the app.

The app follows the system light/dark appearance, since everything but the
category colours is drawn from semantic system colours.

- `--stream KEY` — which stream to show (default `timecard`).
- `--view KEY` — open with this saved view already active.

### `minmax revert <import-id>`

Undoes one import completely: every observation it produced is removed and
aggregates are rebuilt from what remains. Other imports are untouched.

Use this when an export contained edited run times that conflict with what is
already stored — Minmax refuses such an import rather than storing both versions
(see *Conflicting edits* below).

### `minmax archive prune`

Deletes archived raw copies that a later copy of the same file fully contains.

```
$ minmax archive prune --dry-run
  actions_per_minute: 74008 redundant copies
  ...
79123 redundant copies, 95.21 GB to free; 0 kept because a later copy does not contain them, 0 already missing
dry run: nothing deleted
```

Archived files are otherwise never deleted. This command exists for the archive
that auto-fetch used to grow by a full copy per minute (now capped at one per six
hours, so a fresh install never needs it). It is deliberately conservative: a copy
is deleted only if it is recorded in the database, lives under the archive
directory, a later copy of the same file from the same UTC day exists, **and its
bytes are a prefix of that later copy** — so everything it held is still held. A
file that was rewritten rather than appended to fails that test and is kept. The
newest copy of each file on each day is never deleted. The data in the database is
not touched; the pruned import runs just stop pointing at a file.

- `--dry-run` — print what would be deleted and delete nothing. Run this first.

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
never to hand-edit the database. A file that grows between imports hashes
differently each time, so archiving every auto-fetch would write a complete new
copy every minute -- it reached 99 GB and filled a disk. Auto-fetch therefore
archives a stream at most once every six hours (your own imports always archive),
and `minmax archive prune` removes the redundant copies an older version left
behind. Between copies, the live source file and the database are the record.

**A point-in-time stream's payload is columns, not a blob.** Its declared
fields (`stream_field`) and their values (`event_value`) are ordinary rows
with a `CHECK` the database enforces, not a JSON column trusted to be
well-formed — the same posture as everywhere else in the schema, that bad
data is rejected rather than accepted and sorted out later. The natural key
that makes a re-import a no-op includes a hash of a row's own field values,
not just its instant, so two distinct readings landing in the same second
(two GPS fixes, say) both survive.

**Category colours are assigned once and stored.** Colour belongs to a
top-level group, as in Timecard. What is stored is a *palette slot*, not a hex
value, so each slot can carry its own step for light and dark surfaces and the
palette can be re-stepped without a data migration. Assignments are persisted
rather than derived from the name, so a group's colour never shifts because a
new group appeared, and two groups never collide while the palette lasts.

**Colours you choose are stored separately from the automatic ones.** A hand-picked
colour lives in `series_colour` (migration `0008`), keyed by stream and category
path, as either a palette slot or a hex; no row means Auto. Keeping it apart is
what makes "back to Auto" a delete rather than a lost assignment. The auto
assignment that works around your picks is a pure function in `core/palette.py`
(`resolve_auto`), measured in OKLab — the same colour space the palette was
validated in — and checked under simulated colour-vision deficiency as well.

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
├── core/       domain model, timeline/chart arithmetic, calendar grouping,
│               palette — all pure, no I/O, no Qt
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
