"""The application window and its screens.

Two screens today, and the structure assumes more: a day timeline for low-level
detail, and a trends chart for the shape of things over time. Each screen is a
self-contained widget owning its own actions, registered with
``WidgetWithChildrenShortcut`` context so that ``←`` means "previous day" on one
screen and "scroll back" on the other without the two fighting over the key.

Keyboard-first by design. Every action is a :class:`QAction` driven from a table
that also feeds the Shortcuts dialog, so a binding cannot exist without being
documented or be documented without existing.

Nothing here imports ``storage`` or ``ingest`` -- only
:class:`~minmax.api.MinmaxClient`, which is what makes the UI replaceable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
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

from minmax.api import DayView, MinmaxClient
from minmax.api.format import format_duration
from minmax.ui.summary_panel import SummaryPanel
from minmax.ui.timeline_view import TimelinePane
from minmax.ui.trends_legend import TrendsLegend
from minmax.ui.trends_view import TrendsChart

__all__ = [
    "DAY_SHORTCUTS",
    "GLOBAL_SHORTCUTS",
    "TRENDS_SHORTCUTS",
    "DayScreen",
    "MainWindow",
    "TrendsScreen",
]

#: (label, shortcut, method name, help text). One table per screen so the
#: dialog, the actions, and the status hint can never drift apart.
DAY_SHORTCUTS: tuple[tuple[str, str, str, str], ...] = (
    ("Previous day", "Left", "show_previous_day", "Move back one day"),
    ("Next day", "Right", "show_next_day", "Move forward one day"),
    ("Today", "T", "show_today", "Jump to today"),
    ("First day", "Home", "show_first_day", "Jump to the earliest day with data"),
    ("Last day", "End", "show_last_day", "Jump to the latest day with data"),
    ("Go to date…", "G", "prompt_for_date", "Jump to a specific date"),
    ("Zoom in", "+", "zoom_in", "Show more detail per hour"),
    ("Zoom out", "-", "zoom_out", "Fit more of the day on screen"),
    ("Fit day", "F", "fit_day", "Zoom so the whole day fits the window"),
    ("Reset zoom", "0", "reset_zoom", "Return to the default hour density"),
)

TRENDS_SHORTCUTS: tuple[tuple[str, str, str, str], ...] = (
    ("Scroll back", "Left", "scroll_back", "Move the window earlier"),
    ("Scroll forward", "Right", "scroll_forward", "Move the window later"),
    ("Earliest", "Home", "scroll_to_start", "Jump to the start of history"),
    ("Latest", "End", "scroll_to_end", "Jump to the most recent day"),
    ("Show fewer days", "+", "zoom_in", "Widen each day"),
    ("Show more days", "-", "zoom_out", "Fit more days on screen"),
    ("Fit all", "F", "fit_all", "Show the entire recorded span at once"),
    ("Percentage view", "N", "toggle_normalised", "Switch between hours and share of each day"),
    ("Back out", "Escape", "pop_focus", "Leave the focused group"),
)

GLOBAL_SHORTCUTS: tuple[tuple[str, str, str, str], ...] = (
    ("Day view", "1", "show_day_screen", "The day timeline"),
    ("Trends view", "2", "show_trends_screen", "The stacked area over days"),
    ("Shortcuts", "?", "show_shortcuts", "Show this list"),
)


def _bind(owner: QWidget, table: tuple[tuple[str, str, str, str], ...], *, scoped: bool) -> None:
    """Attach one action per table row to ``owner``.

    Screen actions are scoped so they fire only while their screen has focus;
    global ones stay active everywhere.
    """
    for label, shortcut, method, _ in table:
        action = QAction(label, owner)
        action.setShortcut(QKeySequence(shortcut))
        if scoped:
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(getattr(owner, method))
        owner.addAction(action)


class DayScreen(QWidget):
    """One local day: its timeline and its category totals."""

    def __init__(
        self, client: MinmaxClient, stream_key: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._stream_key = stream_key
        self._bounds = client.bounds(stream_key)
        self._day = self._bounds[1] if self._bounds else date.today()
        self._fitted = False

        self._build_ui()
        _bind(self, DAY_SHORTCUTS, scoped=True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.refresh(scroll=True)

    def _build_ui(self) -> None:
        self._date_label = QLabel()
        date_font = QFont(self.font())
        date_font.setPointSizeF(date_font.pointSizeF() + 3)
        date_font.setBold(True)
        self._date_label.setFont(date_font)

        self._total_label = QLabel()
        self._total_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 14, 10)
        header.addWidget(self._date_label)
        header.addStretch(1)
        header.addWidget(self._total_label)

        self._timeline = TimelinePane(self)
        self._summary = SummaryPanel(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._timeline)
        splitter.addWidget(self._summary)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(splitter, 1)
        self.setLayout(layout)

    # -- navigation ------------------------------------------------------

    @property
    def day(self) -> date:
        return self._day

    def show_day(self, day: date, *, scroll: bool = True) -> None:
        self._day = day
        self.refresh(scroll=scroll)

    def show_previous_day(self) -> None:
        self.show_day(self._day - timedelta(days=1))

    def show_next_day(self) -> None:
        self.show_day(self._day + timedelta(days=1))

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
            self, "Go to date", "Date (YYYY-MM-DD):", text=self._day.isoformat()
        )
        if not accepted:
            return
        try:
            self.show_day(date.fromisoformat(text.strip()))
        except ValueError:
            QMessageBox.warning(self, "Go to date", f"{text!r} is not a YYYY-MM-DD date.")

    # -- zoom ------------------------------------------------------------

    def zoom_in(self) -> None:
        self._timeline.zoom_by(1.25)

    def zoom_out(self) -> None:
        self._timeline.zoom_by(1 / 1.25)

    def reset_zoom(self) -> None:
        self._timeline.reset_zoom()

    def fit_day(self) -> None:
        self._timeline.fit_day()

    def showEvent(self, event: QShowEvent) -> None:
        """Fit the day the first time the screen is shown.

        Deferred to here because the viewport has no meaningful height until the
        window has been laid out.
        """
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            self.fit_day()

    # -- refresh ---------------------------------------------------------

    def refresh(self, *, scroll: bool = False) -> None:
        view = self._client.day(self._stream_key, self._day)

        # Built piecewise because the no-pad day directive is not portable.
        self._date_label.setText(f"{self._day:%A} {self._day.day} {self._day:%B %Y}")
        self._total_label.setText(
            format_duration(view.tracked_seconds) + " tracked"
            if view.tracked_seconds
            else "nothing tracked"
        )

        self._timeline.set_day(view)
        self._summary.set_slices(view.totals)

        if scroll:
            self._scroll_somewhere_useful(view)

    def _scroll_somewhere_useful(self, view: DayView) -> None:
        """Open at the part of the day the user cares about.

        Today opens at "now", the way Timecard's history does. Any other day
        opens at its first record, so a day that starts at 09:00 does not
        present eight hours of empty grid.
        """
        if self._day == date.today():
            self._timeline.scroll_to_epoch(int(datetime.now().timestamp()))
        elif view.records:
            self._timeline.scroll_to_epoch(view.records[0].start_epoch)


class TrendsScreen(QWidget):
    """A side-scrolling stacked area over days, with per-group shares."""

    def __init__(
        self, client: MinmaxClient, stream_key: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._stream_key = stream_key
        #: Drill-down stack. Empty means top-level groups.
        self._focus: list[str] = []

        self._build_ui()
        _bind(self, TRENDS_SHORTCUTS, scoped=True)
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

    def reload(self) -> None:
        self._view = self._client.trends(self._stream_key, prefix=self.prefix)
        self.chart.set_view(self._view)
        self._title_label.setText(
            f"Trends · {self.prefix}" if self.prefix else "Trends · all categories"
        )
        self._refresh_legend()

    def push_focus(self, label: str) -> None:
        """Focus one group, splitting it into its children."""
        self._focus.append(label)
        self.reload()

    def pop_focus(self) -> None:
        if self._focus:
            self._focus.pop()
            self.reload()

    # -- interaction -----------------------------------------------------

    def scroll_back(self) -> None:
        self.chart.scroll_by_days(-self._scroll_step())

    def scroll_forward(self) -> None:
        self.chart.scroll_by_days(self._scroll_step())

    def _scroll_step(self) -> float:
        """Scroll by about a tenth of a screen, so context is never lost."""
        visible = max(1.0, self.chart.plot_width / self.chart.pixels_per_day)
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
        self._range_label.setText(
            # An en dash is the correct mark for a span of dates.
            f"{first.day} {first:%b %Y}  –  {last.day} {last:%b %Y}   ({days} days)"  # noqa: RUF001
        )
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        self.legend.set_series(
            list(self._view.series),
            self.chart.window_shares(),
            self.chart.window_totals_seconds(),
        )


class MainWindow(QMainWindow):
    def __init__(self, client: MinmaxClient, stream_key: str = "timecard") -> None:
        super().__init__()
        self._client = client
        self._stream_key = stream_key

        self.setWindowTitle("Minmax")
        self.resize(1100, 800)

        self._day_screen = DayScreen(client, stream_key, self)
        self._trends_screen = TrendsScreen(client, stream_key, self)

        self._screens = QStackedWidget()
        self._screens.addWidget(self._day_screen)
        self._screens.addWidget(self._trends_screen)
        self.setCentralWidget(self._screens)

        _bind(self, GLOBAL_SHORTCUTS, scoped=False)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

        self.show_day_screen()

    # -- screens ---------------------------------------------------------

    def show_day_screen(self) -> None:
        self._screens.setCurrentWidget(self._day_screen)
        self._day_screen.setFocus()
        self.statusBar().showMessage(
            "←/→ day  ·  T today  ·  G go to date  ·  +/- zoom  ·  F fit  "
            "·  2 trends  ·  ? shortcuts"
        )

    def show_trends_screen(self) -> None:
        self._screens.setCurrentWidget(self._trends_screen)
        self._trends_screen.setFocus()
        self.statusBar().showMessage(
            "←/→ scroll  ·  +/- zoom  ·  F fit all  ·  N percentages  "
            "·  double-click to focus  ·  1 day  ·  ? shortcuts"
        )

    @property
    def current_screen(self) -> QWidget:
        return self._screens.currentWidget()

    @property
    def day_screen(self) -> DayScreen:
        return self._day_screen

    @property
    def trends_screen(self) -> TrendsScreen:
        return self._trends_screen

    # -- help ------------------------------------------------------------

    def show_shortcuts(self) -> None:
        sections = (
            ("Anywhere", GLOBAL_SHORTCUTS),
            ("Day view", DAY_SHORTCUTS),
            ("Trends view", TRENDS_SHORTCUTS),
        )
        blocks = []
        for heading, table in sections:
            rows = "\n".join(f"{shortcut:<8}  {help_}" for _, shortcut, _, help_ in table)
            blocks.append(f"{heading}\n{rows}")

        message = QMessageBox(self)
        message.setWindowTitle("Shortcuts")
        message.setText("Keyboard shortcuts")
        message.setInformativeText(
            "\n\n".join(blocks) + "\n\nCtrl/Cmd + scroll zooms around the pointer on both screens."
        )
        message.setFont(QFont("Menlo"))
        message.exec()

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
