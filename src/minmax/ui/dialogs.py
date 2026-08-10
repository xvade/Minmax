"""The app's modal prompts: category filter, trends range, shortcut remapping.

Each one returns a plain value and does no work of its own, so the screens keep
owning their state and these stay testable by constructing them and reading the
result rather than by driving a pointer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from minmax.api import MAX_INTERVAL_COUNT, CsvPreview, FieldRole, FieldSpec, Granularity, Interval
from minmax.ui.shortcuts import SCOPE_TITLES, SCOPES, Action, ShortcutMap

__all__ = [
    "ALL_CATEGORIES",
    "IntervalDialog",
    "NewStreamDialog",
    "RangeDialog",
    "ShortcutsDialog",
    "prompt_for_prefix",
]

#: The first entry of the filter list, meaning "no filter". A sentinel rather
#: than an empty string so it is impossible to confuse with a real path.
ALL_CATEGORIES = "— all categories —"


def prompt_for_prefix(
    parent: QWidget, category_paths: list[str], current: str | None
) -> tuple[bool, str | None]:
    """Ask for a category prefix.

    Returns ``(accepted, prefix)``. Cancelling and clearing the filter are
    different answers -- both leave ``prefix`` as ``None`` -- so the accepted
    flag is carried separately rather than overloading the value.

    Every known path is offered, not just top-level groups, because filtering to
    ``media/gaming`` is as reasonable as filtering to ``media`` and the backend
    matches component-wise either way.
    """
    items = [ALL_CATEGORIES, *category_paths]
    try:
        index = items.index(current) if current else 0
    except ValueError:
        index = 0

    chosen, accepted = QInputDialog.getItem(
        parent, "Filter", "Show only this category and its children:", items, index, True
    )
    if not accepted:
        return (False, None)
    cleaned = chosen.strip()
    if not cleaned or cleaned == ALL_CATEGORIES:
        return (True, None)
    return (True, cleaned)


@dataclass(frozen=True, slots=True)
class _Preset:
    label: str
    days: int | None
    """Days back from the last day with data. ``None`` means all history."""


_PRESETS = (
    _Preset("All history", None),
    _Preset("Last 30 days", 30),
    _Preset("Last 90 days", 90),
    _Preset("Last 180 days", 180),
    _Preset("Last year", 365),
)


#: The units offered, in the order they appear. "Automatic" is first because it
#: is the right answer most of the time and the only one that needs no thought.
_UNITS: tuple[tuple[str, Granularity], ...] = (
    ("Automatic", Granularity.AUTO),
    ("Days", Granularity.DAY),
    ("Weeks", Granularity.WEEK),
    ("Months", Granularity.MONTH),
)


class IntervalDialog(QDialog):
    """Choose how much time one trends column covers.

    The ``D`` key cycles the three common intervals, which is faster than a
    dialog for the case that is nearly always wanted. This exists for the rest:
    a fortnight, a quarter, five days. The count is capped rather than free so
    that a slipped keystroke cannot ask for a column measured in centuries.
    """

    def __init__(self, parent: QWidget, current: Interval) -> None:
        super().__init__(parent)
        self.setWindowTitle("Column interval")

        self._unit = QComboBox()
        for label, _ in _UNITS:
            self._unit.addItem(label)
        self._unit.setCurrentIndex(next(i for i, (_, u) in enumerate(_UNITS) if u == current.unit))
        self._unit.currentIndexChanged.connect(self._on_changed)

        self._count = QSpinBox()
        self._count.setRange(1, MAX_INTERVAL_COUNT)
        self._count.setValue(current.count)
        self._count.valueChanged.connect(self._on_changed)

        form = QFormLayout()
        form.addRow("Every", self._count)
        form.addRow("Unit", self._unit)

        self._preview = QLabel()
        self._preview.setEnabled(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self._preview)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self._on_changed()

    def _on_changed(self) -> None:
        # AUTO picks its own unit from the span, so a multiplier on it would
        # have nothing to multiply. Disable rather than hide, so the control
        # does not jump around as the unit changes.
        automatic = self._unit.currentIndex() == 0
        self._count.setEnabled(not automatic)
        self._preview.setText(
            "Each column covers as much as fits the range."
            if automatic
            else f"Each column covers {self.selection().label}."
        )

    def selection(self) -> Interval:
        unit = _UNITS[self._unit.currentIndex()][1]
        return Interval(unit, 1 if unit is Granularity.AUTO else self._count.value())


class RangeDialog(QDialog):
    """Pin the trends chart to a fixed window, or release it to all history.

    Presets are relative to the last day with data rather than to today, so
    "last 30 days" means thirty days of data instead of thirty days that might
    be mostly empty if an import is overdue.
    """

    def __init__(
        self,
        parent: QWidget,
        bounds: tuple[date, date],
        current: tuple[date | None, date | None] = (None, None),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trends range")
        self._bounds = bounds

        self._presets = QComboBox()
        for preset in _PRESETS:
            self._presets.addItem(preset.label)
        self._presets.addItem("Custom…")
        self._presets.currentIndexChanged.connect(self._on_preset_changed)

        self._since = QDateEdit()
        self._until = QDateEdit()
        for editor in (self._since, self._until):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setDateRange(_qdate(bounds[0]), _qdate(bounds[1]))
            editor.dateChanged.connect(self._on_date_edited)

        self._since.setDate(_qdate(current[0] or bounds[0]))
        self._until.setDate(_qdate(current[1] or bounds[1]))
        self._presets.setCurrentIndex(len(_PRESETS) if current[0] or current[1] else 0)

        form = QFormLayout()
        form.addRow("Range", self._presets)
        form.addRow("From", self._since)
        form.addRow("To", self._until)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        hint = QLabel(f"Data runs {bounds[0].isoformat()} to {bounds[1].isoformat()}.")
        hint.setEnabled(False)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self._on_preset_changed(self._presets.currentIndex())

    def _on_preset_changed(self, index: int) -> None:
        custom = index >= len(_PRESETS)
        self._since.setEnabled(custom)
        self._until.setEnabled(custom)
        if custom:
            return
        preset = _PRESETS[index]
        last = self._bounds[1]
        first = (
            self._bounds[0]
            if preset.days is None
            else max(self._bounds[0], last - timedelta(days=preset.days - 1))
        )
        # Set without letting the edit handler bounce the combo to Custom.
        self._since.blockSignals(True)
        self._until.blockSignals(True)
        self._since.setDate(_qdate(first))
        self._until.setDate(_qdate(last))
        self._since.blockSignals(False)
        self._until.blockSignals(False)

    def _on_date_edited(self) -> None:
        if self._presets.currentIndex() < len(_PRESETS):
            return
        if self._until.date() < self._since.date():
            self._until.setDate(self._since.date())

    def selection(self) -> tuple[date | None, date | None]:
        """``(since, until)``, both ``None`` when the whole history was chosen."""
        index = self._presets.currentIndex()
        if index < len(_PRESETS) and _PRESETS[index].days is None:
            return (None, None)
        return (_pydate(self._since.date()), _pydate(self._until.date()))


class NewStreamDialog(QDialog):
    """Declare a point-in-time stream: pick a CSV, name it, and assign roles.

    Roles are guessed from the file -- confidently for the timestamp,
    conservatively (number vs. text) for everything else -- and start there,
    but every column's role is an ordinary dropdown a reader can change:
    nothing is committed until "Create" is pressed. ``inspect`` is injected
    rather than reaching for the client directly so this stays constructible
    in a test without a real file on disk.
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        existing_keys: Sequence[str],
        inspect: Callable[[Path], CsvPreview],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New stream")
        self.resize(560, 480)
        self._existing_keys = set(existing_keys)
        self._inspect = inspect
        self._preview: CsvPreview | None = None
        self._path: Path | None = None
        self._key_edited_by_hand = False

        self._path_label = QLabel("No file chosen.")
        self._path_label.setEnabled(False)
        choose = QPushButton("Choose CSV…")
        choose.clicked.connect(self._choose_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self._path_label, 1)
        file_row.addWidget(choose)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Heart Rate")
        self._name.textEdited.connect(self._on_name_edited)
        self._key = QLineEdit()
        self._key.setPlaceholderText("heart_rate")
        self._key.textEdited.connect(self._on_key_edited)

        form = QFormLayout()
        form.addRow("Name", self._name)
        form.addRow("Key", self._key)

        self._fields_table = QTableWidget(0, 2)
        self._fields_table.setHorizontalHeaderLabels(["CSV column", "Role"])
        self._fields_table.verticalHeader().setVisible(False)
        self._fields_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._fields_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        self._problem = QLabel()
        self._problem.setWordWrap(True)
        self._problem.hide()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        layout = QVBoxLayout()
        layout.addLayout(file_row)
        layout.addLayout(form)
        layout.addWidget(self._fields_table, 1)
        layout.addWidget(self._problem)
        layout.addWidget(self._buttons)
        self.setLayout(layout)

    def _choose_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "New stream", "", "CSV files (*.csv);;All files (*)"
        )
        if not path_str:
            return
        self._path = Path(path_str)
        self._path_label.setText(self._path.name)
        self._preview = self._inspect(self._path)

        if not self._preview.ok:
            self._problem.setText(self._preview.error or "could not read that file")
            self._problem.show()
            self._fields_table.setRowCount(0)
            self._update_ok_state()
            return

        self._problem.hide()
        if not self._name.text():
            self._name.setText(self._path.stem.replace("_", " ").replace("-", " ").title())
            self._sync_key_from_name()
        self._populate_fields()
        self._update_ok_state()

    def _sync_key_from_name(self) -> None:
        if not self._key_edited_by_hand:
            self._key.setText(_slugify(self._name.text()))

    def _on_name_edited(self, _text: str) -> None:
        self._sync_key_from_name()
        self._update_ok_state()

    def _on_key_edited(self, _text: str) -> None:
        self._key_edited_by_hand = True
        self._update_ok_state()

    def _populate_fields(self) -> None:
        assert self._preview is not None
        self._fields_table.setRowCount(0)
        for spec in self._preview.suggested_fields:
            row = self._fields_table.rowCount()
            self._fields_table.insertRow(row)
            self._fields_table.setItem(row, 0, QTableWidgetItem(spec.source_column))

            combo = QComboBox()
            for role in FieldRole:
                combo.addItem(role.value.replace("_", " "))
            combo.setCurrentIndex(list(FieldRole).index(spec.role))
            combo.currentIndexChanged.connect(self._update_ok_state)
            self._fields_table.setCellWidget(row, 1, combo)

    def _current_fields(self) -> list[FieldSpec]:
        """The declared fields with whatever role each dropdown now shows.

        Read back by index into ``FieldRole`` rather than via
        ``combo.currentData()``: ``FieldRole`` is a ``StrEnum``, and PySide6's
        ``QVariant`` round-trip silently coerces it to a plain ``str`` on the
        way back out, which would make every ``is FieldRole.X`` check below
        fail despite the value comparing equal.
        """
        assert self._preview is not None
        roles = list(FieldRole)
        fields: list[FieldSpec] = []
        for row, spec in enumerate(self._preview.suggested_fields):
            combo = self._fields_table.cellWidget(row, 1)
            assert isinstance(combo, QComboBox)
            fields.append(
                FieldSpec(
                    key=spec.key,
                    label=spec.label,
                    role=roles[combo.currentIndex()],
                    source_column=spec.source_column,
                    unit=spec.unit,
                )
            )
        return fields

    def _update_ok_state(self, *_args: object) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self._is_valid())

    def _is_valid(self) -> bool:
        if self._preview is None or not self._preview.ok:
            return False
        key = self._key.text().strip()
        if not key or key in self._existing_keys:
            return False
        if not self._name.text().strip():
            return False
        at_fields = [f for f in self._current_fields() if f.role is FieldRole.AT]
        return len(at_fields) == 1

    def selection(self) -> tuple[str, str, list[FieldSpec], Path]:
        """``(key, name, fields, path)``. ``path`` is the CSV just inspected,
        so the caller can offer to import it immediately."""
        assert self._path is not None
        return (
            self._key.text().strip(),
            self._name.text().strip(),
            self._current_fields(),
            self._path,
        )


def _slugify(text: str) -> str:
    slug = "".join(ch if ch.isalnum() else "_" for ch in text.strip().lower())
    slug = "_".join(part for part in slug.split("_") if part)
    return slug or "stream"


class ShortcutsDialog(QDialog):
    """Show every binding, and let any of them be remapped and saved.

    Conflicts are refused on save rather than on edit: catching them at the end
    means a reader swapping two keys can type the second one without the dialog
    complaining about the state in between.
    """

    def __init__(self, parent: QWidget, shortcuts: ShortcutMap) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shortcuts")
        self.resize(560, 620)
        self._shortcuts = shortcuts
        self._editors: dict[str, QKeySequenceEdit] = {}
        self._result = shortcuts

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Action", "Key", "What it does"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._populate()

        self._problem = QLabel()
        self._problem.setWordWrap(True)
        self._problem.hide()

        restore = QPushButton("Restore defaults")
        restore.clicked.connect(self._restore_defaults)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        footer = QHBoxLayout()
        footer.addWidget(restore)
        footer.addStretch(1)
        footer.addWidget(buttons)

        where = QLabel(
            f"Saved to {shortcuts.path}" if shortcuts.path else "Changes last for this session only"
        )
        where.setEnabled(False)

        layout = QVBoxLayout()
        layout.addWidget(self._table, 1)
        layout.addWidget(self._problem)
        layout.addWidget(where)
        layout.addLayout(footer)
        self.setLayout(layout)

    def _populate(self) -> None:
        self._table.setRowCount(0)
        self._editors.clear()
        for scope in SCOPES:
            self._add_heading(SCOPE_TITLES[scope])
            for action in self._shortcuts.scope(scope):
                self._add_action(action)

    def _add_heading(self, title: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        item = QTableWidgetItem(title)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._table.setItem(row, 0, item)
        self._table.setSpan(row, 0, 1, 3)

    def _add_action(self, action: Action) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(action.label))

        editor = QKeySequenceEdit(QKeySequence(action.sequence))
        editor.setMaximumSequenceLength(1)
        self._editors[action.key] = editor
        self._table.setCellWidget(row, 1, editor)

        self._table.setItem(row, 2, QTableWidgetItem(action.help))

    def _restore_defaults(self) -> None:
        self._shortcuts = self._shortcuts.with_bindings({})
        self._populate()
        self._problem.hide()

    def _on_save(self) -> None:
        bindings = {
            key: editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            for key, editor in self._editors.items()
        }
        candidate = self._shortcuts.with_bindings(
            {key: value for key, value in bindings.items() if value}
        )
        if candidate.problems:
            self._problem.setText("\n".join(candidate.problems))
            self._problem.show()
            return

        self._result = candidate
        self.accept()

    def result_map(self) -> ShortcutMap:
        """The bindings the reader saved, or the ones passed in if they cancelled."""
        return self._result


def _qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def _pydate(value: QDate) -> date:
    """``QDate.toPython`` is untyped, so the conversion is spelled out."""
    return date(value.year(), value.month(), value.day())
