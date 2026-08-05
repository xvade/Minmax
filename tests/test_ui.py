"""Tests for the desktop app.

The UI is deliberately thin -- the arithmetic is in ``core.timeline`` and the
presentation values are in ``api``, both tested directly. What is left to check
here is wiring: that the window asks the client for the right day, that every
shortcut points at a method that exists, and that the timeline lays out exactly
the records the client returned.

Runs headless via Qt's offscreen platform, so it needs no display.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra")

from PySide6.QtWidgets import QApplication

from minmax.api import InProcessClient, MinmaxClient
from minmax.core.timeline import DEFAULT_PIXELS_PER_HOUR, MAX_PIXELS_PER_HOUR
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository
from minmax.ui.main_window import (
    DAY_SHORTCUTS,
    GLOBAL_SHORTCUTS,
    TRENDS_SHORTCUTS,
    MainWindow,
)

PACIFIC = "America/Los_Angeles"


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def client(repository: Repository, home: Path, sample_csv: Path) -> MinmaxClient:
    from zoneinfo import ZoneInfo

    run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
    return InProcessClient(repository, timezone=ZoneInfo(PACIFIC))


@pytest.fixture
def window(qt_app: QApplication, client: MinmaxClient) -> MainWindow:
    return MainWindow(client)


class TestStartup:
    def test_opens_on_the_most_recent_day_with_data(self, window: MainWindow) -> None:
        # Opening on an arbitrary "today" with nothing on it would look broken.
        assert window.day == date(2026, 6, 6)

    def test_shows_the_day_and_its_total_in_the_header(self, window: MainWindow) -> None:
        assert "June 2026" in window.day_screen._date_label.text()
        assert window.day_screen._total_label.text() == "9h tracked"

    def test_a_stream_with_no_data_still_opens(
        self, qt_app: QApplication, repository: Repository
    ) -> None:
        from minmax.core.models import StreamKind

        repository.ensure_stream("empty", "Empty", StreamKind.INTERVAL)
        blank = MainWindow(InProcessClient(repository), stream_key="empty")
        assert blank.day == date.today()
        assert blank.day_screen._total_label.text() == "nothing tracked"


class TestNavigation:
    def test_moving_a_day_refreshes_the_view(self, window: MainWindow) -> None:
        window.show_previous_day()
        assert window.day == date(2026, 6, 5)
        # The 5th holds only the first hour of the overnight sleep run.
        assert window.day_screen._total_label.text() == "1h tracked"

    def test_next_day_moves_forward(self, window: MainWindow) -> None:
        window.show_next_day()
        assert window.day == date(2026, 6, 7)

    def test_first_and_last_jump_to_the_data_bounds(self, window: MainWindow) -> None:
        window.show_first_day()
        assert window.day == date(2026, 6, 5)
        window.show_last_day()
        assert window.day == date(2026, 6, 6)

    def test_today_jumps_to_today(self, window: MainWindow) -> None:
        window.show_today()
        assert window.day == date.today()

    def test_navigating_to_an_empty_day_is_not_an_error(self, window: MainWindow) -> None:
        window.show_day(date(2026, 6, 6) + timedelta(days=40))
        assert window.day_screen._total_label.text() == "nothing tracked"


class TestZoom:
    def test_zooming_in_increases_hour_density(self, window: MainWindow) -> None:
        before = window.day_screen._timeline.timeline.pixels_per_hour
        window.zoom_in()
        assert window.day_screen._timeline.timeline.pixels_per_hour > before

    def test_zoom_is_clamped_at_the_maximum(self, window: MainWindow) -> None:
        for _ in range(50):
            window.zoom_in()
        assert window.day_screen._timeline.timeline.pixels_per_hour == MAX_PIXELS_PER_HOUR

    def test_reset_returns_to_the_default(self, window: MainWindow) -> None:
        window.zoom_in()
        window.reset_zoom()
        assert window.day_screen._timeline.timeline.pixels_per_hour == DEFAULT_PIXELS_PER_HOUR

    def test_fit_makes_the_whole_day_visible(self, window: MainWindow) -> None:
        # The view that answers "where did today go?" without scrolling.
        window.resize(1000, 800)
        window.show()
        window.fit_day()

        pane = window.day_screen._timeline
        geometry = pane.timeline.day_geometry()
        assert geometry is not None
        assert geometry.height <= pane.viewport().height() + 1

    def test_fitting_a_dst_day_still_fits(self, window: MainWindow) -> None:
        # A 25-hour day must fit too, not overflow by an hour.
        window.resize(1000, 800)
        window.show()
        window.show_day(date(2026, 11, 1))
        window.fit_day()

        pane = window.day_screen._timeline
        geometry = pane.timeline.day_geometry()
        assert geometry is not None
        assert geometry.seconds == 25 * 3600
        assert geometry.height <= pane.viewport().height() + 1


class TestTimelineContents:
    def test_lays_out_every_record_the_client_returned(self, window: MainWindow) -> None:
        timeline = window.day_screen._timeline.timeline
        assert len(timeline._blocks) == 4

    def test_the_overnight_run_is_flagged_as_continuing_from_yesterday(
        self, window: MainWindow
    ) -> None:
        first = window.day_screen._timeline.timeline._blocks[0]
        assert first.continues_before
        assert first.top == pytest.approx(0.0)

    def test_blocks_are_ordered_down_the_day(self, window: MainWindow) -> None:
        tops = [block.top for block in window.day_screen._timeline.timeline._blocks]
        assert tops == sorted(tops)

    def test_hour_marks_reach_the_view(self, window: MainWindow) -> None:
        assert len(window.day_screen._timeline.timeline._view.hour_marks) == 24


class TestShortcuts:
    @pytest.mark.parametrize(("label", "shortcut", "method", "description"), DAY_SHORTCUTS)
    def test_every_shortcut_binds_to_a_real_method(
        self, window: MainWindow, label: str, shortcut: str, method: str, description: str
    ) -> None:
        # The shortcut table drives the actions and the help dialog alike, so a
        # typo here would silently produce a dead key.
        assert callable(getattr(window.day_screen, method))
        assert label and shortcut and description

    def test_the_day_screen_registers_an_action_per_shortcut(self, window: MainWindow) -> None:
        bound = {
            action.shortcut().toString()
            for action in window.day_screen.actions()
            if not action.shortcut().isEmpty()
        }
        for _, shortcut, _, _ in DAY_SHORTCUTS:
            assert shortcut in bound or shortcut.replace("+", "") in "".join(bound)

    def test_shortcuts_are_unique_within_each_screen(self) -> None:
        for table in (DAY_SHORTCUTS, GLOBAL_SHORTCUTS):
            shortcuts = [shortcut for _, shortcut, _, _ in table]
            assert len(shortcuts) == len(set(shortcuts))


class TestLayerBoundary:
    def test_the_ui_never_imports_the_backend_directly(self) -> None:
        """The invariant that makes the UI replaceable, checked mechanically."""
        import minmax.ui as ui_package

        ui_dir = Path(ui_package.__file__).parent
        offenders: list[str] = []
        for module in sorted(ui_dir.glob("*.py")):
            source = module.read_text(encoding="utf-8")
            for forbidden in ("minmax.storage", "minmax.ingest", "minmax.analysis"):
                if forbidden in source:
                    offenders.append(f"{module.name} imports {forbidden}")
        assert offenders == []


class TestTrendsScreen:
    def test_switching_screens_changes_what_is_shown(self, window: MainWindow) -> None:
        assert window.current_screen is window.day_screen
        window.show_trends_screen()
        assert window.current_screen is window.trends_screen
        window.show_day_screen()
        assert window.current_screen is window.day_screen

    def test_the_chart_receives_every_day_in_the_range(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        assert chart._view is not None
        assert chart._view.days == [date(2026, 6, 5), date(2026, 6, 6)]

    def test_the_visible_range_is_always_reported(self, window: MainWindow) -> None:
        # "The earliest and latest day shown should be on screen."
        window.resize(1100, 800)
        window.show()
        window.show_trends_screen()
        screen = window.trends_screen
        screen.chart.resize(800, 500)
        screen.chart.fit_all()

        assert screen.chart.visible_dates() == (date(2026, 6, 5), date(2026, 6, 6))
        assert "5 Jun 2026" in screen._range_label.text()
        assert "6 Jun 2026" in screen._range_label.text()

    def test_shares_are_shown_for_every_series(self, window: MainWindow) -> None:
        # "The % of time dedicated to each timer should be on screen."
        screen = window.trends_screen
        shares = screen.chart.window_shares()
        assert len(shares) == len(screen.chart._view.series)
        assert sum(shares) == pytest.approx(1.0)

    def test_shares_follow_the_visible_window(self, window: MainWindow) -> None:
        # Percentages must describe the days on screen, not the whole dataset --
        # otherwise scrolling would tell the reader nothing.
        window.show_trends_screen()
        chart = window.trends_screen.chart

        chart.resize(800, 500)
        chart.fit_all()
        assert chart.visible_indices() == (0, 1)
        both_days = chart.window_shares()

        # A narrow viewport at maximum zoom leaves room for only the last day.
        chart.resize(120, 500)
        chart.zoom_by(1000.0)
        chart.scroll_to_end()
        assert chart.visible_indices() == (1, 1)
        last_day_only = chart.window_shares()

        assert both_days != last_day_only
        assert sum(last_day_only) == pytest.approx(1.0)
        # The 5th holds only sleep, so dropping it lifts every other share.
        labels = [series.label for series in chart._view.series]
        sleep = labels.index("sleep")
        assert last_day_only[sleep] < both_days[sleep]

    def test_zooming_changes_how_many_days_are_visible(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        before = chart.pixels_per_day
        chart.zoom_by(2.0)
        assert chart.pixels_per_day > before
        chart.zoom_by(0.1)
        assert chart.pixels_per_day < before

    def test_normalised_mode_switches_the_axis_to_shares(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        assert chart.geometry_model().max_value > 1.0  # seconds
        window.trends_screen.toggle_normalised()
        assert chart.normalised
        assert chart.geometry_model().max_value == 1.0  # fractions

    def test_focusing_a_group_splits_it_into_children(self, window: MainWindow) -> None:
        screen = window.trends_screen
        assert screen.prefix is None
        screen.push_focus("media")
        assert screen.prefix == "media"
        assert [s.label for s in screen.chart._view.series] == ["media/gaming"]

    def test_backing_out_returns_to_the_top_level(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.push_focus("media")
        screen.pop_focus()
        assert screen.prefix is None
        screen.pop_focus()  # already at the top; must not raise
        assert screen.prefix is None

    def test_scrolling_stays_within_the_recorded_span(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        chart.scroll_by_days(-9999)
        assert chart.scroll_offset == 0.0
        chart.scroll_by_days(9999)
        assert chart.scroll_offset <= chart._max_offset() + 0.001

    def test_an_empty_stream_renders_without_data(
        self, qt_app: QApplication, repository: Repository
    ) -> None:
        from minmax.core.models import StreamKind

        repository.ensure_stream("empty", "Empty", StreamKind.INTERVAL)
        blank = MainWindow(InProcessClient(repository), stream_key="empty")
        blank.show_trends_screen()
        assert blank.trends_screen.chart.geometry_model() is None


class TestTrendsShortcuts:
    @pytest.mark.parametrize(("label", "shortcut", "method", "description"), TRENDS_SHORTCUTS)
    def test_every_trends_shortcut_binds_to_a_real_method(
        self, window: MainWindow, label: str, shortcut: str, method: str, description: str
    ) -> None:
        assert callable(getattr(window.trends_screen, method))
        assert label and shortcut and description

    def test_day_and_trends_may_reuse_keys_because_they_are_scoped(self) -> None:
        # Left means "previous day" on one screen and "scroll back" on the
        # other; scoping the actions is what lets both exist.
        day_keys = {shortcut for _, shortcut, _, _ in DAY_SHORTCUTS}
        trends_keys = {shortcut for _, shortcut, _, _ in TRENDS_SHORTCUTS}
        assert day_keys & trends_keys

    def test_screen_actions_are_scoped_not_global(self, window: MainWindow) -> None:
        from PySide6.QtCore import Qt as _Qt

        for action in window.trends_screen.actions():
            if not action.shortcut().isEmpty():
                assert action.shortcutContext() == _Qt.ShortcutContext.WidgetWithChildrenShortcut


class TestTrendsRendering:
    def test_the_end_days_are_drawn_across_their_whole_column(self, window: MainWindow) -> None:
        # Stopping at the column centre leaves a blank half-column that reads as
        # the data ramping up from nothing, which the data does not claim.
        from minmax.core.trends import stack_bands

        window.show_trends_screen()
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        chart.fit_all()

        geometry = chart.geometry_model()
        assert geometry is not None
        bands = stack_bands([s.values for s in chart._view.series])
        points = chart._edge_points(geometry, bands[-1], 0, len(chart._view.days) - 1, upper=True)

        from minmax.ui.trends_view import AXIS_GUTTER

        assert points[0].x() == pytest.approx(AXIS_GUTTER)
        assert points[-1].x() == pytest.approx(AXIS_GUTTER + geometry.content_width)
        # The extended points carry the real end values, not zero.
        assert points[0].y() == pytest.approx(points[1].y())
        assert points[-1].y() == pytest.approx(points[-2].y())

    def test_interior_windows_get_centre_points_only(self, window: MainWindow) -> None:
        # Only the true first and last days of the series are extended; a window
        # scrolled into the middle must not invent flat runs at its edges.
        from minmax.core.trends import TrendsGeometry

        chart = window.trends_screen.chart
        geometry = TrendsGeometry(
            day_count=10, pixels_per_day=20.0, plot_height=100.0, max_value=100.0
        )
        band = [(0.0, 50.0)] * 10

        interior = chart._edge_points(geometry, band, 3, 6, upper=True)
        assert len(interior) == 4

        touching_both = chart._edge_points(geometry, band, 0, 9, upper=True)
        assert len(touching_both) == 12
