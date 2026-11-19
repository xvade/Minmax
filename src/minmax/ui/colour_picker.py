"""A small popup for choosing one series' colour.

Offers exactly what the storage layer can hold -- *Auto*, one of the palette's
eight hues, or a custom colour -- and reports the choice through a signal. It
knows nothing about streams or charts: the widget that opens it decides what a
choice means.

The eight hues are drawn in the step for the current surface, the same one the
chart uses, so a swatch here is the colour that will actually appear.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QColorDialog,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from minmax.api import PALETTE, SLOT_NAMES, ColourChoice
from minmax.ui import theme

__all__ = ["ColourPicker", "SwatchGrid"]

CELL = 34
GAP = 8
COLUMNS = 4


class SwatchGrid(QWidget):
    """The eight palette hues in a grid, one of which may be marked as chosen."""

    slotClicked = Signal(int)

    def __init__(self, selected: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._selected = selected
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        rows = -(-len(PALETTE) // COLUMNS)
        self.setFixedSize(
            QSize(COLUMNS * CELL + (COLUMNS - 1) * GAP, rows * CELL + (rows - 1) * GAP)
        )

    def cell_rect(self, slot: int) -> QRectF:
        """Where one hue's cell sits, in widget coordinates."""
        row, column = divmod(slot, COLUMNS)
        return QRectF(column * (CELL + GAP), row * (CELL + GAP), CELL, CELL)

    def slot_at(self, point: QPoint) -> int | None:
        for slot in range(len(PALETTE)):
            if self.cell_rect(slot).contains(point.x(), point.y()):
                return slot
        return None

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        appearance = self.palette()
        dark = theme.is_dark(appearance)
        for slot, swatch in enumerate(PALETTE):
            rect = self.cell_rect(slot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(swatch.for_surface(dark=dark)))
            painter.drawRoundedRect(rect, 6, 6)
            if slot == self._selected:
                ring = QPen(appearance.color(appearance.ColorRole.Text), 2)
                painter.setPen(ring)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 8, 8)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        slot = self.slot_at(event.position().toPoint())
        if slot is not None and event.button() == Qt.MouseButton.LeftButton:
            self.slotClicked.emit(slot)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        slot = self.slot_at(event.position().toPoint())
        self.setToolTip(SLOT_NAMES[slot] if slot is not None else "")
        super().mouseMoveEvent(event)


class ColourPicker(QFrame):
    """Popup offering Auto, the eight hues, and a custom colour.

    ``chosen`` carries a :class:`~minmax.api.ColourChoice`, or ``None`` for
    Auto. The popup closes itself after a choice, and on Escape or a click
    outside, like a menu.
    """

    chosen = Signal(object)

    def __init__(
        self,
        title: str,
        current: ColourChoice | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._current = current

        heading = QLabel(title)
        heading.setStyleSheet("font-weight: 600;")

        self.auto_button = QPushButton("Auto")
        self.auto_button.setCheckable(True)
        self.auto_button.setChecked(current is None)
        self.auto_button.setToolTip(
            "Let Minmax pick: a hue that stays clear of any colour you chose by hand."
        )
        self.auto_button.clicked.connect(lambda: self._choose(None))

        self.grid = SwatchGrid(current.slot if current is not None else None)
        self.grid.slotClicked.connect(lambda slot: self._choose(ColourChoice(slot=slot)))

        custom_label = "Custom…"
        if current is not None and current.hex is not None:
            custom_label = f"Custom… ({current.hex})"
        self.custom_button = QPushButton(custom_label)
        self.custom_button.setCheckable(True)
        self.custom_button.setChecked(current is not None and current.hex is not None)
        self.custom_button.clicked.connect(self._pick_custom)

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(self.auto_button)
        layout.addWidget(self.grid)
        layout.addWidget(self.custom_button)
        self.setLayout(layout)

    def popup_at(self, global_point: QPoint) -> None:
        """Show with the top-left corner at ``global_point``."""
        self.adjustSize()
        self.move(global_point)
        self.show()

    def _choose(self, choice: ColourChoice | None) -> None:
        self.chosen.emit(choice)
        self.close()

    def _pick_custom(self) -> None:
        initial = QColor(self._current.hex) if self._current and self._current.hex else QColor()
        # Closed first: a Popup-flagged window dismisses itself when a modal
        # dialog opens over it, which would drop the choice on the floor.
        self.hide()
        colour = QColorDialog.getColor(initial, None, "Custom colour")
        if colour.isValid():
            self._choose(ColourChoice(hex=colour.name()))
        else:
            self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
