"""Tests for the desktop app.

The UI is deliberately thin -- the arithmetic is in ``core.timeline`` and
``core.trends``, the calendar grouping is in ``core.columns``, the binding rules
are in ``ui.shortcuts``, and the presentation values are in ``api``, all tested
directly elsewhere. What is left to check here is wiring: that the window asks
the client for the right days, that scrolling loads more of them, that every
shortcut points at a method that exists, and that the timeline lays out exactly
the records the client returned.

Runs headless via Qt's offscreen platform, so it needs no display.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the desktop UI is an optional extra")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from minmax.api import (
    MAX_INTERVAL_COUNT,
    Granularity,
    InProcessClient,
    Interval,
    MinmaxClient,
    TimelineRecord,
    TimelineWindow,
)
from minmax.core.palette import swatch_for_slot
from minmax.core.timeline import DEFAULT_PIXELS_PER_HOUR, MAX_PIXELS_PER_HOUR
from minmax.ingest import TimecardCsvImporter, run_import
from minmax.storage.repository import Repository
from minmax.ui import shortcuts as bindings
from minmax.ui.dialogs import IntervalDialog
from minmax.ui.main_window import MAX_WINDOW_DAYS, MainWindow
from minmax.ui.timeline_view import Timeline

PACIFIC = "America/Los_Angeles"


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def client(repository: Repository, home: Path, sample_csv: Path) -> MinmaxClient:
    from zoneinfo import ZoneInfo

    run_import(repository, TimecardCsvImporter(), sample_csv, home=home)
    return InProcessClient(repository, timezone=ZoneInfo(PACIFIC), home=home)


@pytest.fixture
def window(qt_app: QApplication, client: MinmaxClient) -> MainWindow:
    return MainWindow(client)


@pytest.fixture
def shown(window: MainWindow) -> MainWindow:
    """A window with a real viewport, for anything that depends on layout."""
    window.resize(1100, 800)
    window.show()
    return window


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


class TestContinuousTimeline:
    def test_more_than_one_day_is_loaded_at_a_time(self, window: MainWindow) -> None:
        # The point of scrolling past midnight is that the next day is already
        # there to scroll into.
        loaded = window.day_screen.pane.timeline.window_view
        assert loaded is not None
        assert loaded.first_day < loaded.last_day

    def test_the_overnight_run_is_one_block_not_two(self, window: MainWindow) -> None:
        timeline = window.day_screen.pane.timeline
        loaded = timeline.window_view
        assert loaded is not None
        sleep = [r for r in loaded.records if r.category_path == "sleep/night"]
        assert len(sleep) == 1
        # And it is not marked as continuing, because both ends are loaded.
        block = next(b for b in timeline._blocks if loaded.records[b.index].id == sleep[0].id)
        assert not block.continues_before
        assert not block.continues_after

    def test_each_local_midnight_is_marked(self, window: MainWindow) -> None:
        loaded = window.day_screen.pane.timeline.window_view
        assert loaded is not None
        days = [mark.day for mark in loaded.day_marks]
        assert days == sorted(days)
        assert len(days) == (loaded.last_day - loaded.first_day).days + 1

    def test_scrolling_to_the_top_loads_earlier_days(self, shown: MainWindow) -> None:
        screen = shown.day_screen
        # From somewhere with room behind it -- the sample's first day is the
        # floor, and refusing to go past it is the next test's subject.
        screen.show_day(date(2026, 6, 20))
        before = screen.pane.timeline.window_view
        assert before is not None

        screen.extend_earlier()
        after = screen.pane.timeline.window_view
        assert after is not None
        assert after.first_day < before.first_day

    def test_scrolling_to_the_bottom_loads_later_days(self, shown: MainWindow) -> None:
        screen = shown.day_screen
        before = screen.pane.timeline.window_view
        assert before is not None

        screen.extend_later()
        after = screen.pane.timeline.window_view
        assert after is not None
        assert after.last_day > before.last_day

    def test_extending_keeps_the_reader_where_they_were(self, shown: MainWindow) -> None:
        # Prepending days grows the content above the viewport; without an
        # anchor the view would jerk backwards by however many were added.
        screen = shown.day_screen
        screen.pane.scroll_to_epoch(
            screen.pane.timeline.window_view.summary_for(date(2026, 6, 6)).day_start_epoch,
            align_top=True,
        )
        before = screen.pane.epoch_at_top()

        screen.extend_earlier()
        assert screen.pane.epoch_at_top() == pytest.approx(before, abs=60)

    def test_the_window_never_grows_without_bound(self, shown: MainWindow) -> None:
        screen = shown.day_screen
        for _ in range(40):
            screen.extend_earlier()
        loaded = screen.pane.timeline.window_view
        assert loaded is not None
        assert (loaded.last_day - loaded.first_day).days + 1 <= MAX_WINDOW_DAYS

    def test_scrolling_stops_at_the_earliest_recorded_day(self, shown: MainWindow) -> None:
        # Scrolling into a decade of empty days is not navigation, it is a way
        # to get lost.
        screen = shown.day_screen
        for _ in range(40):
            screen.extend_earlier()
        loaded = screen.pane.timeline.window_view
        assert loaded is not None
        assert loaded.first_day == date(2026, 6, 5)

    def test_the_header_follows_the_day_under_the_viewport(self, shown: MainWindow) -> None:
        screen = shown.day_screen
        loaded = screen.pane.timeline.window_view
        assert loaded is not None

        screen.pane.scroll_to_epoch(
            loaded.summary_for(date(2026, 6, 5)).day_start_epoch, align_top=True
        )
        assert screen.day == date(2026, 6, 5)
        assert screen._total_label.text() == "1h tracked"


class TestNavigation:
    def test_moving_a_day_updates_the_header(self, window: MainWindow) -> None:
        window.show_previous_day()
        assert window.day == date(2026, 6, 5)
        # The 5th holds only the first hour of the overnight sleep run.
        assert window.day_screen._total_label.text() == "1h tracked"

    def test_next_day_moves_forward(self, window: MainWindow) -> None:
        window.show_next_day()
        assert window.day == date(2026, 6, 7)

    def test_moving_within_the_loaded_window_does_not_refetch(self, shown: MainWindow) -> None:
        # Scrolling to an already-loaded day keeps its neighbours in place,
        # which is the whole point of a continuous timeline.
        screen = shown.day_screen
        before = screen.pane.timeline.window_view
        screen.show_day(date(2026, 6, 5))
        assert screen.pane.timeline.window_view is before

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
        before = window.day_screen.pane.timeline.pixels_per_hour
        window.zoom_in()
        assert window.day_screen.pane.timeline.pixels_per_hour > before

    def test_zoom_is_clamped_at_the_maximum(self, window: MainWindow) -> None:
        for _ in range(50):
            window.zoom_in()
        assert window.day_screen.pane.timeline.pixels_per_hour == MAX_PIXELS_PER_HOUR

    def test_reset_returns_to_the_default(self, window: MainWindow) -> None:
        window.zoom_in()
        window.reset_zoom()
        assert window.day_screen.pane.timeline.pixels_per_hour == DEFAULT_PIXELS_PER_HOUR

    def test_fit_makes_one_whole_day_visible(self, shown: MainWindow) -> None:
        # The view that answers "where did today go?" without scrolling.
        shown.fit_day()
        pane = shown.day_screen.pane
        loaded = pane.timeline.window_view
        assert loaded is not None

        summary = loaded.summary_for(shown.day)
        day_height = (
            (summary.day_end_epoch - summary.day_start_epoch) / 3600 * pane.timeline.pixels_per_hour
        )
        assert day_height <= pane.viewport().height() + 1

    def test_fitting_a_dst_day_still_fits(self, shown: MainWindow) -> None:
        # A 25-hour day must fit too, not overflow by an hour.
        shown.show_day(date(2026, 11, 1))
        shown.fit_day()

        pane = shown.day_screen.pane
        summary = pane.timeline.window_view.summary_for(date(2026, 11, 1))
        assert summary.day_end_epoch - summary.day_start_epoch == 25 * 3600
        height = 25 * pane.timeline.pixels_per_hour
        assert height <= pane.viewport().height() + 1


class TestTimelineContents:
    def test_lays_out_every_record_the_client_returned(self, window: MainWindow) -> None:
        timeline = window.day_screen.pane.timeline
        assert len(timeline._blocks) == len(timeline.window_view.records)

    def test_blocks_are_ordered_down_the_timeline(self, window: MainWindow) -> None:
        tops = [block.top for block in window.day_screen.pane.timeline._blocks]
        assert tops == sorted(tops)

    def test_hour_marks_cover_every_loaded_day(self, window: MainWindow) -> None:
        loaded = window.day_screen.pane.timeline.window_view
        days = (loaded.last_day - loaded.first_day).days + 1
        assert len(loaded.hour_marks) == 24 * days


class TestTimelineHitTesting:
    """A busy stream can load far more blocks than the sample fixture does --
    a heart-rate or location feed sampled every few minutes, say. record_at
    and paintEvent narrow the blocks they check to those near the pointer or
    the dirty rect (Timeline._visible_block_range) rather than scanning every
    block in the loaded window, so these exercise that with enough blocks for
    the narrowing to actually matter, not just the handful the fixture loads.
    """

    @pytest.fixture
    def busy_timeline(self, qt_app: QApplication) -> tuple[Timeline, int]:
        count = 500
        start = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
        records = [
            TimelineRecord(
                id=index,
                category_path="p",
                recorded_path="p",
                top_level="p",
                swatch=swatch_for_slot(0),
                start_epoch=start + index * 120,
                end_epoch=start + index * 120 + 60,
                duration_seconds=60,
                start_label="00:00",
                end_label="00:01",
            )
            for index in range(count)
        ]
        window_view = TimelineWindow(
            first_day=date(2026, 6, 1),
            last_day=date(2026, 6, 1),
            start_epoch=start,
            end_epoch=start + count * 120,
            records=records,
        )

        timeline = Timeline()
        timeline.resize(400, 20_000)
        timeline.set_zoom(MAX_PIXELS_PER_HOUR)
        timeline.set_window(window_view)
        return timeline, count

    def test_finds_the_right_record_anywhere_in_a_busy_window(
        self, busy_timeline: tuple[Timeline, int]
    ) -> None:
        timeline, count = busy_timeline
        for index in (0, count // 2, count - 1):
            block = timeline._blocks[index]
            centre = QPoint(100, round(block.top + block.height / 2))
            found = timeline.record_at(centre)
            assert found is not None
            assert found.id == index

    def test_a_gap_between_blocks_finds_nothing(self, busy_timeline: tuple[Timeline, int]) -> None:
        timeline, _ = busy_timeline
        earlier, later = timeline._blocks[10], timeline._blocks[11]
        gap_y = round((earlier.bottom + later.top) / 2)
        assert timeline.record_at(QPoint(100, gap_y)) is None


class TestCategoryFilter:
    def test_filtering_restricts_the_timeline_and_the_totals(self, window: MainWindow) -> None:
        screen = window.day_screen
        screen.set_prefix("social")

        loaded = screen.pane.timeline.window_view
        assert loaded is not None
        assert {r.recorded_path for r in loaded.records} == {"social", "social/friends"}
        assert screen._total_label.text() == "1h 11m tracked"

    def test_the_filter_is_named_in_the_header(self, window: MainWindow) -> None:
        window.day_screen.set_prefix("media")
        assert "media" in window.day_screen._filter_label.text()

    def test_clearing_the_filter_restores_everything(self, window: MainWindow) -> None:
        screen = window.day_screen
        screen.set_prefix("social")
        screen.set_prefix(None)

        assert screen.prefix is None
        assert screen._filter_label.text() == ""
        assert screen._total_label.text() == "9h tracked"

    def test_filtering_keeps_the_day_the_reader_was_on(self, window: MainWindow) -> None:
        window.day_screen.show_day(date(2026, 6, 5))
        window.day_screen.set_prefix("sleep")
        assert window.day_screen.day == date(2026, 6, 5)

    def test_escape_clears_the_filter(self, window: MainWindow) -> None:
        screen = window.day_screen
        screen.set_prefix("social")
        screen.clear_context()
        assert screen.prefix is None


class TestRecordInspector:
    def test_selecting_a_record_shows_its_detail(self, window: MainWindow) -> None:
        screen = window.day_screen
        record = screen.pane.timeline.window_view.records[0]
        screen.pane.timeline.select(record)

        assert screen.inspector.record is record
        assert screen.inspector.isVisibleTo(screen)

    def test_the_inspector_is_hidden_until_something_is_selected(self, window: MainWindow) -> None:
        assert window.day_screen.inspector.record is None
        assert window.day_screen.inspector.isHidden()

    def test_detail_includes_exact_times_and_provenance(self, window: MainWindow) -> None:
        from minmax.ui.record_inspector import _detail_rows

        record = window.day_screen.pane.timeline.window_view.records[0]
        rows = dict(_detail_rows(record))

        assert ":" in rows["started"]
        assert rows["duration"]
        assert str(record.import_run_id) in rows["from import"]
        assert record.source_name in rows["from import"]

    def test_a_renamed_timer_shows_both_paths(self, window: MainWindow) -> None:
        # Identity is the source id, so a record's recorded path and its
        # category's current path can legitimately differ.
        from minmax.ui.record_inspector import _detail_rows

        loaded = window.day_screen.pane.timeline.window_view
        renamed = next(r for r in loaded.records if r.recorded_path != r.category_path)
        rows = dict(_detail_rows(renamed))
        assert rows["now filed as"] == renamed.category_path

    def test_escape_closes_the_inspector_before_the_filter(self, window: MainWindow) -> None:
        screen = window.day_screen
        screen.set_prefix("social")
        screen.pane.timeline.select(screen.pane.timeline.window_view.records[0])

        screen.clear_context()
        assert screen.inspector.record is None
        assert screen.prefix == "social"  # still filtered

        screen.clear_context()
        assert screen.prefix is None

    def test_the_selection_clears_when_the_record_leaves_the_window(
        self, window: MainWindow
    ) -> None:
        screen = window.day_screen
        screen.pane.timeline.select(screen.pane.timeline.window_view.records[0])
        screen.show_day(date(2026, 9, 1))
        assert screen.inspector.record is None

    def test_an_instant_record_shows_a_single_at_row_and_its_fields(self) -> None:
        from minmax.api import Swatch, TimelineRecord
        from minmax.ui.record_inspector import _detail_rows

        record = TimelineRecord(
            id=1,
            category_path="heart_rate",
            recorded_path="heart_rate",
            top_level="heart_rate",
            swatch=Swatch(light="#2a78d6", dark="#3987e5"),
            start_epoch=0,
            end_epoch=0,
            duration_seconds=0,
            start_label="08:00",
            end_label="08:00",
            start_detail="2026-06-06 08:00:00 -0700",
            end_detail="2026-06-06 08:00:00 -0700",
            is_instant=True,
            fields=(("BPM", "60 bpm"),),
        )
        rows = dict(_detail_rows(record))
        assert rows["at"] == "2026-06-06 08:00:00 -0700"
        assert "started" not in rows
        assert "ended" not in rows
        assert "duration" not in rows
        assert rows["BPM"] == "60 bpm"


class TestShortcutWiring:
    @pytest.mark.parametrize("action", bindings.actions_in(bindings.DAY), ids=lambda a: a.name)
    def test_every_day_action_binds_to_a_real_method(
        self, window: MainWindow, action: bindings.Action
    ) -> None:
        # The table drives the actions and the dialog alike, so a typo here
        # would silently produce a dead key.
        assert callable(getattr(window.day_screen, action.name))

    @pytest.mark.parametrize("action", bindings.actions_in(bindings.TRENDS), ids=lambda a: a.name)
    def test_every_trends_action_binds_to_a_real_method(
        self, window: MainWindow, action: bindings.Action
    ) -> None:
        assert callable(getattr(window.trends_screen, action.name))

    @pytest.mark.parametrize("action", bindings.actions_in(bindings.GLOBAL), ids=lambda a: a.name)
    def test_every_global_action_binds_to_a_real_method(
        self, window: MainWindow, action: bindings.Action
    ) -> None:
        assert callable(getattr(window, action.name))

    def test_each_screen_registers_one_action_per_binding(self, window: MainWindow) -> None:
        for screen, scope in (
            (window.day_screen, bindings.DAY),
            (window.trends_screen, bindings.TRENDS),
        ):
            bound = {
                action.shortcut().toString()
                for action in screen.actions()
                if not action.shortcut().isEmpty()
            }
            assert len(bound) == len(bindings.actions_in(scope))

    def test_screen_actions_are_scoped_not_global(self, window: MainWindow) -> None:
        from PySide6.QtCore import Qt as _Qt

        for action in window.trends_screen.actions():
            if not action.shortcut().isEmpty():
                assert action.shortcutContext() == _Qt.ShortcutContext.WidgetWithChildrenShortcut


class TestRemappableShortcuts:
    def test_a_config_file_changes_the_bound_key(
        self, qt_app: QApplication, client: MinmaxClient, tmp_path: Path
    ) -> None:
        import json

        config = tmp_path / "shortcuts.json"
        config.write_text(json.dumps({"version": 1, "bindings": {"day.show_today": "Y"}}))

        remapped = MainWindow(client, config_path=config)
        bound = {
            action.text(): action.shortcut().toString() for action in remapped.day_screen.actions()
        }
        assert bound["Today"] == "Y"

    def test_a_broken_config_still_leaves_a_usable_app(
        self, qt_app: QApplication, client: MinmaxClient, tmp_path: Path
    ) -> None:
        # Losing every shortcut is a far worse failure than losing a custom one.
        config = tmp_path / "shortcuts.json"
        config.write_text("{not json")

        salvaged = MainWindow(client, config_path=config)
        assert salvaged.shortcuts.problems
        bound = {
            action.text(): action.shortcut().toString() for action in salvaged.day_screen.actions()
        }
        assert bound["Today"] == "T"

    def test_applying_a_new_map_rebinds_without_leaving_the_old_key(
        self, window: MainWindow
    ) -> None:
        window.set_shortcuts(window.shortcuts.with_bindings({"day.show_today": "Y"}))
        bound = {
            action.shortcut().toString()
            for action in window.day_screen.actions()
            if not action.shortcut().isEmpty()
        }
        assert "Y" in bound
        assert "T" not in bound

    def test_no_path_means_changes_last_only_for_the_session(self, window: MainWindow) -> None:
        assert window.shortcuts.path is None
        with pytest.raises(ValueError, match="nowhere to save"):
            window.shortcuts.save()


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

    def test_the_chart_receives_every_column_in_the_range(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        assert chart._view is not None
        assert [c.start for c in chart._view.columns] == [date(2026, 6, 5), date(2026, 6, 6)]

    def test_the_visible_range_is_always_reported(self, shown: MainWindow) -> None:
        # "The earliest and latest day shown should be on screen."
        shown.show_trends_screen()
        screen = shown.trends_screen
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
        # Percentages must describe the columns on screen, not the whole
        # dataset -- otherwise scrolling would tell the reader nothing.
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

    def test_zooming_changes_how_many_columns_are_visible(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        before = chart.pixels_per_column
        chart.zoom_by(2.0)
        assert chart.pixels_per_column > before
        chart.zoom_by(0.1)
        assert chart.pixels_per_column < before

    def test_normalised_mode_switches_the_axis_to_shares(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        assert chart.geometry_model().max_value > 1.0  # seconds
        window.trends_screen.toggle_normalised()
        assert chart.normalised
        assert chart.geometry_model().max_value == 1.0  # fractions

    def test_stacked_bands_stay_in_sync_with_the_view(self, window: MainWindow) -> None:
        """The chart caches its stacked bands rather than recomputing them on
        every paint (a hover-driven crosshair move repaints far more often
        than the view or normalisation actually change) -- so the cache must
        be rebuilt whenever either does, or the chart would silently go on
        showing a stale shape."""
        from minmax.core.trends import stack_bands

        chart = window.trends_screen.chart
        assert chart._bands == stack_bands(chart._plot_values(), normalise=False)

        window.trends_screen.toggle_normalised()
        assert chart._bands == stack_bands(chart._plot_values(), normalise=True)

        window.trends_screen.push_focus("media")
        assert chart._bands == stack_bands(chart._plot_values(), normalise=True)

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

    def test_scrolling_stays_within_the_range(self, window: MainWindow) -> None:
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        chart.scroll_by_columns(-9999)
        assert chart.scroll_offset == 0.0
        chart.scroll_by_columns(9999)
        assert chart.scroll_offset <= chart._max_offset() + 0.001

    def test_an_empty_stream_renders_without_data(
        self, qt_app: QApplication, repository: Repository
    ) -> None:
        from minmax.core.models import StreamKind

        repository.ensure_stream("empty", "Empty", StreamKind.INTERVAL)
        blank = MainWindow(InProcessClient(repository), stream_key="empty")
        blank.show_trends_screen()
        assert blank.trends_screen.chart.geometry_model() is None


class TestTrendsInterval:
    def test_cycling_walks_auto_day_week_month(self, window: MainWindow) -> None:
        screen = window.trends_screen
        assert screen.interval.unit is Granularity.AUTO
        for expected in (Granularity.DAY, Granularity.WEEK, Granularity.MONTH, Granularity.AUTO):
            screen.cycle_interval()
            assert screen.interval.unit is expected

    def test_changing_granularity_rebuilds_the_columns(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 1), date(2026, 6, 30))
        daily = len(screen.chart._view.columns)

        screen.set_interval(Granularity.WEEK)
        assert len(screen.chart._view.columns) < daily

    def test_the_title_names_the_granularity_in_effect(self, window: MainWindow) -> None:
        screen = window.trends_screen
        # Under AUTO the reader did not choose it, so it must be spelled out.
        assert "auto (daily)" in screen._title_label.text()
        screen.set_interval(Granularity.MONTH)
        assert "monthly" in screen._title_label.text()

    def test_a_custom_interval_can_be_set(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 1), date(2026, 6, 30))
        weekly = len(screen.chart._view.columns)

        screen.set_interval(Interval(Granularity.WEEK, 2))
        assert len(screen.chart._view.columns) < weekly
        assert screen.interval == Interval(Granularity.WEEK, 2)

    def test_the_title_names_a_custom_interval_by_its_span(self, window: MainWindow) -> None:
        # "fortnightly" is obscure and "2-weekly" is not a word, so the title
        # says how long a column is instead of trying to make an adverb of it.
        screen = window.trends_screen
        screen.set_interval(Interval(Granularity.WEEK, 2))
        assert "every 2 weeks" in screen._title_label.text()

    def test_cycling_from_a_custom_interval_returns_to_the_first_preset(
        self, window: MainWindow
    ) -> None:
        # A fortnight is not on the cycle, so guessing which preset the reader
        # meant would be less useful than being predictable.
        screen = window.trends_screen
        screen.set_interval(Interval(Granularity.DAY, 5))
        screen.cycle_interval()
        assert screen.interval == Interval(Granularity.AUTO)

    def test_setting_the_same_interval_again_is_a_no_op(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_interval(Granularity.WEEK)
        before = screen.chart._view
        screen.set_interval(Interval(Granularity.WEEK, 1))
        assert screen.chart._view is before


class TestIntervalDialog:
    def test_it_opens_on_the_interval_in_effect(self, window: MainWindow) -> None:
        dialog = IntervalDialog(window.trends_screen, Interval(Granularity.WEEK, 3))
        assert dialog.selection() == Interval(Granularity.WEEK, 3)

    def test_choosing_a_unit_and_count_returns_that_interval(self, window: MainWindow) -> None:
        dialog = IntervalDialog(window.trends_screen, Interval(Granularity.AUTO))
        dialog._unit.setCurrentIndex(3)  # Months
        dialog._count.setValue(3)
        assert dialog.selection() == Interval(Granularity.MONTH, 3)

    def test_automatic_takes_no_count(self, window: MainWindow) -> None:
        dialog = IntervalDialog(window.trends_screen, Interval(Granularity.DAY, 5))
        dialog._unit.setCurrentIndex(0)  # Automatic
        assert not dialog._count.isEnabled()
        assert dialog.selection() == Interval(Granularity.AUTO)

    def test_the_count_cannot_leave_the_permitted_range(self, window: MainWindow) -> None:
        dialog = IntervalDialog(window.trends_screen, Interval(Granularity.DAY))
        dialog._count.setValue(9999)
        assert dialog.selection().count == MAX_INTERVAL_COUNT
        dialog._count.setValue(0)
        assert dialog.selection().count == 1

    def test_the_preview_says_what_a_column_will_cover(self, window: MainWindow) -> None:
        dialog = IntervalDialog(window.trends_screen, Interval(Granularity.DAY))
        dialog._unit.setCurrentIndex(2)  # Weeks
        dialog._count.setValue(2)
        assert "2 weeks" in dialog._preview.text()


class TestTrendsRange:
    def test_pinning_a_range_restricts_the_chart(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 6), date(2026, 6, 6))

        assert len(screen.chart._view.columns) == 1
        assert screen.chart._view.since == date(2026, 6, 6)

    def test_a_pinned_range_is_announced(self, shown: MainWindow) -> None:
        shown.show_trends_screen()
        screen = shown.trends_screen
        screen.chart.resize(800, 500)
        screen.set_range(date(2026, 6, 6), date(2026, 6, 6))
        screen.chart.fit_all()
        assert "pinned" in screen._range_label.text()

    def test_clearing_the_range_returns_to_all_history(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 6), date(2026, 6, 6))
        screen.clear_range()

        assert screen.range_selection == (None, None)
        assert len(screen.chart._view.columns) == 2

    def test_the_range_survives_a_drill_down(self, window: MainWindow) -> None:
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 6), date(2026, 6, 6))
        screen.push_focus("media")
        assert screen.chart._view.since == date(2026, 6, 6)


class TestAxisLabel:
    """The y-axis gridline text, pulled out of the paint method so the
    rounding bug it once had (a small count ceiling showed the same label on
    two different gridlines) has a test that does not need a display."""

    def test_a_duration_axis_never_shows_seconds_at_zero(self) -> None:
        from minmax.ui.trends_view import _axis_label

        assert _axis_label(0.0, normalised=False, is_event=False) == "0"

    def test_a_percentage_axis_is_a_bare_percent(self) -> None:
        from minmax.ui.trends_view import _axis_label

        assert _axis_label(0.5, normalised=True, is_event=False) == "50%"

    def test_a_count_axis_shows_distinct_labels_at_half_integer_gridlines(self) -> None:
        # The regression: round(1.5) == round(2.0) == 2 via round-half-to-even,
        # which put "2" on two of the four gridlines for a ceiling of 2.
        from minmax.ui.trends_view import _axis_label

        ceiling = 2.0
        labels = [
            _axis_label(ceiling * step / 4, normalised=False, is_event=True) for step in range(5)
        ]
        assert labels == ["0", "0.5", "1", "1.5", "2"]
        assert len(set(labels)) == len(labels)

    def test_a_duration_axis_still_uses_hour_minute_form(self) -> None:
        from minmax.ui.trends_view import _axis_label

        assert _axis_label(2 * 3600.0, normalised=False, is_event=False) == "2h"


class TestTrendsRendering:
    def test_the_end_columns_are_drawn_across_their_whole_width(self, window: MainWindow) -> None:
        # Stopping at the column centre leaves a blank half-column that reads as
        # the data ramping up from nothing, which the data does not claim.
        from minmax.core.trends import stack_bands
        from minmax.ui.trends_view import AXIS_GUTTER

        window.show_trends_screen()
        chart = window.trends_screen.chart
        chart.resize(800, 500)
        chart.fit_all()

        geometry = chart.geometry_model()
        assert geometry is not None
        bands = stack_bands(chart._plot_values())
        points = chart._edge_points(
            geometry, bands[-1], 0, len(chart._view.columns) - 1, upper=True
        )

        assert points[0].x() == pytest.approx(AXIS_GUTTER)
        assert points[-1].x() == pytest.approx(AXIS_GUTTER + geometry.content_width)
        # The extended points carry the real end values, not zero.
        assert points[0].y() == pytest.approx(points[1].y())
        assert points[-1].y() == pytest.approx(points[-2].y())

    def test_interior_windows_get_centre_points_only(self, window: MainWindow) -> None:
        # Only the true first and last columns are extended; a window scrolled
        # into the middle must not invent flat runs at its edges.
        from minmax.core.trends import TrendsGeometry

        chart = window.trends_screen.chart
        geometry = TrendsGeometry(
            column_count=10, pixels_per_column=20.0, plot_height=100.0, max_value=100.0
        )
        band = [(0.0, 50.0)] * 10

        interior = chart._edge_points(geometry, band, 3, 6, upper=True)
        assert len(interior) == 4

        touching_both = chart._edge_points(geometry, band, 0, 9, upper=True)
        assert len(touching_both) == 12

    def test_a_weekly_column_is_plotted_as_its_daily_average(self, window: MainWindow) -> None:
        # Plotting totals would rescale the axis by seven when the granularity
        # changed, making the same data look like a different life.
        screen = window.trends_screen
        screen.set_range(date(2026, 6, 1), date(2026, 6, 7))
        screen.set_interval(Granularity.WEEK)

        view = screen.chart._view
        plotted = screen.chart._plot_values()
        for index, series in enumerate(view.series):
            assert plotted[index][0] == pytest.approx(series.values[0] / 7)


class TestFileImport:
    """``_import_path`` is where the file dialog and the drag-and-drop handler
    both end up, so it is the one place that needs covering: talking to the
    client, telling the reader what happened, and refreshing the screens only
    when something actually changed.

    ``QMessageBox`` is monkeypatched throughout -- its real methods run a modal
    event loop that would hang a headless test waiting for a click that never
    comes.
    """

    def test_a_successful_import_refreshes_both_screens(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        extra = tmp_path / "extra.csv"
        extra.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )

        window._import_path(extra)

        assert window.day_screen._bounds == (date(2026, 6, 5), date(2026, 6, 7))
        assert window.trends_screen._view.until == date(2026, 6, 7)

    def test_a_missing_file_warns_without_touching_the_screens(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow
    ) -> None:
        warnings: list[tuple[object, ...]] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        bounds_before = window.day_screen._bounds

        window._import_path(Path("/no/such/file.csv"))

        assert len(warnings) == 1
        assert "no such file" in warnings[0][-1]
        assert window.day_screen._bounds == bounds_before

    def test_reimporting_identical_content_offers_to_force_it(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, sample_csv: Path
    ) -> None:
        # The fixture has already imported sample_csv once, so importing it
        # again is the skipped path.
        questions: list[tuple[object, ...]] = []
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **k: (questions.append(a), QMessageBox.StandardButton.No)[1],
        )

        window._import_path(sample_csv)

        assert len(questions) == 1
        assert "already imported" in questions[0][-1]

    def test_accepting_the_reimport_offer_reruns_it_with_force(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, sample_csv: Path
    ) -> None:
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        reports: list[tuple[object, ...]] = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: reports.append(a))

        window._import_path(sample_csv)

        assert len(reports) == 1
        assert "4 already present" in reports[0][-1]

    def test_dropping_files_imports_each_local_url(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        from PySide6.QtCore import QUrl

        extra = tmp_path / "extra.csv"
        extra.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW2,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )

        imported: list[Path] = []
        monkeypatch.setattr(window, "_import_path", lambda path, **k: imported.append(path))

        event = MagicMock()
        event.mimeData.return_value.hasUrls.return_value = True
        event.mimeData.return_value.urls.return_value = [QUrl.fromLocalFile(str(extra))]

        window.dropEvent(event)

        assert imported == [extra]
        event.acceptProposedAction.assert_called_once()


class TestAutoFetch:
    """Auto-fetch is opt-in per stream -- a checkbox next to the picker -- and
    ``MainWindow._run_auto_fetch`` is exactly what the real once-a-minute
    QTimer calls, so tests call it directly rather than waiting for the timer.
    ``QMessageBox``/``QFileDialog`` are monkeypatched for the same reason
    ``TestFileImport`` does it: their real methods run a modal event loop with
    no test double behind it.
    """

    def test_the_checkbox_starts_unchecked(self, window: MainWindow) -> None:
        assert not window._auto_fetch_checkbox.isChecked()

    def test_turning_it_on_without_a_known_path_prompts_for_one(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        feed = tmp_path / "feed.csv"
        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(feed), ""))

        window._auto_fetch_checkbox.setChecked(True)

        [summary] = [s for s in window._client.streams() if s.key == "timecard"]
        assert summary.watch_enabled
        assert summary.watch_path == str(feed)
        # Tried immediately, rather than left until the next tick.
        assert window.day_screen._bounds == (date(2026, 6, 5), date(2026, 6, 7))

    def test_cancelling_the_file_picker_leaves_it_off(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow
    ) -> None:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))

        window._auto_fetch_checkbox.setChecked(True)

        assert not window._auto_fetch_checkbox.isChecked()
        [summary] = [s for s in window._client.streams() if s.key == "timecard"]
        assert not summary.watch_enabled

    def test_a_successful_tick_merges_new_rows_and_refreshes_the_screen(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        feed = tmp_path / "feed.csv"
        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        window._client.set_auto_fetch("timecard", enabled=True, path=feed)

        window._run_auto_fetch()

        assert window.day_screen._bounds == (date(2026, 6, 5), date(2026, 6, 7))
        assert "auto-fetched" in window.statusBar().currentMessage()

    def test_a_tick_does_nothing_for_a_disabled_stream(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        feed = tmp_path / "feed.csv"
        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        window._client.set_auto_fetch("timecard", enabled=False, path=feed)
        bounds_before = window.day_screen._bounds

        window._run_auto_fetch()

        assert window.day_screen._bounds == bounds_before

    def test_a_missing_file_prompts_once_and_pauses_on_cancel(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        missing = tmp_path / "gone.csv"
        window._client.set_auto_fetch("timecard", enabled=True, path=missing)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))

        window._run_auto_fetch()

        [summary] = [s for s in window._client.streams() if s.key == "timecard"]
        assert not summary.watch_enabled
        assert not window._auto_fetch_checkbox.isChecked()
        assert "paused" in window.statusBar().currentMessage()

    def test_a_missing_file_prompts_and_resumes_with_a_replacement(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        missing = tmp_path / "gone.csv"
        replacement = tmp_path / "replacement.csv"
        replacement.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        window._client.set_auto_fetch("timecard", enabled=True, path=missing)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(replacement), ""))

        window._run_auto_fetch()

        [summary] = [s for s in window._client.streams() if s.key == "timecard"]
        assert summary.watch_enabled
        assert summary.watch_path == str(replacement)
        assert window.day_screen._bounds == (date(2026, 6, 5), date(2026, 6, 7))

    def test_a_second_failure_on_the_replacement_pauses_without_a_second_prompt(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        missing = tmp_path / "gone.csv"
        also_missing = tmp_path / "still-gone.csv"
        window._client.set_auto_fetch("timecard", enabled=True, path=missing)
        warnings: list[tuple[object, ...]] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(also_missing), ""))

        window._run_auto_fetch()

        [summary] = [s for s in window._client.streams() if s.key == "timecard"]
        assert not summary.watch_enabled
        # One prompt to choose a replacement, one report that it failed too --
        # never a second invitation to keep choosing within the same tick.
        assert len(warnings) == 2

    def test_a_tick_is_skipped_while_a_modal_dialog_is_open(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, tmp_path: Path
    ) -> None:
        feed = tmp_path / "feed.csv"
        feed.write_text(
            "timer_id,timer_name,started_at,stopped_at,duration_seconds\n"
            "NEW1,reading,2026-06-07T09:00:00-07:00,2026-06-07T10:00:00-07:00,3600\n",
            encoding="utf-8",
        )
        window._client.set_auto_fetch("timecard", enabled=True, path=feed)
        monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: object()))
        bounds_before = window.day_screen._bounds

        window._run_auto_fetch()

        assert window.day_screen._bounds == bounds_before


class TestStreamPicker:
    def test_the_picker_lists_every_stream(self, window: MainWindow) -> None:
        assert window._stream_picker.count() == 1
        assert window._stream_picker.currentText() == "Timecard"
        assert window._stream_picker.currentData() == "timecard"

    def test_a_second_stream_appears_after_creation(
        self, window: MainWindow, repository: Repository
    ) -> None:
        from minmax.core.models import StreamKind

        repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        window._refresh_stream_picker()
        assert window._stream_picker.count() == 2
        assert {
            window._stream_picker.itemData(i) for i in range(window._stream_picker.count())
        } == {"timecard", "pickups"}
        # Refreshing must not silently switch the reader away from whatever
        # they were looking at.
        assert window._stream_picker.currentData() == "timecard"

    def test_switching_streams_reloads_both_screens_and_resets_their_state(
        self, window: MainWindow, repository: Repository
    ) -> None:
        from minmax.core.models import StreamKind

        repository.create_stream("pickups", "Pickups", StreamKind.EVENT)
        window.day_screen.set_prefix("media")
        window.trends_screen.push_focus("media")
        window._refresh_stream_picker()

        window.set_stream("pickups")

        assert window._stream_key == "pickups"
        assert window.day_screen._stream_key == "pickups"
        assert window.trends_screen._stream_key == "pickups"
        assert window.day_screen.prefix is None
        assert window.trends_screen.prefix is None
        assert window._stream_picker.currentData() == "pickups"

    def test_picking_the_same_stream_again_is_a_no_op(self, window: MainWindow) -> None:
        # set_stream short-circuits on no change; this just proves it doesn't
        # blow up or double-reload when the picker fires on the same index.
        window.set_stream("timecard")
        assert window._stream_key == "timecard"

    def test_prompt_for_stream_opens_the_pickers_popup(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow
    ) -> None:
        opened = []
        monkeypatch.setattr(window._stream_picker, "showPopup", lambda: opened.append(True))
        window.prompt_for_stream()
        assert opened == [True]


class TestNewStreamDialog:
    """The dialog itself, exercised directly rather than through the file
    picker Qt would otherwise show natively."""

    def _dialog(self, window: MainWindow, existing: list[str] | None = None) -> object:
        from minmax.ui.dialogs import NewStreamDialog

        return NewStreamDialog(
            window, existing_keys=existing or [], inspect=window._client.inspect_csv
        )

    def test_choosing_a_file_populates_the_field_table_and_a_guessed_name(
        self, window: MainWindow, heart_rate_csv: Path
    ) -> None:
        dialog = self._dialog(window)
        dialog._path = heart_rate_csv
        dialog._preview = window._client.inspect_csv(heart_rate_csv)
        dialog._populate_fields()
        assert dialog._fields_table.rowCount() == 2
        assert dialog._fields_table.item(0, 0).text() == "measured_at"

    def test_is_invalid_until_a_key_and_exactly_one_at_field_are_set(
        self, window: MainWindow, heart_rate_csv: Path
    ) -> None:
        dialog = self._dialog(window)
        assert not dialog._is_valid()
        dialog._preview = window._client.inspect_csv(heart_rate_csv)
        dialog._populate_fields()
        assert not dialog._is_valid(), "no name or key yet"
        dialog._name.setText("Heart Rate")
        dialog._sync_key_from_name()
        assert dialog._is_valid()

    def test_a_key_already_taken_is_rejected(
        self, window: MainWindow, heart_rate_csv: Path
    ) -> None:
        dialog = self._dialog(window, existing=["heart_rate"])
        dialog._preview = window._client.inspect_csv(heart_rate_csv)
        dialog._populate_fields()
        dialog._name.setText("Heart Rate")
        dialog._sync_key_from_name()
        assert dialog._key.text() == "heart_rate"
        assert not dialog._is_valid()

    def test_selection_returns_the_edited_roles_not_just_the_guess(
        self, window: MainWindow, heart_rate_csv: Path
    ) -> None:
        from minmax.api import FieldRole

        dialog = self._dialog(window)
        dialog._path = heart_rate_csv
        dialog._preview = window._client.inspect_csv(heart_rate_csv)
        dialog._populate_fields()
        dialog._name.setText("Heart Rate")
        dialog._sync_key_from_name()

        # bpm was guessed as text (the fixture has one bad row); override it.
        bpm_combo = dialog._fields_table.cellWidget(1, 1)
        bpm_combo.setCurrentIndex(list(FieldRole).index(FieldRole.NUMBER))

        key, name, fields, path = dialog.selection()
        assert key == "heart_rate"
        assert name == "Heart Rate"
        assert path == heart_rate_csv
        bpm_field = next(f for f in fields if f.source_column == "bpm")
        assert bpm_field.role is FieldRole.NUMBER


class TestPromptForNewStream:
    """``MainWindow.prompt_for_new_stream`` end to end, with the dialog itself
    swapped out so the test drives no real file picker."""

    def test_creating_a_stream_switches_to_it_and_imports_the_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        window: MainWindow,
        pickups_csv: Path,
    ) -> None:
        from minmax.api import FieldRole, FieldSpec
        from minmax.ui.dialogs import NewStreamDialog
        from minmax.ui.main_window import MainWindow as MainWindowClass

        fields = [FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="occurred_at")]
        monkeypatch.setattr(NewStreamDialog, "exec", lambda self: 1)
        monkeypatch.setattr(
            NewStreamDialog, "selection", lambda self: ("pickups", "Pickups", fields, pickups_csv)
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        assert isinstance(window, MainWindowClass)

        window.prompt_for_new_stream()

        assert window._stream_key == "pickups"
        assert {s.key for s in window._client.streams()} == {"timecard", "pickups"}
        assert window._client.bounds("pickups") == (date(2026, 6, 6), date(2026, 6, 6))
        # The day screen's actually-loaded window, not just the client's data:
        # importing into a brand-new stream before switching to it must not
        # leave the screen anchored on today's date with nothing loaded.
        loaded = window.day_screen.pane.timeline.window_view
        assert loaded is not None
        assert len(loaded.records) == 3

    def test_a_duplicate_key_warns_without_switching(
        self, monkeypatch: pytest.MonkeyPatch, window: MainWindow, pickups_csv: Path
    ) -> None:
        from minmax.api import FieldRole, FieldSpec
        from minmax.ui.dialogs import NewStreamDialog

        fields = [FieldSpec(key="at", label="Time", role=FieldRole.AT, source_column="occurred_at")]
        monkeypatch.setattr(NewStreamDialog, "exec", lambda self: 1)
        monkeypatch.setattr(
            NewStreamDialog, "selection", lambda self: ("timecard", "Timecard", fields, pickups_csv)
        )
        warnings: list[tuple[object, ...]] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

        window.prompt_for_new_stream()

        assert len(warnings) == 1
        assert "already exists" in warnings[0][-1]
        assert window._stream_key == "timecard"


@pytest.fixture
def event_window(window: MainWindow, repository: Repository) -> MainWindow:
    """The ``window`` fixture, switched to a point-in-time stream (heart
    rate, one numeric field) with two readings on the same local day."""
    from minmax.core.models import EventRecord, FieldRole, FieldSpec, StreamKind
    from minmax.core.timeref import Instant

    stream = repository.create_stream(
        "heart_rate",
        "Heart Rate",
        StreamKind.EVENT,
        fields=[
            FieldSpec(
                key="bpm", label="BPM", role=FieldRole.NUMBER, source_column="bpm", unit="bpm"
            )
        ],
    )
    assert stream.id is not None
    run = repository.begin_import(
        stream.id, importer="test", source_name="hr.csv", source_sha256="e" * 64, archived_path=None
    )
    repository.insert_events(
        stream,
        run,
        [
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-06-06T08:00:00-07:00"), {"bpm": 60.0}
            ),
            EventRecord(
                None, "heart_rate", Instant.from_iso("2026-06-06T20:00:00-07:00"), {"bpm": 75.0}
            ),
        ],
    )
    window._refresh_stream_picker()
    window.set_stream("heart_rate")
    return window


class TestEventStreamRendering:
    """Once a point-in-time stream is loaded, the day screen and trends
    screen must both read it as counts, not durations -- the reuse the
    scope-cut in TODO.md describes rather than a Measure abstraction."""

    def test_records_lay_out_as_markers_not_blocks(self, event_window: MainWindow) -> None:
        blocks = event_window.day_screen.pane.timeline._blocks
        assert blocks
        assert all(block.is_marker for block in blocks)

    def test_the_day_total_reads_as_a_count(self, event_window: MainWindow) -> None:
        assert event_window.day_screen._total_label.text() == "2 events"

    def test_the_summary_panel_ranks_and_formats_by_count(self, event_window: MainWindow) -> None:
        from minmax.api import StreamKind

        rows = event_window.day_screen._summary._rows
        [slice_] = rows._slices
        assert slice_.record_count == 2
        assert rows._stream_kind is StreamKind.EVENT
        assert rows._figure(slice_) == "2 events"

    def test_the_trends_chart_ceiling_has_no_24_hour_style_cap(
        self, event_window: MainWindow
    ) -> None:
        from minmax.core.trends import nice_count_ceiling

        event_window.show_trends_screen()
        chart = event_window.trends_screen.chart
        chart.resize(800, 500)
        chart.fit_all()

        geometry = chart.geometry_model()
        assert geometry is not None
        assert geometry.max_value == nice_count_ceiling(chart._view.peak_daily_seconds)

    def test_the_legend_reports_counts_not_durations(self, event_window: MainWindow) -> None:
        event_window.show_trends_screen()
        screen = event_window.trends_screen
        [total] = screen.chart.window_totals_seconds()
        assert total == 2
        assert screen.legend._stream_kind.value == "event"
