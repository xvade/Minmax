"""Full detail for one observation.

A block on the timeline shows what fits: a name, and a duration if there is
room. This shows what was actually recorded -- both category paths, the exact
instants with seconds and UTC offset, and which import the row came from.

The provenance line is the point. Every observation carries the id of the import
that produced it, so when something looks wrong the next question is always
"which file said that", and the answer is one click away rather than a database
query. It is also what makes ``minmax revert`` actionable from the UI: the id
shown here is the id that command takes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from minmax.api import TimelineRecord
from minmax.api.format import format_duration

__all__ = ["RecordInspector"]


class RecordInspector(QFrame):
    """Detail panel for the selected record. Hidden when nothing is selected."""

    #: The reader dismissed the panel.
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._record: TimelineRecord | None = None
        self._build_ui()
        self.hide()

    def _build_ui(self) -> None:
        self._title = QLabel()
        title_font = QFont(self.font())
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setWordWrap(True)

        close = QPushButton("✕")
        close.setFlat(True)
        close.setFixedWidth(24)
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.closed)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self._title, 1)
        header.addWidget(close)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 6, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(3)
        self._grid.setColumnStretch(1, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addLayout(header)
        layout.addLayout(self._grid)
        self.setLayout(layout)

        self._rows: list[tuple[QLabel, QLabel]] = []

    # -- content ---------------------------------------------------------

    @property
    def record(self) -> TimelineRecord | None:
        return self._record

    def set_record(self, record: TimelineRecord | None) -> None:
        self._record = record
        if record is None:
            self.hide()
            return

        self._title.setText(record.recorded_path)
        self._set_rows(_detail_rows(record))
        self.show()

    def _set_rows(self, rows: list[tuple[str, str]]) -> None:
        """Rebuild the grid. Small enough that reuse would only add bookkeeping."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        for index, (name, value) in enumerate(rows):
            key = QLabel(name)
            key.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            key.setEnabled(False)  # renders as secondary ink in every appearance

            shown = QLabel(value)
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            self._grid.addWidget(key, index, 0)
            self._grid.addWidget(shown, index, 1)


def _detail_rows(record: TimelineRecord) -> list[tuple[str, str]]:
    if record.is_instant:
        # A single instant, not a span: "started"/"ended"/"duration" would
        # either repeat the same value twice or report a duration that never
        # existed. One "at" row says exactly what happened.
        rows = [("at", record.start_detail or record.start_label)]
        rows.extend(record.fields)
    else:
        rows = [
            ("started", record.start_detail or record.start_label),
            ("ended", record.end_detail or record.end_label),
            ("duration", format_duration(record.duration_seconds)),
        ]

    # Only worth a line when it differs: a timer that was renamed or moved keeps
    # its identity, so the path it was recorded under is not always today's.
    if record.category_path != record.recorded_path:
        rows.append(("now filed as", record.category_path))

    source = record.source_name or "unknown file"
    rows.append(("from import", f"{record.import_run_id} · {source}"))
    rows.append(("record id", str(record.id)))
    return rows
