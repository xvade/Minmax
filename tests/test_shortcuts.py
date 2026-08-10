"""Tests for the keyboard binding registry and its config file.

These need no display and no PySide6: :mod:`minmax.ui.shortcuts` is deliberately
Qt-free so that the rules about what a valid override is can be checked without
one. What is being protected here is that a hand-edited or stale config file can
never leave the app with a dead key, an ambiguous one, or no keys at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minmax.ui import shortcuts as shortcuts_module
from minmax.ui.shortcuts import (
    ACTIONS,
    CORRELATIONS,
    DAY,
    GLOBAL,
    TRENDS,
    ShortcutMap,
    load_shortcuts,
)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "shortcuts.json"


def write(path: Path, bindings: dict[str, str], *, version: int = 1) -> None:
    path.write_text(json.dumps({"version": version, "bindings": bindings}))


class TestDefaults:
    def test_every_action_has_a_key_a_label_and_help(self) -> None:
        for action in ACTIONS:
            assert action.sequence, action.key
            assert action.label, action.key
            assert action.help, action.key

    def test_action_ids_are_unique(self) -> None:
        keys = [action.key for action in ACTIONS]
        assert len(keys) == len(set(keys))

    def test_no_two_actions_share_a_key_within_a_scope(self) -> None:
        for scope in (GLOBAL, DAY, TRENDS, CORRELATIONS):
            sequences = [action.sequence for action in shortcuts_module.actions_in(scope)]
            assert len(sequences) == len(set(sequences)), scope

    def test_no_screen_key_collides_with_a_global_one(self) -> None:
        # A global action fires everywhere, so it would shadow a screen's key.
        globals_ = {action.sequence for action in shortcuts_module.actions_in(GLOBAL)}
        for scope in (DAY, TRENDS, CORRELATIONS):
            screen = {action.sequence for action in shortcuts_module.actions_in(scope)}
            assert not (globals_ & screen), scope

    def test_the_two_screens_deliberately_reuse_keys(self) -> None:
        # Left means "previous day" on one screen and "scroll back" on the
        # other; scoping the actions is what lets both exist.
        day = {action.sequence for action in shortcuts_module.actions_in(DAY)}
        trends = {action.sequence for action in shortcuts_module.actions_in(TRENDS)}
        assert day & trends


class TestLoading:
    def test_a_missing_file_yields_the_defaults(self, config: Path) -> None:
        shortcuts = load_shortcuts(config)
        assert shortcuts.actions == ACTIONS
        assert shortcuts.problems == ()

    def test_no_path_at_all_yields_the_defaults(self) -> None:
        assert load_shortcuts(None).actions == ACTIONS

    def test_an_override_replaces_one_binding_and_leaves_the_rest(self, config: Path) -> None:
        write(config, {"day.show_next_day": "Ctrl+Right"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "show_next_day") == "Ctrl+Right"
        assert shortcuts.sequence_for(DAY, "show_previous_day") == "Left"
        assert shortcuts.problems == ()

    def test_scoped_ids_keep_the_two_screens_independent(self, config: Path) -> None:
        # Both screens expose zoom_in; remapping one must not touch the other.
        write(config, {"day.zoom_in": "Ctrl+="})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "zoom_in") == "Ctrl+="
        assert shortcuts.sequence_for(TRENDS, "zoom_in") == "+"


class TestBadConfigs:
    def test_corrupt_json_is_reported_but_still_yields_working_keys(self, config: Path) -> None:
        config.write_text("{not json")
        shortcuts = load_shortcuts(config)

        assert shortcuts.actions == ACTIONS
        assert shortcuts.problems  # the reason is surfaced, not swallowed

    def test_a_non_object_payload_is_refused(self, config: Path) -> None:
        config.write_text("[1, 2, 3]")
        assert load_shortcuts(config).actions == ACTIONS

    def test_a_non_object_bindings_key_is_refused(self, config: Path) -> None:
        config.write_text(json.dumps({"version": 1, "bindings": "Left"}))
        shortcuts = load_shortcuts(config)
        assert shortcuts.actions == ACTIONS
        assert shortcuts.problems

    def test_an_unknown_action_is_reported_and_ignored(self, config: Path) -> None:
        # A binding for an action that no longer exists must not stop the rest
        # of the file from applying.
        write(config, {"day.fly_to_the_moon": "M", "day.show_today": "Y"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "show_today") == "Y"
        assert any("fly_to_the_moon" in problem for problem in shortcuts.problems)

    def test_a_renamed_action_keeps_the_binding_it_was_saved_under(self, config: Path) -> None:
        # ``trends.cycle_granularity`` became ``trends.cycle_interval``. A rename
        # inside the app is no reason to silently drop the reader's remap.
        write(config, {"trends.cycle_granularity": "Ctrl+G"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(TRENDS, "cycle_interval") == "Ctrl+G"
        assert shortcuts.problems == ()

    def test_an_empty_sequence_leaves_the_default_in_place(self, config: Path) -> None:
        # Unbinding a key entirely would leave a documented action unreachable.
        write(config, {"day.show_today": "   "})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "show_today") == "T"
        assert shortcuts.problems

    def test_a_conflicting_override_is_refused_not_applied(self, config: Path) -> None:
        # "T" already means Today on the day screen.
        write(config, {"day.fit_day": "T"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "fit_day") == "F"
        assert shortcuts.sequence_for(DAY, "show_today") == "T"
        assert any("already bound" in problem for problem in shortcuts.problems)

    def test_a_screen_key_may_not_shadow_a_global_one(self, config: Path) -> None:
        write(config, {"day.fit_day": "1"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "fit_day") == "F"
        assert shortcuts.problems

    def test_the_same_key_on_two_different_screens_is_allowed(self, config: Path) -> None:
        write(config, {"day.fit_day": "W", "trends.fit_all": "W"})
        shortcuts = load_shortcuts(config)

        assert shortcuts.sequence_for(DAY, "fit_day") == "W"
        assert shortcuts.sequence_for(TRENDS, "fit_all") == "W"
        assert shortcuts.problems == ()


class TestSaving:
    def test_only_the_changed_bindings_are_written(self, config: Path) -> None:
        # Writing all thirty would freeze today's defaults into the file, so a
        # later change to an untouched key would never reach the user.
        shortcuts = ShortcutMap(actions=ACTIONS, path=config).with_bindings({"day.show_today": "Y"})
        shortcuts.save()

        payload = json.loads(config.read_text())
        assert payload["bindings"] == {"day.show_today": "Y"}
        assert payload["version"] == shortcuts_module.CONFIG_VERSION

    def test_saving_then_loading_round_trips(self, config: Path) -> None:
        ShortcutMap(actions=ACTIONS, path=config).with_bindings(
            {"day.show_today": "Y", "trends.fit_all": "Ctrl+0"}
        ).save()

        reloaded = load_shortcuts(config)
        assert reloaded.sequence_for(DAY, "show_today") == "Y"
        assert reloaded.sequence_for(TRENDS, "fit_all") == "Ctrl+0"
        assert reloaded.problems == ()

    def test_the_directory_is_created_if_absent(self, tmp_path: Path) -> None:
        nested = tmp_path / "does" / "not" / "exist" / "shortcuts.json"
        ShortcutMap(actions=ACTIONS, path=nested).with_bindings({"day.show_today": "Y"}).save()
        assert nested.exists()

    def test_restoring_defaults_writes_an_empty_binding_set(self, config: Path) -> None:
        shortcuts = ShortcutMap(actions=ACTIONS, path=config).with_bindings({})
        shortcuts.save()
        assert json.loads(config.read_text())["bindings"] == {}

    def test_saving_without_a_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nowhere to save"):
            ShortcutMap(actions=ACTIONS, path=None).save()
