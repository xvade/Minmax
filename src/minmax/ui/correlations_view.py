"""The correlations screen's results table: paint-free, Qt does the drawing.

Unlike the timeline or the trends chart, nothing here is custom-painted --
a native ``QTableWidget`` already gives sortable columns, native selection,
and system styling for free, and a correlation result is exactly the kind of
record-per-row data a table (not a chart) is the right shape for.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget

from minmax.api import CorrelationRow

__all__ = ["CorrelationsTable"]

#: A q at or below this is flagged in the r column, matching the CLI's `*`.
_NOTABLE_Q = 0.10

_COLUMNS = ("r", "q", "n", "Period", "X", "Y", "Stability")
_COL_R, _COL_Q, _COL_N, _COL_PERIOD, _COL_X, _COL_Y, _COL_STABILITY = range(len(_COLUMNS))

#: Shown as the column header's own tooltip -- the two columns a reader
#: cannot be expected to decode from a one-word header alone.
_COLUMN_TOOLTIPS = {
    _COL_Q: "Benjamini-Hochberg false-discovery rate over every combination"
    " the sweep tested, not just this row -- lower is more trustworthy."
    " Marked with * at or below 0.10.",
    _COL_STABILITY: "Share of the tested period lengths that agree with this"
    " row in both strength and sign. Blank means every one did.",
}


class _NumericItem(QTableWidgetItem):
    """A cell that displays formatted text but sorts by its own raw value.

    Qt's default sort compares *displayed* text, so ``"0.90"`` would sort
    before ``"0.7"`` character by character -- wrong for every numeric column
    here. Storing the value and comparing on it directly is the fix.
    """

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class CorrelationsTable(QTableWidget):
    """One row per :class:`~minmax.api.CorrelationRow`, sortable by any column."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self.setHorizontalHeaderLabels(_COLUMNS)
        for column, tooltip in _COLUMN_TOOLTIPS.items():
            header_item = self.horizontalHeaderItem(column)
            assert header_item is not None
            header_item.setToolTip(tooltip)

        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSortingEnabled(True)

        header = self.horizontalHeader()
        header.setSectionResizeMode(_COL_X, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_Y, QHeaderView.ResizeMode.Stretch)
        for column in (_COL_R, _COL_Q, _COL_N, _COL_PERIOD, _COL_STABILITY):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    def set_rows(self, rows: Sequence[CorrelationRow]) -> None:
        # Sorting disabled while populating: Qt re-sorts after every
        # setItem() otherwise, which would scatter row `index` across the
        # table as it fills rather than filling it in order.
        self.setSortingEnabled(False)
        self.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self._populate_row(index, row)
        self.setSortingEnabled(True)

    def _populate_row(self, index: int, row: CorrelationRow) -> None:
        flag = "*" if row.q_value <= _NOTABLE_Q else ""
        perfect = row.stability >= 1.0 and row.consistent_sign
        stability_text = "" if perfect else f"{row.stability:.0%}"

        self.setItem(index, _COL_R, _NumericItem(f"{row.r:+.2f}{flag}", row.r))
        self.setItem(index, _COL_Q, _NumericItem(f"{row.q_value:.2f}", row.q_value))
        self.setItem(index, _COL_N, _NumericItem(str(row.n), row.n))
        self.setItem(index, _COL_PERIOD, QTableWidgetItem(row.interval_label))
        self.setItem(index, _COL_X, QTableWidgetItem(row.x_label))
        self.setItem(index, _COL_Y, QTableWidgetItem(row.y_label))
        self.setItem(index, _COL_STABILITY, _NumericItem(stability_text, row.stability))

        tooltip = "\n".join(row.caveats)
        if tooltip:
            for column in range(len(_COLUMNS)):
                item = self.item(index, column)
                assert item is not None
                item.setToolTip(tooltip)
