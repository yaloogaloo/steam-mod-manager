"""Checklist dialog for Mod → Collection membership (many-to-many)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import CollectionRecord
from services.collection import compute_membership_edits

_STATE_TO_CHECK = {
    "all": Qt.CheckState.Checked,
    "none": Qt.CheckState.Unchecked,
    "mixed": Qt.CheckState.PartiallyChecked,
}
_CHECK_TO_STATE = {
    Qt.CheckState.Checked: "all",
    Qt.CheckState.Unchecked: "none",
    Qt.CheckState.PartiallyChecked: "mixed",
}


class CollectionMembershipDialog(QDialog):
    """One checklist for the current ``_selected_mod_ids`` batch."""

    def __init__(
        self,
        rows: list[tuple[CollectionRecord, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置合集")
        self.setModal(True)
        self._initial: dict[int, str] = {}
        layout = QVBoxLayout(self)
        hint = QLabel("一个 Mod 可以属于多个合集。勾选表示加入，取消勾选表示移出。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._list = QListWidget(self)
        self._list.setObjectName("collectionMembershipList")
        for rec, state in rows:
            cid = int(rec.collection_id)
            label = str(rec.name or "").strip() or f"合集 {cid}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cid)
            key = str(state or "none")
            if key not in _STATE_TO_CHECK:
                key = "none"
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            if key == "mixed":
                flags |= Qt.ItemFlag.ItemIsAutoTristate
            item.setFlags(flags)
            item.setCheckState(_STATE_TO_CHECK[key])
            self._initial[cid] = key
            self._list.addItem(item)
        layout.addWidget(self._list)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def current_states(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            cid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
            if cid <= 0:
                continue
            out[cid] = _CHECK_TO_STATE.get(item.checkState(), "none")
        return out

    def edits(self) -> tuple[list[int], list[int]]:
        return compute_membership_edits(self._initial, self.current_states())
