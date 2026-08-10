"""Every keyboard binding in the app, and the file that overrides them.

This module is the single source the actions, the Shortcuts dialog, and the
config file are all built from. A binding therefore cannot exist without being
documented, be documented without existing, or be remapped in the dialog without
the remap being what the app actually uses.

Two rules keep the file honest across versions:

**Actions are identified by ``scope.name``, not by key.** ``zoom_in`` means
different things on the two screens, and a user who remaps the day view's zoom
should not silently have remapped the trends view's. The name is the method the
screen already exposes, so a config file written today still resolves after the
default key changes.

**A bad override is reported and ignored, never applied.** An unparseable key,
an unknown action, or a key already taken inside the same scope leaves that one
action on its default rather than leaving the app with a dead or ambiguous key.
Nothing here imports Qt, so the parsing and conflict rules are testable without
a display.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

__all__ = [
    "ACTIONS",
    "CONFIG_VERSION",
    "Action",
    "ShortcutMap",
    "actions_in",
    "load_shortcuts",
]

CONFIG_VERSION = 1

#: Scope names, in the order the Shortcuts dialog lists them.
GLOBAL = "global"
DAY = "day"
TRENDS = "trends"

SCOPES = (GLOBAL, DAY, TRENDS)

SCOPE_TITLES = {
    GLOBAL: "Anywhere",
    DAY: "Day view",
    TRENDS: "Trends view",
}


@dataclass(frozen=True, slots=True)
class Action:
    """One thing the user can do, and the key that does it."""

    scope: str
    name: str
    """The method the owning widget exposes. Also the stable id in the config."""

    label: str
    sequence: str
    """A ``QKeySequence`` portable string, e.g. ``"Ctrl+Right"``."""

    help: str

    @property
    def key(self) -> str:
        """``scope.name`` -- how the config file refers to this action."""
        return f"{self.scope}.{self.name}"


#: The defaults. Order within a scope is the order the dialog shows.
ACTIONS: tuple[Action, ...] = (
    Action(GLOBAL, "show_day_screen", "Day view", "1", "The day timeline"),
    Action(GLOBAL, "show_trends_screen", "Trends view", "2", "The stacked area over time"),
    Action(GLOBAL, "show_shortcuts", "Shortcuts", "?", "Show the shortcut list"),
    Action(GLOBAL, "edit_shortcuts", "Customize shortcuts", "Ctrl+,", "Remap any of these keys"),
    Action(GLOBAL, "prompt_for_import", "Import…", "Ctrl+I", "Import a source file"),
    Action(GLOBAL, "prompt_for_stream", "Switch stream…", "Ctrl+K", "Pick a different stream"),
    Action(GLOBAL, "prompt_for_new_stream", "New stream…", "Ctrl+N", "Declare a new datastream"),
    Action(DAY, "show_previous_day", "Previous day", "Left", "Move back one day"),
    Action(DAY, "show_next_day", "Next day", "Right", "Move forward one day"),
    Action(DAY, "show_today", "Today", "T", "Jump to today"),
    Action(DAY, "show_first_day", "First day", "Home", "Jump to the earliest day with data"),
    Action(DAY, "show_last_day", "Last day", "End", "Jump to the latest day with data"),
    Action(DAY, "prompt_for_date", "Go to date…", "G", "Jump to a specific date"),
    Action(DAY, "zoom_in", "Zoom in", "+", "Show more detail per hour"),
    Action(DAY, "zoom_out", "Zoom out", "-", "Fit more of the day on screen"),
    Action(DAY, "fit_day", "Fit day", "F", "Zoom so the whole day fits the window"),
    Action(DAY, "reset_zoom", "Reset zoom", "0", "Return to the default hour density"),
    Action(DAY, "prompt_for_filter", "Filter…", "/", "Restrict to a category and its children"),
    Action(DAY, "clear_context", "Clear", "Escape", "Close the inspector, then clear the filter"),
    Action(TRENDS, "scroll_back", "Scroll back", "Left", "Move the window earlier"),
    Action(TRENDS, "scroll_forward", "Scroll forward", "Right", "Move the window later"),
    Action(TRENDS, "scroll_to_start", "Earliest", "Home", "Jump to the start of the range"),
    Action(TRENDS, "scroll_to_end", "Latest", "End", "Jump to the most recent column"),
    Action(TRENDS, "zoom_in", "Show fewer columns", "+", "Widen each column"),
    Action(TRENDS, "zoom_out", "Show more columns", "-", "Fit more columns on screen"),
    Action(TRENDS, "fit_all", "Fit all", "F", "Show the whole range at once"),
    Action(TRENDS, "toggle_normalised", "Percentage view", "N", "Hours, or share of each column"),
    Action(TRENDS, "cycle_interval", "Interval", "D", "Cycle auto, daily, weekly, monthly"),
    Action(TRENDS, "prompt_for_interval", "Interval…", "Shift+D", "Set how long a column covers"),
    Action(TRENDS, "prompt_for_range", "Range…", "R", "Pin the chart to a fixed date range"),
    Action(TRENDS, "clear_range", "All history", "Shift+R", "Unpin the range"),
    Action(TRENDS, "pop_focus", "Back out", "Escape", "Leave the focused group"),
)


def actions_in(scope: str, actions: tuple[Action, ...] = ACTIONS) -> tuple[Action, ...]:
    return tuple(action for action in actions if action.scope == scope)


@dataclass(frozen=True, slots=True)
class ShortcutMap:
    """The bindings in effect, after overrides have been applied and checked."""

    actions: tuple[Action, ...]
    problems: tuple[str, ...] = ()
    """Human-readable reasons any override was refused. Empty when all applied."""

    path: Path | None = None
    """Where overrides are read from and written to, if anywhere."""

    def scope(self, scope: str) -> tuple[Action, ...]:
        return actions_in(scope, self.actions)

    def sequence_for(self, scope: str, name: str) -> str | None:
        for action in self.actions:
            if action.scope == scope and action.name == name:
                return action.sequence
        return None

    def overrides(self) -> dict[str, str]:
        """Bindings that differ from the defaults -- what gets written to disk."""
        defaults = {action.key: action.sequence for action in ACTIONS}
        return {
            action.key: action.sequence
            for action in self.actions
            if defaults.get(action.key) != action.sequence
        }

    def with_bindings(self, bindings: dict[str, str]) -> ShortcutMap:
        """A new map with ``{scope.name: sequence}`` applied over the defaults."""
        return _apply(ACTIONS, bindings, path=self.path)

    def save(self) -> None:
        """Write the non-default bindings, creating the file's directory if needed."""
        if self.path is None:
            raise ValueError("this shortcut map has nowhere to save to")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CONFIG_VERSION, "bindings": self.overrides()}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_shortcuts(path: Path | None) -> ShortcutMap:
    """Read overrides from ``path``, falling back to the defaults for anything bad.

    A missing file is not an error -- it is the normal state before anyone has
    remapped anything. A corrupt one is reported but still yields a working app,
    because losing every keyboard shortcut is a far worse failure than losing a
    customization.
    """
    if path is None or not path.exists():
        return ShortcutMap(actions=ACTIONS, path=path)

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return ShortcutMap(actions=ACTIONS, problems=(f"{path}: {error}",), path=path)

    if not isinstance(payload, dict):
        return ShortcutMap(actions=ACTIONS, problems=(f"{path}: not a JSON object",), path=path)

    raw = payload.get("bindings", {})
    if not isinstance(raw, dict):
        return ShortcutMap(
            actions=ACTIONS, problems=(f"{path}: 'bindings' is not an object",), path=path
        )

    bindings = {str(key): str(value) for key, value in raw.items()}
    return _apply(ACTIONS, bindings, path=path)


#: Actions that have been renamed, old key to new. A saved override names an
#: action, so renaming one would silently discard the user's binding and report
#: it as an unknown action. Keeping the old name readable here costs a line and
#: means a remap survives a refactor it had nothing to do with.
_RENAMED = {
    "trends.cycle_granularity": "trends.cycle_interval",
}


def _apply(
    defaults: tuple[Action, ...], bindings: dict[str, str], *, path: Path | None
) -> ShortcutMap:
    by_key = {action.key: action for action in defaults}
    problems: list[str] = []
    applied: dict[str, str] = {}

    bindings = {_RENAMED.get(key, key): sequence for key, sequence in bindings.items()}

    for key, sequence in bindings.items():
        action = by_key.get(key)
        if action is None:
            problems.append(f"unknown action {key!r}")
            continue
        cleaned = sequence.strip()
        if not cleaned:
            problems.append(f"{key}: empty key sequence")
            continue
        applied[key] = cleaned

    resolved = tuple(
        replace(action, sequence=applied.get(action.key, action.sequence)) for action in defaults
    )

    # A key may mean different things on different screens, but never two things
    # on the same one -- and never two things where a global would shadow it.
    kept: list[Action] = []
    for action in resolved:
        rivals = [
            other
            for other in kept
            if other.sequence == action.sequence
            and (other.scope == action.scope or GLOBAL in (other.scope, action.scope))
        ]
        if rivals and action.key in applied:
            problems.append(
                f"{action.key}: {action.sequence!r} is already bound to {rivals[0].key}"
            )
            kept.append(replace(action, sequence=by_key[action.key].sequence))
            continue
        kept.append(action)

    return ShortcutMap(actions=tuple(kept), problems=tuple(problems), path=path)
