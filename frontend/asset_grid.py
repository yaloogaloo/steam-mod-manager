"""Viewport-owned icon layout. The same cell drives paint, hit-test and scrolling."""
from PySide6.QtCore import QEvent, QSize, QTimer, Qt, QItemSelectionModel, QRect
from PySide6.QtWidgets import QListView


class AssetGrid(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.compact = False
        self.columns = 1
        self._queued = False
        self.setMouseTracking(True)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize and not self._queued:
            self._queued = True
            QTimer.singleShot(0, self.reflow)
        return super().eventFilter(watched, event)

    def reflow(self):
        self._queued = False
        width = max(1, self.viewport().width())
        minimum = 194 if self.compact else 214
        self.columns = max(1, width // minimum)
        cell_width = max(1, (width - 1) // self.columns)
        # Avoid huge cards for few columns; surplus stays symmetrically inside cells.
        card_width = min(cell_width - 8, 280)
        from .asset_model import card_body_height
        height = round((card_width - 2) / (1.78 if self.compact else 1.6)) + card_body_height(self.font()) + 8
        size = QSize(cell_width, height)
        if self.gridSize() != size:
            self.setGridSize(size)
            if self.itemDelegate():
                self.itemDelegate().cell_size = size
                self.itemDelegate().compact = self.compact
            self.doItemsLayout()

    def set_density(self, compact):
        self.compact = compact
        self.reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, '_queued'):
            QTimer.singleShot(0, self.reflow)

    def selectionCommand(self, index, event=None):
        # Plain click/arrows change the inspected item, not the checked collection.
        if event is not None and not event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return QItemSelectionModel.NoUpdate
        return super().selectionCommand(index, event)

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and index.isValid():
            from .asset_model import card_rects
            card, _, _ = card_rects(self.visualRect(index), self.font(), self.compact)
            if QRect(card.left()+8, card.top()+8, 22, 22).contains(event.position().toPoint()):
                self.selectionModel().select(index, QItemSelectionModel.Toggle)
                self.setFocus(Qt.MouseFocusReason)
                self.viewport().update()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and self.currentIndex().isValid():
            self.selectionModel().select(self.currentIndex(), QItemSelectionModel.Toggle)
            event.accept()
            return
        super().keyPressEvent(event)
