"""The application window and its screens.

Two screens today, and the structure assumes more: a timeline for low-level
detail, and a trends chart for the shape of things over time. Each screen is a
self-contained widget owning its own actions, registered with
``WidgetWithChildrenShortcut`` context so that ``←`` means "earlier day" on one
screen and "scroll back" on the other without the two fighting over the key.

Keyboard-first by design. Every action is a :class:`QAction` built from
:mod:`minmax.ui.shortcuts`, which is also what the Shortcuts dialog edits and
what the config file overrides -- so a binding cannot exist without being
documented, be documented without existing, or be remapped without the remap
taking effect.

Nothing here imports ``storage`` or ``ingest`` -- only
:class:`~minmax.api.MinmaxClient`, which is what makes the UI replaceable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QKeySequence,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from minmax.api import (
    Granularity,
    ImportOutcome,
    Interval,
    MinmaxClient,
    StreamKind,
    StreamSummary,
    TimelineRecord,
)
from minmax.api.format import format_duration, format_measure
from minmax.ui import shortcuts as bindings
from minmax.ui.dialogs import (
    IntervalDialog,
    NewStreamDialog,
    RangeDialog,
    ShortcutsDialog,
    prompt_for_prefix,
)
from minmax.ui.record_inspector import RecordInspector
from minmax.ui.shortcuts import ShortcutMap, load_shortcuts
from minmax.ui.summary_panel import SummaryPanel
from minmax.ui.timeline_view import TimelinePane
from minmax.ui.trends_legend import TrendsLegend
from minmax.ui.trends_view import TrendsChart

__all__ = ["DayScreen", "MainWindow", "TrendsScreen"]

#: Days loaded either side of the focus day when jumping somewhere new. Enough
#: that the reader can scroll a little before anything has to be fetched.
INITIAL_PAD = 3

#: Days added each time the reader approaches an edge.
CHUNK_DAYS = 7

#: Most days held in memory at once. Past this the far end is dropped, so
#: scrolling through a year does not accumulate a year of blocks.
MAX_WINDOW_DAYS = 61

#: How often a stream with auto-fetch on is re-imported from its remembered path.
AUTO_FETCH_INTERVAL_MS = 60_000

#: What ``cycle_interval`` walks through, in order. Only the single-unit
#: intervals: cycling every count up to the cap would take dozens of keystrokes
#: to cross, so anything else is set in the dialog instead.
INTERVALS = (
    Interval(Granularity.AUTO),
    Interval(Granularity.DAY),
    Interval(Granularity.WEEK),
    Interval(Granularity.MONTH),
)

#: How each single-unit interval is named in the title. Spelled out rather than
#: suffixed, because "dayly" is not a word.
GRANULARITY_WORDS = {
    Granularity.DAY: "daily",
    Granularity.WEEK: "weekly",
    Granularity.MONTH: "monthly",
}


def interval_words(interval: Interval) -> str:
    """How an interval is named in the title: ``weekly``, or ``every 2 weeks``.

    A count of one has an adverb and reads better with it. Anything else does
    not -- "fortnightly" is obscure and "3-daily" is not a word -- so those are
    named by the span they cover instead.
    """
    if interval.count == 1:
        return GRANULARITY_WORDS.get(interval.unit, str(interval.unit))
    return f"every {interval.label}"


#: The handful of bindings worth putting in the status bar, per screen. Listing
#: every one turns the bar into a wall of text that gets truncated anyway; these
#: are the ones a reader needs before they think to press ``?``.
STATUS_HINTS = {
    bindings.DAY: (
        "show_previous_day",
        "show_next_day",
        "prompt_for_date",
        "fit_day",
        "prompt_for_filter",
    ),
    bindings.TRENDS: (
        "scroll_back",
        "zoom_in",
        "fit_all",
        "toggle_normalised",
        "cycle_interval",
        "prompt_for_range",
    ),
}


class _ShortcutHost(QWidget):
    """A widget whose actions are rebuilt from a :class:`ShortcutMap`."""

    #: Which scope in the map this widget's actions come from.
    scope = bindings.DAY

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._actions: list[QAction] = []

    def apply_shortcuts(self, shortcuts: ShortcutMap, *, scoped: bool = True) -> None:
        """Replace every action with the bindings now in effect.

        Actions are torn down and rebuilt rather than mutated so that remapping
        can never leave a stale binding attached to a widget.
        """
        for action in self._actions:
            self.removeAction(action)
            action.deleteLater()
        self._actions = []

        for entry in shortcuts.scope(self.scope):
            action = QAction(entry.label, self)
            action.setShortcut(QKeySequence(entry.sequence))
            if scoped:
                action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(getattr(self, entry.name))
            self.addAction(action)
            self._actions.append(action)


class DayScreen(_ShortcutHost):
    """A continuously scrolling timeline, its totals, and a record inspector."""

    scope = bindings.DAY

    def __init__(
        self, client: MinmaxClient, stream_key: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._stream_key = stream_key
        self._bounds = client.bounds(stream_key)
        self._prefix: str | None = None
        self._focus_day = self._bounds[1] if self._bounds else date.today()
        self._top_day = self._focus_day
        self._loading = False
        self._fitted = False

        self._build_ui()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._reload_around(self._focus_day)

    def _build_ui(self) -> None:
        self._date_label = QLabel()
        date_font = QFont(self.font())
        date_font.setPointSizeF(date_font.pointSizeF() + 3)
        date_font.setBold(True)
        self._date_label.setFont(date_font)

        self._filter_label = QLabel()
        self._filter_label.setEnabled(False)

        self._total_label = QLabel()
        self._total_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 14, 10)
        header.addWidget(self._date_label)
        header.addSpacing(10)
        header.addWidget(self._filter_label)
        header.addStretch(1)
        header.addWidget(self._total_label)

        self.pane = TimelinePane(self)
        self.pane.topDayChanged.connect(self._on_top_day_changed)
        self.pane.needsEarlier.connect(self.extend_earlier)
        self.pane.needsLater.connect(self.extend_later)
        self.pane.recordSelected.connect(self._on_record_selected)

        self._summary = SummaryPanel(self)
        self.inspector = RecordInspector(self)
        self.inspector.closed.connect(self.clear_selection)

        side = QWidget()
        side_layout = QVBoxLayout()
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(6)
        side_layout.addWidget(self._summary, 1)
        side_layout.addWidget(self.inspector)
        side.setLayout(side_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.pane)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(splitter, 1)
        self.setLayout(layout)

    # -- loading ---------------------------------------------------------

    @property
    def day(self) -> date:
        """The local day at the top of the viewport."""
        return self._top_day

    @property
    def prefix(self) -> str | None:
        return self._prefix

    def _limits(self) -> tuple[date, date]:
        """How far *scrolling* may extend the window.

        Bounded by the data rather than open-ended: scrolling into a decade of
        empty days before tracking began is not navigation, it is a way to get
        lost. Today is included so an empty day still opens on the right date.

        Jumping somewhere explicitly is not bound by this -- "go to date" means
        what it says -- so the loaded window widens the limits, which is what
        lets a reader scroll back from wherever they landed.
        """
        today = date.today()
        low, high = self._bounds if self._bounds is not None else (today, today)
        high = max(high, today)

        window = self.pane.timeline.window_view
        if window is not None:
            low = min(low, window.first_day)
            high = max(high, window.last_day)
        return low, high

    def _load(self, first: date, last: date, *, anchor_epoch: int | None = None) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            window = self._client.window(self._stream_key, first, last, prefix=self._prefix)
            self.pane.set_window(window, anchor_epoch=anchor_epoch)
        finally:
            self._loading = False
        self._refresh_header()

    def _reload_around(self, day: date, *, align_to_day: bool = True) -> None:
        low, high = self._limits()
        # Padded, clamped to the data -- but never so clamped that the day the
        # reader asked for falls outside the window that gets loaded.
        first = min(day, max(low, day - timedelta(days=INITIAL_PAD)))
        last = max(day, min(high, day + timedelta(days=INITIAL_PAD)))
        self._focus_day = day
        self._load(first, last)

        window = self.pane.timeline.window_view
        if align_to_day and window is not None:
            summary = window.summary_for(day)
            if summary is not None:
                self.pane.scroll_to_epoch(summary.day_start_epoch, align_top=True)

        # Set last, so it wins over whatever the scrollbar reported on the way
        # through: before the window is on screen there is no viewport to scroll
        # in, and the header must still name the day that was asked for.
        self._top_day = day
        self.pane.assume_top_day(day)
        self._refresh_header()

    def extend_earlier(self) -> None:
        window = self.pane.timeline.window_view
        if self._loading or window is None:
            return
        first = max(self._limits()[0], window.first_day - timedelta(days=CHUNK_DAYS))
        if first == window.first_day:
            return
        last = window.last_day
        if (last - first).days + 1 > MAX_WINDOW_DAYS:
            last = first + timedelta(days=MAX_WINDOW_DAYS - 1)
        self._load(first, last, anchor_epoch=self.pane.epoch_at_top())

    def extend_later(self) -> None:
        window = self.pane.timeline.window_view
        if self._loading or window is None:
            return
        last = min(self._limits()[1], window.last_day + timedelta(days=CHUNK_DAYS))
        if last == window.last_day:
            return
        first = window.first_day
        if (last - first).days + 1 > MAX_WINDOW_DAYS:
            first = last - timedelta(days=MAX_WINDOW_DAYS - 1)
        self._load(first, last, anchor_epoch=self.pane.epoch_at_top())

    def refresh(self) -> None:
        """Reload the loaded window after data changed underneath it (an import).

        Re-fetches exactly the span already on screen, anchored so the reader's
        scroll position does not jump -- an import is additive, so there is
        nothing about the current view that needs to move.
        """
        self._bounds = self._client.bounds(self._stream_key)
        window = self.pane.timeline.window_view
        first = window.first_day if window is not None else self.day
        last = window.last_day if window is not None else self.day
        self._load(first, last, anchor_epoch=self.pane.epoch_at_top())

    def set_stream(self, stream_key: str) -> None:
        """Switch to a different stream, resetting the filter and selection
        (both are category paths in the old stream, meaningless in the new
        one) and reloading around its most recent day."""
        if stream_key == self._stream_key:
            return
        self._stream_key = stream_key
        self._prefix = None
        self.clear_selection()
        self._bounds = self._client.bounds(stream_key)
        self._focus_day = self._bounds[1] if self._bounds else date.today()
        self._reload_around(self._focus_day)

    # -- navigation ------------------------------------------------------

    def show_day(self, day: date, *, scroll: bool = True) -> None:
        """Put ``day`` at the top of the viewport, loading it if necessary."""
        window = self.pane.timeline.window_view

        # Already loaded: scrolling there keeps the surrounding days in place,
        # which is the whole point of a continuous timeline.
        if window is not None and window.covers(day, day):
            summary = window.summary_for(day)
            if summary is not None:
                self._focus_day = day
                if scroll:
                    self.pane.scroll_to_epoch(summary.day_start_epoch, align_top=True)
                self._on_top_day_changed(day)
                return

        self._reload_around(day)

    def show_previous_day(self) -> None:
        self.show_day(self.day - timedelta(days=1))

    def show_next_day(self) -> None:
        self.show_day(self.day + timedelta(days=1))

    def show_today(self) -> None:
        self.show_day(date.today())

    def show_first_day(self) -> None:
        if self._bounds:
            self.show_day(self._bounds[0])

    def show_last_day(self) -> None:
        if self._bounds:
            self.show_day(self._bounds[1])

    def prompt_for_date(self) -> None:
        text, accepted = QInputDialog.getText(
            self, "Go to date", "Date (YYYY-MM-DD):", text=self.day.isoformat()
        )
        if not accepted:
            return
        try:
            self.show_day(date.fromisoformat(text.strip()))
        except ValueError:
            QMessageBox.warning(self, "Go to date", f"{text!r} is not a YYYY-MM-DD date.")

    # -- filtering -------------------------------------------------------

    def prompt_for_filter(self) -> None:
        accepted, chosen = prompt_for_prefix(
            self, self._client.category_paths(self._stream_key), self._prefix
        )
        if accepted:
            self.set_prefix(chosen)

    def set_prefix(self, prefix: str | None) -> None:
        if prefix == self._prefix:
            return
        self._prefix = prefix
        self.clear_selection()
        self._reload_around(self.day)

    def clear_selection(self) -> None:
        self.pane.timeline.select(None)

    def clear_context(self) -> None:
        """Escape: dismiss the inspector first, then the filter.

        One key, two jobs, most-recent first -- so Escape always undoes the last
        thing the reader did rather than the more drastic of the two.
        """
        if self.inspector.record is not None:
            self.clear_selection()
        elif self._prefix is not None:
            self.set_prefix(None)

    # -- zoom ------------------------------------------------------------

    def zoom_in(self) -> None:
        self.pane.zoom_by(1.25)

    def zoom_out(self) -> None:
        self.pane.zoom_by(1 / 1.25)

    def reset_zoom(self) -> None:
        self.pane.reset_zoom()

    def fit_day(self) -> None:
        self.pane.fit_day()

    def showEvent(self, event: QShowEvent) -> None:
        """Fit a day the first time the screen is shown.

        Deferred to here because the viewport has no meaningful height until the
        window has been laid out.
        """
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            self.fit_day()
            self._scroll_somewhere_useful()

    # -- reacting --------------------------------------------------------

    def _on_top_day_changed(self, day: date) -> None:
        self._top_day = day
        self._refresh_header()

    def _on_record_selected(self, record: TimelineRecord | None) -> None:
        self.inspector.set_record(record)

    def _refresh_header(self) -> None:
        day = self.day
        # Built piecewise because the no-pad day directive is not portable.
        self._date_label.setText(f"{day:%A} {day.day} {day:%B %Y}")
        self._filter_label.setText(f"filtered to {self._prefix}" if self._prefix else "")

        window = self.pane.timeline.window_view
        summary = window.summary_for(day) if window is not None else None
        if summary is None or window is None:
            self._total_label.setText("")
            self._summary.set_slices([])
            return

        if not summary.tracked_seconds:
            self._total_label.setText("nothing tracked")
        elif window.stream_kind is StreamKind.EVENT:
            # tracked_seconds is a count for an event stream -- see DayView.stream_kind.
            self._total_label.setText(format_measure(summary.tracked_seconds, window.stream_kind))
        else:
            self._total_label.setText(format_duration(summary.tracked_seconds) + " tracked")
        self._summary.set_slices(list(summary.totals), stream_kind=window.stream_kind)

    def _scroll_somewhere_useful(self) -> None:
        """Open at the part of the day the reader cares about.

        Today opens at "now", the way Timecard's history does. Any other day
        opens at its own start, which the initial load has already done.
        """
        if self.day == date.today():
            self.pane.scroll_to_epoch(int(datetime.now().timestamp()))


class TrendsScreen(_ShortcutHost):
    """A side-scrolling stacked area over calendar columns, with per-group shares."""

    scope = bindings.TRENDS

    def __init__(
        self, client: MinmaxClient, stream_key: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._stream_key = stream_key
        #: Drill-down stack. Empty means top-level groups.
        self._focus: list[str] = []
        self._since: date | None = None
        self._until: date | None = None
        self._interval = Interval(Granularity.AUTO)

        self._build_ui()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.reload()

    def _build_ui(self) -> None:
        self._title_label = QLabel()
        title_font = QFont(self.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 3)
        title_font.setBold(True)
        self._title_label.setFont(title_font)

        self._range_label = QLabel()
        self._range_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 14, 10)
        header.addWidget(self._title_label)
        header.addStretch(1)
        header.addWidget(self._range_label)

        self.chart = TrendsChart(self)
        self.legend = TrendsLegend(self)
        self.chart.visibleRangeChanged.connect(self._on_range_changed)
        self.chart.seriesActivated.connect(self.push_focus)
        self.legend.seriesActivated.connect(self.push_focus)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.chart, 1)
        body.addWidget(self.legend)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addLayout(body, 1)
        self.setLayout(layout)

    # -- data ------------------------------------------------------------

    @property
    def prefix(self) -> str | None:
        return self._focus[-1] if self._focus else None

    @property
    def interval(self) -> Interval:
        return self._interval

    @property
    def range_selection(self) -> tuple[date | None, date | None]:
        return (self._since, self._until)

    def reload(self) -> None:
        self._view = self._client.trends(
            self._stream_key,
            since=self._since,
            until=self._until,
            prefix=self.prefix,
            interval=self._interval,
        )
        self.chart.set_view(self._view)
        self._title_label.setText(self._title())
        self._refresh_legend()

    def _title(self) -> str:
        where = f"Trends · {self.prefix}" if self.prefix else "Trends · all categories"
        shown = interval_words(self._view.interval)
        # Naming the resolved interval matters most under AUTO, where the reader
        # has not chosen it and would otherwise have to infer it.
        if self._interval.is_auto:
            return f"{where} · auto ({shown})"
        return f"{where} · {shown}"

    def push_focus(self, label: str) -> None:
        """Focus one group, splitting it into its children."""
        self._focus.append(label)
        self.reload()

    def pop_focus(self) -> None:
        if self._focus:
            self._focus.pop()
            self.reload()

    def set_stream(self, stream_key: str) -> None:
        """Switch to a different stream, resetting everything scoped to the
        old one -- the drill-down stack, a pinned range, and a non-default
        interval all describe the previous stream's data, not this one's."""
        if stream_key == self._stream_key:
            return
        self._stream_key = stream_key
        self._focus = []
        self._since = None
        self._until = None
        self._interval = Interval(Granularity.AUTO)
        self.reload()

    # -- range and granularity -------------------------------------------

    def prompt_for_range(self) -> None:
        bounds = self._client.bounds(self._stream_key)
        if bounds is None:
            return
        dialog = RangeDialog(self, bounds, self.range_selection)
        if dialog.exec():
            self.set_range(*dialog.selection())

    def set_range(self, since: date | None, until: date | None) -> None:
        if (since, until) == (self._since, self._until):
            return
        self._since, self._until = since, until
        self.reload()

    def clear_range(self) -> None:
        self.set_range(None, None)

    def cycle_interval(self) -> None:
        """Step to the next single-unit interval, or back to the first.

        An interval set in the dialog is not in the cycle, so ``D`` from a
        fortnight lands on the first entry rather than guessing which preset the
        reader meant. Predictable beats clever for a key pressed repeatedly.
        """
        index = INTERVALS.index(self._interval) + 1 if self._interval in INTERVALS else 0
        self.set_interval(INTERVALS[index % len(INTERVALS)])

    def prompt_for_interval(self) -> None:
        dialog = IntervalDialog(self, self._interval)
        if dialog.exec():
            self.set_interval(dialog.selection())

    def set_interval(self, interval: Interval | Granularity) -> None:
        chosen = Interval.coerce(interval)
        if chosen == self._interval:
            return
        self._interval = chosen
        self.reload()

    # -- interaction -----------------------------------------------------

    def scroll_back(self) -> None:
        self.chart.scroll_by_columns(-self._scroll_step())

    def scroll_forward(self) -> None:
        self.chart.scroll_by_columns(self._scroll_step())

    def _scroll_step(self) -> float:
        """Scroll by about a tenth of a screen, so context is never lost."""
        visible = max(1.0, self.chart.plot_width / self.chart.pixels_per_column)
        return max(1.0, visible / 10)

    def scroll_to_start(self) -> None:
        self.chart.scroll_to_start()

    def scroll_to_end(self) -> None:
        self.chart.scroll_to_end()

    def zoom_in(self) -> None:
        self.chart.zoom_by(1.3)

    def zoom_out(self) -> None:
        self.chart.zoom_by(1 / 1.3)

    def fit_all(self) -> None:
        self.chart.fit_all()

    def toggle_normalised(self) -> None:
        self.chart.toggle_normalised()
        self._refresh_legend()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_legend()

    def _on_range_changed(self, first: date, last: date) -> None:
        # The span on screen is always readable, whatever the tick interval on
        # the axis works out to.
        days = (last - first).days + 1
        pinned = " · pinned" if self._since or self._until else ""
        self._range_label.setText(
            # An en dash is the correct mark for a span of dates.
            f"{first.day} {first:%b %Y}  –  {last.day} {last:%b %Y}"  # noqa: RUF001
            f"   ({days} days{pinned})"
        )
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        self.legend.set_series(
            list(self._view.series),
            self.chart.window_shares(),
            self.chart.window_totals_seconds(),
            stream_kind=self._view.stream_kind,
        )


class MainWindow(QMainWindow):
    def __init__(
        self,
        client: MinmaxClient,
        stream_key: str = "timecard",
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._stream_key = stream_key
        self._shortcuts = load_shortcuts(config_path)
        self._global_actions: list[QAction] = []

        self.setWindowTitle("Minmax")
        self.resize(1100, 800)
        self.setAcceptDrops(True)

        self._day_screen = DayScreen(client, stream_key, self)
        self._trends_screen = TrendsScreen(client, stream_key, self)

        self._screens = QStackedWidget()
        self._screens.addWidget(self._day_screen)
        self._screens.addWidget(self._trends_screen)

        self._stream_picker = QComboBox()
        self._stream_picker.currentIndexChanged.connect(self._on_stream_picker_changed)

        self._auto_fetch_checkbox = QCheckBox("Auto-fetch every minute")
        self._auto_fetch_checkbox.setToolTip(
            "Re-import this stream's remembered CSV once a minute."
        )
        self._auto_fetch_checkbox.toggled.connect(self._on_auto_fetch_toggled)

        self._refresh_stream_picker()  # also syncs the checkbox to the current stream

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(12, 6, 12, 6)
        top_bar.addWidget(QLabel("Stream"))
        top_bar.addWidget(self._stream_picker)
        top_bar.addWidget(self._auto_fetch_checkbox)
        top_bar.addStretch(1)

        central = QWidget()
        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addLayout(top_bar)
        central_layout.addWidget(self._screens, 1)
        central.setLayout(central_layout)
        self.setCentralWidget(central)

        self._apply_shortcuts()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

        self._auto_fetch_timer = QTimer(self)
        self._auto_fetch_timer.setInterval(AUTO_FETCH_INTERVAL_MS)
        self._auto_fetch_timer.timeout.connect(self._run_auto_fetch)
        self._auto_fetch_timer.start()

        self.show_day_screen()
        if self._shortcuts.problems:
            self.statusBar().showMessage(
                "shortcut config: " + "; ".join(self._shortcuts.problems), 10_000
            )

    # -- shortcuts -------------------------------------------------------

    @property
    def shortcuts(self) -> ShortcutMap:
        return self._shortcuts

    def _apply_shortcuts(self) -> None:
        for action in self._global_actions:
            self.removeAction(action)
            action.deleteLater()
        self._global_actions = []

        for entry in self._shortcuts.scope(bindings.GLOBAL):
            action = QAction(entry.label, self)
            action.setShortcut(QKeySequence(entry.sequence))
            action.triggered.connect(getattr(self, entry.name))
            self.addAction(action)
            self._global_actions.append(action)

        self._day_screen.apply_shortcuts(self._shortcuts)
        self._trends_screen.apply_shortcuts(self._shortcuts)

    def set_shortcuts(self, shortcuts: ShortcutMap) -> None:
        self._shortcuts = shortcuts
        self._apply_shortcuts()

    def show_shortcuts(self) -> None:
        self.edit_shortcuts()

    def edit_shortcuts(self) -> None:
        dialog = ShortcutsDialog(self, self._shortcuts)
        if not dialog.exec():
            return
        self.set_shortcuts(dialog.result_map())
        try:
            self._shortcuts.save()
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                self,
                "Shortcuts",
                f"The new keys are active, but saving them failed:\n{error}",
            )
            return
        self.statusBar().showMessage(f"shortcuts saved to {self._shortcuts.path}", 5_000)

    # -- streams -----------------------------------------------------------

    def set_stream(self, stream_key: str) -> None:
        """Switch the whole window to a different stream.

        Both screens reset everything scoped to the previous stream (filter,
        drill-down, pinned range) rather than carrying it over, since a
        category path or a date range from one stream means nothing in
        another.
        """
        if stream_key == self._stream_key:
            return
        self._stream_key = stream_key
        self._day_screen.set_stream(stream_key)
        self._trends_screen.set_stream(stream_key)
        self._refresh_stream_picker()

    def prompt_for_stream(self) -> None:
        """The keyboard path to the picker: focus it and drop the list down,
        rather than a second dialog duplicating what is already on screen."""
        self._stream_picker.setFocus()
        self._stream_picker.showPopup()

    def prompt_for_new_stream(self) -> None:
        existing = [summary.key for summary in self._client.streams()]
        dialog = NewStreamDialog(self, existing_keys=existing, inspect=self._client.inspect_csv)
        if not dialog.exec():
            return
        key, name, fields, path = dialog.selection()
        try:
            self._client.create_stream(key, name, StreamKind.EVENT, fields=fields)
        except ValueError as error:
            QMessageBox.warning(self, "New stream", str(error))
            return

        self._refresh_stream_picker()
        # Imported into the new stream *before* switching to it: switching
        # first would compute the day screen's window from a stream with no
        # data yet, landing on today rather than wherever this data actually
        # is. Reusing _import_path still gets the new stream its usual
        # import report, force-reimport offer included.
        self._import_path(path, stream_key=key)
        self.set_stream(key)

    def _refresh_stream_picker(self) -> None:
        self._stream_picker.blockSignals(True)
        self._stream_picker.clear()
        current: StreamSummary | None = None
        for summary in self._client.streams():
            self._stream_picker.addItem(summary.name, summary.key)
            if summary.key == self._stream_key:
                current = summary
        index = self._stream_picker.findData(self._stream_key)
        if index >= 0:
            self._stream_picker.setCurrentIndex(index)
        self._stream_picker.blockSignals(False)

        self._auto_fetch_checkbox.blockSignals(True)
        self._auto_fetch_checkbox.setChecked(bool(current and current.watch_enabled))
        self._auto_fetch_checkbox.blockSignals(False)

    def _on_stream_picker_changed(self, index: int) -> None:
        key = self._stream_picker.itemData(index)
        if key and key != self._stream_key:
            self.set_stream(key)

    # -- auto-fetch --------------------------------------------------------
    # Opt-in per stream: not every imported file is a live feed worth polling,
    # and a stream that has never been (re-)imported since this shipped has no
    # remembered path to resume from regardless (only a filename was ever kept
    # in import history before now, not the full original path). Turning the
    # checkbox on is therefore also how a stream gets its first remembered
    # path, when it does not have one yet.

    def _current_stream_summary(self) -> StreamSummary | None:
        return next((s for s in self._client.streams() if s.key == self._stream_key), None)

    def _on_auto_fetch_toggled(self, checked: bool) -> None:
        key = self._stream_key
        summary = self._current_stream_summary()
        name = summary.name if summary is not None else key

        if not checked:
            self._client.set_auto_fetch(key, enabled=False)
            return

        if summary is not None and summary.watch_path:
            self._client.set_auto_fetch(key, enabled=True)
            self._auto_fetch_one(key, name, Path(summary.watch_path))
            return

        path_str, _ = QFileDialog.getOpenFileName(
            self, "Choose the CSV to auto-fetch", "", "CSV files (*.csv);;All files (*)"
        )
        if not path_str:
            # Nothing was persisted, so the checkbox must go back to matching
            # reality rather than staying checked with nothing behind it.
            self._refresh_auto_fetch_checkbox()
            return

        path = Path(path_str)
        self._client.set_auto_fetch(key, enabled=True, path=path)
        # Tried once immediately, rather than waiting up to a minute to learn
        # a freshly chosen file was itself unusable.
        self._auto_fetch_one(key, name, path)

    def _refresh_auto_fetch_checkbox(self) -> None:
        summary = self._current_stream_summary()
        self._auto_fetch_checkbox.blockSignals(True)
        self._auto_fetch_checkbox.setChecked(bool(summary and summary.watch_enabled))
        self._auto_fetch_checkbox.blockSignals(False)

    def _run_auto_fetch(self) -> None:
        """Re-import every stream with auto-fetch on, from its remembered path.

        Skipped while a modal dialog is already open (the New Stream dialog,
        the reimport-offer prompt, this feature's own recovery prompt on a
        previous stream in the same tick, ...) so a background tick cannot pop
        a file picker on top of whatever the reader is doing right now -- the
        next tick, a minute later, tries again.
        """
        if QApplication.activeModalWidget() is not None:
            return
        for summary in self._client.streams():
            if summary.watch_enabled and summary.watch_path:
                self._auto_fetch_one(summary.key, summary.name, Path(summary.watch_path))

    def _auto_fetch_one(self, key: str, name: str, path: Path) -> None:
        outcome = self._try_auto_fetch(key, path)
        if outcome.ok:
            self._on_auto_fetch_success(key, outcome)
            return

        # Found by hand (missing file) or by merging (an OverlapError or a
        # column-shape mismatch) -- either way nothing was stored, and per the
        # chosen UX this asks exactly once rather than nagging every minute.
        replacement = self._prompt_for_replacement(name, outcome.error or "unknown error")
        if replacement is None:
            self._pause_auto_fetch(key, name, outcome.error or "unknown error")
            return

        retry = self._try_auto_fetch(key, replacement)
        if not retry.ok:
            self._pause_auto_fetch(key, name, retry.error or "unknown error")
            QMessageBox.warning(
                self,
                "Auto-fetch",
                f"That didn't work either:\n{retry.error}\n\n"
                f"Auto-fetch for {name!r} has been paused.",
            )
            return

        self._client.set_auto_fetch(key, enabled=True, path=replacement)
        self._on_auto_fetch_success(key, retry)
        self.statusBar().showMessage(f"auto-fetch for {name} now uses {replacement.name}", 5_000)

    def _try_auto_fetch(self, key: str, path: Path) -> ImportOutcome:
        try:
            return self._client.import_file(path, stream_key=key)
        except Exception as error:
            # This runs unattended on a timer -- an exception here must be
            # reported through the same recovery path as a normal import
            # failure, never left to crash the tick (or the app).
            return ImportOutcome(source_name=path.name, stream_key=key, error=str(error))

    def _on_auto_fetch_success(self, key: str, outcome: ImportOutcome) -> None:
        if not outcome.inserted:
            return
        if key == self._stream_key:
            self._day_screen.refresh()
            self._trends_screen.reload()
        self.statusBar().showMessage(
            f"auto-fetched {outcome.source_name}: {outcome.inserted} new", 5_000
        )

    def _pause_auto_fetch(self, key: str, name: str, reason: str) -> None:
        self._client.set_auto_fetch(key, enabled=False)
        self.statusBar().showMessage(f"auto-fetch paused for {name}: {reason}", 8_000)
        if key == self._stream_key:
            self._refresh_auto_fetch_checkbox()

    def _prompt_for_replacement(self, name: str, reason: str) -> Path | None:
        QMessageBox.warning(
            self,
            "Auto-fetch",
            f"Auto-fetch for {name!r} failed:\n{reason}\n\n"
            "Choose a new CSV to continue, or Cancel to pause it.",
        )
        path_str, _ = QFileDialog.getOpenFileName(
            self, f"Choose a new CSV for {name}", "", "CSV files (*.csv);;All files (*)"
        )
        return Path(path_str) if path_str else None

    # -- importing ---------------------------------------------------------
    # A file can arrive two ways -- the dialog below, or a drop onto the
    # window (dragEnterEvent/dropEvent, further down) -- and both end up here
    # so there is exactly one place that talks to the client and refreshes
    # the screens afterwards.

    def prompt_for_import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import", "", "CSV files (*.csv);;All files (*)"
        )
        if path_str:
            self._import_path(Path(path_str))

    def _import_path(
        self, path: Path, *, force: bool = False, stream_key: str | None = None
    ) -> None:
        """Import ``path``, into ``stream_key`` if given, else the current stream.

        A caller creating a stream and importing straight into it passes
        ``stream_key`` explicitly and switches *after* this returns
        (:meth:`prompt_for_new_stream` does exactly that) -- switching first
        would compute the day screen's bounds and load a window before the
        data exists, and refreshing afterwards would just reload that same,
        now-stale range rather than the one the new data actually landed in.
        """
        target = stream_key if stream_key is not None else self._stream_key
        try:
            outcome = self._client.import_file(path, stream_key=target, force=force)
        except RuntimeError as error:
            QMessageBox.warning(self, "Import", str(error))
            return

        if not outcome.ok:
            QMessageBox.warning(self, "Import", f"Could not import {path.name}:\n{outcome.error}")
            return

        if outcome.skipped:
            self._offer_reimport(path, outcome, stream_key=target)
            return

        self._report_import(path, outcome)
        if outcome.inserted and target == self._stream_key:
            self._day_screen.refresh()
            self._trends_screen.reload()

    def _offer_reimport(self, path: Path, outcome: ImportOutcome, *, stream_key: str) -> None:
        detail = f"\n{outcome.notes[0]}" if outcome.notes else ""
        again = QMessageBox.question(
            self,
            "Import",
            f"{path.name} was already imported; nothing new to add.{detail}\n\n"
            "Import it again anyway?",
        )
        if again == QMessageBox.StandardButton.Yes:
            self._import_path(path, force=True, stream_key=stream_key)

    def _report_import(self, path: Path, outcome: ImportOutcome) -> None:
        lines = [
            f"{outcome.inserted} new, {outcome.duplicates} already present, "
            f"{outcome.rejected} rejected ({outcome.rows_read} rows read)",
            *outcome.notes,
        ]
        self.statusBar().showMessage(f"imported {path.name}: {lines[0]}", 8_000)
        QMessageBox.information(self, "Import", f"Imported {path.name}:\n" + "\n".join(lines))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        for path in paths:
            self._import_path(path)

    # -- screens ---------------------------------------------------------

    def show_day_screen(self) -> None:
        self._screens.setCurrentWidget(self._day_screen)
        self._day_screen.setFocus()
        self.statusBar().showMessage(self._hint(bindings.DAY))

    def show_trends_screen(self) -> None:
        self._screens.setCurrentWidget(self._trends_screen)
        self._trends_screen.setFocus()
        self.statusBar().showMessage(self._hint(bindings.TRENDS))

    def _hint(self, scope: str) -> str:
        """A one-line reminder, built from the bindings actually in effect.

        Only the highlights: the full list is one keystroke away, and a status
        bar naming thirteen keys is read as decoration rather than as help.
        """
        wanted = STATUS_HINTS.get(scope, ())
        by_name = {entry.name: entry for entry in self._shortcuts.scope(scope)}
        parts = [
            f"{by_name[name].sequence} {by_name[name].label.lower().rstrip('…')}"
            for name in wanted
            if name in by_name
        ]
        help_key = self._shortcuts.sequence_for(bindings.GLOBAL, "show_shortcuts")
        return "  ·  ".join([*parts, f"{help_key} shortcuts"])

    @property
    def current_screen(self) -> QWidget:
        return self._screens.currentWidget()

    @property
    def day_screen(self) -> DayScreen:
        return self._day_screen

    @property
    def trends_screen(self) -> TrendsScreen:
        return self._trends_screen

    # -- day-screen delegation -------------------------------------------
    # The day screen's controls stay reachable from the window so that callers
    # need not know which screen owns what.

    @property
    def day(self) -> date:
        return self._day_screen.day

    def show_day(self, day: date, *, scroll: bool = True) -> None:
        self._day_screen.show_day(day, scroll=scroll)

    def show_previous_day(self) -> None:
        self._day_screen.show_previous_day()

    def show_next_day(self) -> None:
        self._day_screen.show_next_day()

    def show_today(self) -> None:
        self._day_screen.show_today()

    def show_first_day(self) -> None:
        self._day_screen.show_first_day()

    def show_last_day(self) -> None:
        self._day_screen.show_last_day()

    def prompt_for_date(self) -> None:
        self._day_screen.prompt_for_date()

    def zoom_in(self) -> None:
        self._day_screen.zoom_in()

    def zoom_out(self) -> None:
        self._day_screen.zoom_out()

    def reset_zoom(self) -> None:
        self._day_screen.reset_zoom()

    def fit_day(self) -> None:
        self._day_screen.fit_day()
