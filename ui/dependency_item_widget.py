"""Display-only dependency list widgets for ModDetailPanel.

Pairs already-projected relationship rows with sidecar Workspace IDs.
Does not query the database or call dependency services.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
)
from ui.styles import ACCENT_ERROR, ACCENT_SUCCESS, TEXT_MUTED

DEPENDENCY_ITEM_HEIGHT_PX = 52
_DEPLOY_DOT_SIZE_PX = 8

_DEPLOY_STATUS_LABELS = {
    DEPLOY_STATUS_DEPLOYED: "已部署",
    DEPLOY_STATUS_FAILED: "部署失败",
    DEPLOY_STATUS_NOT_DEPLOYED: "未部署",
}

_DEPLOY_DOT_COLORS = {
    DEPLOY_STATUS_DEPLOYED: ACCENT_SUCCESS,
    DEPLOY_STATUS_FAILED: ACCENT_ERROR,
    DEPLOY_STATUS_NOT_DEPLOYED: TEXT_MUTED,
}


def normalize_dependency_deploy_status(value: str | None) -> str:
    """Map a mods.deploy_status token to the three UI states."""
    key = str(value or "").strip().lower()
    if key == DEPLOY_STATUS_DEPLOYED:
        return DEPLOY_STATUS_DEPLOYED
    if key == DEPLOY_STATUS_FAILED:
        return DEPLOY_STATUS_FAILED
    return DEPLOY_STATUS_NOT_DEPLOYED


def dependency_deploy_status_label(status: str | None) -> str:
    token = normalize_dependency_deploy_status(status)
    return _DEPLOY_STATUS_LABELS[token]


@dataclass(frozen=True)
class DependencyDisplayRow:
    """One UI row: display name + optional Workspace ID + optional relation pk."""

    name: str
    workspace_id: str = ""
    relation_id: int = 0
    deploy_status: str = DEPLOY_STATUS_NOT_DEPLOYED


def project_dependency_items(
    relationship_rows: Sequence[Mapping[str, Any]] | None = None,
    sidecar_workspace_ids: Sequence[str] | None = None,
    *,
    deploy_status_by_internal_id: Mapping[str, str] | None = None,
) -> list[DependencyDisplayRow]:
    """Zip current dependency projection into one item per dependency.

    ``relationship_rows`` come from ``get_mod_relationships`` (already fetched).
    ``sidecar_workspace_ids`` come from ``ResolvedModMetadata.dependencies``.
    ``deploy_status_by_internal_id`` is ``mods.deploy_status`` keyed by target
    Internal ID (never Workspace ID).

    Internal IDs on relationship rows are never used as Workspace ID.
    Missing names fall back to the Workspace ID string.
    """
    rows = list(relationship_rows or [])
    wids = [
        str(token or "").strip()
        for token in (sidecar_workspace_ids or [])
        if str(token or "").strip()
    ]
    status_map = deploy_status_by_internal_id or {}
    count = max(len(rows), len(wids))
    out: list[DependencyDisplayRow] = []
    for index in range(count):
        row = rows[index] if index < len(rows) else {}
        title = str(row.get("title") or "").strip()
        workspace_id = wids[index] if index < len(wids) else ""
        name = title or workspace_id
        if not name:
            continue
        try:
            relation_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            relation_id = 0
        target_internal = str(row.get("mod_id") or row.get("target_mod_id") or "").strip()
        raw_status = ""
        if target_internal:
            raw_status = str(status_map.get(target_internal) or "")
        out.append(
            DependencyDisplayRow(
                name=name,
                workspace_id=workspace_id,
                relation_id=relation_id,
                deploy_status=normalize_dependency_deploy_status(raw_status),
            )
        )
    return out


class DependencyItem(QFrame):
    """Single dependency: icon, name, Workspace ID, status dot, optional remove."""

    remove_requested = Signal(int)

    def __init__(
        self,
        row: DependencyDisplayRow,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dependencyItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(DEPENDENCY_ITEM_HEIGHT_PX)
        self._row = row

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 6, 8, 6)
        root.setSpacing(8)

        icon = QLabel("📦")
        icon.setObjectName("dependencyItemIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedWidth(20)
        root.addWidget(icon, 0)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(1)

        self._name_label = QLabel(row.name)
        self._name_label.setObjectName("dependencyItemName")
        self._name_label.setWordWrap(False)
        self._name_label.setMaximumHeight(18)
        self._name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        texts.addWidget(self._name_label)

        wid = str(row.workspace_id or "").strip()
        id_text = f"Workspace ID: {wid}" if wid else "Workspace ID: —"
        self._id_label = QLabel(id_text)
        self._id_label.setObjectName("dependencyItemId")
        self._id_label.setWordWrap(False)
        self._id_label.setMaximumHeight(16)
        self._id_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        texts.addWidget(self._id_label)
        root.addLayout(texts, 1)

        status = normalize_dependency_deploy_status(row.deploy_status)
        self._status_dot = QLabel()
        self._status_dot.setObjectName("dependencyItemDeployDot")
        self._status_dot.setFixedSize(_DEPLOY_DOT_SIZE_PX, _DEPLOY_DOT_SIZE_PX)
        self._status_dot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._status_dot.setProperty("tone", status)
        color = _DEPLOY_DOT_COLORS[status]
        radius = _DEPLOY_DOT_SIZE_PX // 2
        self._status_dot.setStyleSheet(
            f"QLabel#dependencyItemDeployDot {{"
            f"background-color: {color};"
            f"border: 1px solid {color};"
            f"border-radius: {radius}px;"
            f"}}"
        )
        self._status_dot.setToolTip(dependency_deploy_status_label(status))
        root.addWidget(self._status_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._remove_btn = QPushButton("×")
        self._remove_btn.setObjectName("dependencyItemRemove")
        self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_btn.setToolTip("删除依赖")
        self._remove_btn.setFlat(True)
        self._remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(int(row.relation_id))
        )
        if int(row.relation_id or 0) <= 0:
            self._remove_btn.hide()
        root.addWidget(self._remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def name_text(self) -> str:
        return str(self._name_label.text() or "")

    def workspace_id_text(self) -> str:
        return str(self._id_label.text() or "")

    def deploy_status_text(self) -> str:
        """Chinese status lives on the dot tooltip, not as a text label."""
        return str(self._status_dot.toolTip() or "")

    def deploy_status_tone(self) -> str:
        return str(self._status_dot.property("tone") or "")

    def display_row(self) -> DependencyDisplayRow:
        return self._row


class DependencyListHost(QWidget):
    """Vertical list of DependencyItem widgets; replace-on-refresh."""

    item_remove_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dependencyListHost")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._rows: list[DependencyDisplayRow] = []
        self.setVisible(False)

    def displayed_rows(self) -> list[DependencyDisplayRow]:
        return list(self._rows)

    def set_items(self, items: Sequence[DependencyDisplayRow]) -> None:
        while self._layout.count():
            taken = self._layout.takeAt(0)
            widget = taken.widget() if taken is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows = list(items)
        for row in self._rows:
            item = DependencyItem(row, parent=self)
            item.remove_requested.connect(self.item_remove_requested.emit)
            self._layout.addWidget(item)
        self.setVisible(bool(self._rows))
