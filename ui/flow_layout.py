"""Flow layout that wraps child widgets like a CSS flex-wrap row."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    """Lay out widgets left-to-right, wrapping to the next line as needed."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        margin: int = 0,
        h_spacing: int = 12,
        v_spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        size += QSize(left + right, top + bottom)
        return size

    def _horizontal_spacing(self) -> int:
        return self._h_spacing

    def _vertical_spacing(self) -> int:
        return self._v_spacing

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0
        space_x = self._horizontal_spacing()
        space_y = self._vertical_spacing()

        for item in self._items:
            widget = item.widget()
            # Hidden widgets (e.g. library search filter) leave no gap.
            if widget is not None and not widget.isVisibleTo(widget.parentWidget() or widget):
                if not test_only:
                    item.setGeometry(QRect())
                continue
            # Also skip widgets with ExplicitlyHidden visibility while parent is shown.
            if widget is not None and widget.isHidden():
                if not test_only:
                    item.setGeometry(QRect())
                continue

            space_x_eff = space_x
            space_y_eff = space_y
            if widget is not None:
                space_x_eff = _smart_spacing(widget, Qt.Orientation.Horizontal, space_x)
                space_y_eff = _smart_spacing(widget, Qt.Orientation.Vertical, space_y)

            next_x = x + item.sizeHint().width() + space_x_eff
            if next_x - space_x_eff > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y_eff
                next_x = x + item.sizeHint().width() + space_x_eff
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + bottom


def _smart_spacing(widget: QWidget, orientation: Qt.Orientation, fallback: int) -> int:
    policy = widget.sizePolicy()
    if orientation == Qt.Orientation.Horizontal:
        if policy.horizontalPolicy() == QSizePolicy.Policy.Expanding:
            return fallback
    return fallback
