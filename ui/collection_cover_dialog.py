"""Pick a Collection Cover from current Collection membership only."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from services.collection_cover import MemberCoverChoice, list_member_cover_choices

_THUMB = 48


class CollectionCoverDialog(QDialog):
    """Members of one Collection. Does not list other games / collections / library."""

    def __init__(
        self,
        collection_name: str,
        choices: list[MemberCoverChoice],
        parent: QWidget | None = None,
    ) -> None:
        from ui.window_lifecycle import register_toplevel

        super().__init__(parent)
        if parent is not None:
            register_toplevel(self)
        self.setWindowTitle("从合集 Mod 选择封面")
        self.setMinimumSize(420, 420)
        self._choices = list(choices)
        self._selected_file: Path | None = None
        layout = QVBoxLayout(self)
        hint = QLabel(f"合集：{collection_name or '—'}\n只显示当前合集中的 Mod。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._list = QListWidget(self)
        self._list.setObjectName("collectionCoverPickList")
        self._list.setIconSize(QSize(_THUMB, _THUMB))
        placeholder = _placeholder_thumb()
        for choice in self._choices:
            item = QListWidgetItem(choice.name)
            item.setData(Qt.ItemDataRole.UserRole, choice.internal_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, str(choice.cover_file or ""))
            icon_pix = _thumb_from_file(choice.cover_file) if choice.cover_file else placeholder
            item.setIcon(QIcon(icon_pix))
            self._list.addItem(item)
        self._list.itemDoubleClicked.connect(self._accept_item)
        layout.addWidget(self._list, stretch=1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if ok_btn is not None:
            ok_btn.setText("选择")
        if cancel_btn is not None:
            cancel_btn.setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_cover_file(self) -> Path | None:
        return self._selected_file

    def _current_cover_file(self) -> Path | None:
        item = self._list.currentItem()
        if item is None:
            return None
        raw = str(item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None

    def _accept_item(self, _item: QListWidgetItem) -> None:
        self.accept()

    def accept(self) -> None:  # noqa: D401
        chosen = self._current_cover_file()
        if chosen is None:
            QMessageBox.warning(self, "选择封面", "该 Mod 没有可用封面。")
            return
        self._selected_file = chosen
        super().accept()


def _placeholder_thumb() -> QPixmap:
    pix = QPixmap(_THUMB, _THUMB)
    pix.fill(Qt.GlobalColor.darkGray)
    return pix


def _thumb_from_file(path: Path) -> QPixmap:
    image = QImage(str(path))
    if image.isNull():
        return _placeholder_thumb()
    scaled = image.scaled(
        _THUMB,
        _THUMB,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - _THUMB) // 2)
    y = max(0, (scaled.height() - _THUMB) // 2)
    return QPixmap.fromImage(scaled).copy(x, y, _THUMB, _THUMB)


def open_collection_cover_dialog(
    collection_id: int,
    collection_name: str,
    *,
    parent: QWidget | None = None,
    db=None,
) -> Path | None:
    choices = list_member_cover_choices(collection_id, db=db)
    dialog = CollectionCoverDialog(collection_name, choices, parent=parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.selected_cover_file()
