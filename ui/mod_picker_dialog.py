"""Modal Mod picker for declaring relationships (search + filters)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
)


class ModPickerDialog(QDialog):
    """
    Pick one peer Mod for a relationship.

    ``candidates``: list of dicts with keys
    ``mod_id``, ``title``, ``platform``, ``game_name``.
    """

    def __init__(
        self,
        candidates: list[dict],
        *,
        title: str = "Select Mod",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(420, 480)
        self._all = list(candidates)
        self._selected_id: str | None = None

        root = QVBoxLayout(self)
        root.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称 / ID …")
        self.search.textChanged.connect(self._refilter)
        root.addWidget(self.search)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("游戏"))
        self.game_combo = QComboBox()
        self.game_combo.addItem("全部游戏", "")
        games = sorted(
            {
                str(c.get("game_name") or "").strip()
                for c in self._all
                if str(c.get("game_name") or "").strip()
            },
            key=str.casefold,
        )
        for g in games:
            self.game_combo.addItem(g, g)
        self.game_combo.currentIndexChanged.connect(self._refilter)
        filters.addWidget(self.game_combo, stretch=1)

        filters.addWidget(QLabel("平台"))
        self.platform_combo = QComboBox()
        for key, label in (
            ("", "全部平台"),
            (PLATFORM_STEAM, "Steam"),
            (PLATFORM_NEXUS, "Nexus"),
            (PLATFORM_GITHUB, "GitHub"),
        ):
            self.platform_combo.addItem(label, key)
        self.platform_combo.currentIndexChanged.connect(self._refilter)
        filters.addWidget(self.platform_combo)
        root.addLayout(filters)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._accept_item)
        self.list.itemSelectionChanged.connect(self._on_select)
        root.addWidget(self.list, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._ok is not None:
            self._ok.setEnabled(False)
        root.addWidget(buttons)

        self._refilter()

    def selected_mod_id(self) -> str | None:
        return self._selected_id

    def _on_select(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self._selected_id = None
            if self._ok is not None:
                self._ok.setEnabled(False)
            return
        self._selected_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if self._ok is not None:
            self._ok.setEnabled(bool(self._selected_id))

    def _accept_item(self, item: QListWidgetItem) -> None:
        self._selected_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if self._selected_id:
            self.accept()

    def _refilter(self) -> None:
        q = self.search.text().strip().casefold()
        game = str(self.game_combo.currentData() or "")
        platform = str(self.platform_combo.currentData() or "")
        self.list.clear()
        for c in self._all:
            mid = str(c.get("mod_id") or "")
            title = str(c.get("title") or mid)
            plat = normalize_platform(str(c.get("platform") or PLATFORM_STEAM))
            gname = str(c.get("game_name") or "")
            if game and gname != game:
                continue
            if platform and plat != platform:
                continue
            hay = f"{title} {mid} {plat} {gname}".casefold()
            if q and q not in hay:
                continue
            label = f"{title}  [{plat}]  ({mid})"
            if gname:
                label = f"{title}  · {gname}  [{plat}]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, mid)
            self.list.addItem(item)
        self._selected_id = None
        if self._ok is not None:
            self._ok.setEnabled(False)
