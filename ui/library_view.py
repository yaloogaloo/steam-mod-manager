"""Mod Library view — game filter + card grid + detail panel."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import DEPLOY_STATUS_DEPLOYED, get_db
from core.models import ModMetadata
from core.paths import default_mod_library
from services.file_ops import ModFileManager

from .deploy_thread import DeployWorker
from .flow_layout import FlowLayout
from .library_query import (
    FILTER_ALL,
    FILTER_CATEGORY_ALL,
    FILTER_PLATFORM_ALL,
    PLATFORM_FILTER_LABELS,
    SORT_LABELS,
    SORT_MTIME,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
    filter_and_sort,
    folder_mtime,
    offline_page_exists,
)
from .mod_card import ModCardWidget
from .mod_detail_panel import ModDetailPanel

logger = logging.getLogger(__name__)

ALL_GAMES_LABEL = "全部游戏"
DETAIL_PANEL_WIDTH = 400
GAME_PANEL_WIDTH = 220
GAME_ROLE = Qt.ItemDataRole.UserRole

EMPTY_LIBRARY = "empty_library"
EMPTY_GAME = "empty_game"
EMPTY_SEARCH = "empty_search"


class ModLibraryView(QWidget):
    """View B: browse managed mods under the local library (3-column workspace)."""

    filter_changed = Signal(str)
    request_open_sync = Signal()  # optional: MainWindow may ignore

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: list[ModCardWidget] = []
        self._card_entries: list[tuple[ModFilterIndex, ModCardWidget]] = []
        self._selected_card: ModCardWidget | None = None
        self._selected_path: Path | None = None
        self._current_game_filter: str | None = None
        self.current_game_id: int | None = None
        self.current_game_name: str | None = None
        self._target_root = str(default_mod_library())
        self._pending_game_filter: str | None = None
        self._deploy_worker: DeployWorker | None = None
        self._deploy_mod_id: str | None = None
        self._status_filter = FILTER_ALL
        self._platform_filter = FILTER_PLATFORM_ALL
        self._category_filter = FILTER_CATEGORY_ALL
        self._sort_mode = SORT_MTIME
        self._loading = False
        self._deploy_audit: dict[str, object] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Mod 库")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.count_label = QLabel("0 个")
        self.count_label.setStyleSheet("color: #8b9bb0;")
        self.import_btn = QPushButton("导入 Mod")
        self.import_btn.setObjectName("browseButton")
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.clicked.connect(self._on_import_mod)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.count_label)
        header.addWidget(self.import_btn)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        self.deploy_audit_banner = QLabel("")
        self.deploy_audit_banner.setObjectName("subtitleLabel")
        self.deploy_audit_banner.setWordWrap(True)
        self.deploy_audit_banner.setStyleSheet("color: #c9a227;")
        self.deploy_audit_banner.hide()
        root.addWidget(self.deploy_audit_banner)

        self.path_hint = QLabel("")
        self.path_hint.setObjectName("subtitleLabel")
        self.path_hint.setWordWrap(True)
        root.addWidget(self.path_hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)

        # --- Left: game filter ---
        game_panel = QFrame()
        game_panel.setObjectName("controlPanel")
        game_panel.setMinimumWidth(180)
        game_panel.setMaximumWidth(260)
        game_layout = QVBoxLayout(game_panel)
        game_layout.setContentsMargins(10, 10, 10, 10)
        game_layout.setSpacing(8)
        game_label = QLabel("按游戏筛选")
        game_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        game_layout.addWidget(game_label)

        self.game_list = QListWidget()
        self.game_list.setObjectName("gameList")
        self.game_list.currentItemChanged.connect(self._on_game_item_changed)
        game_layout.addWidget(self.game_list)
        splitter.addWidget(game_panel)

        # --- Center: search / status / sort + card grid ---
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("librarySearchBox")
        self.search_box.setPlaceholderText(
            "搜索显示名 / Steam 名 / 备注 / Mod ID / 游戏名…"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumHeight(36)
        self.search_box.textChanged.connect(self._on_search_or_filter_changed)
        center_layout.addWidget(self.search_box)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[str, QPushButton] = {}
        for key, label in STATUS_FILTER_LABELS:
            btn = QPushButton(label)
            btn.setObjectName("libraryFilterChip")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if key == FILTER_ALL:
                btn.setChecked(True)
            self._filter_group.addButton(btn)
            self._filter_buttons[key] = btn
            filter_row.addWidget(btn)
            btn.toggled.connect(
                lambda checked, k=key: self._on_status_filter_toggled(k, checked)
            )
        filter_row.addStretch(1)
        center_layout.addLayout(filter_row)

        platform_row = QHBoxLayout()
        platform_row.setSpacing(6)
        self._platform_group = QButtonGroup(self)
        self._platform_group.setExclusive(True)
        self._platform_buttons: dict[str, QPushButton] = {}
        for key, label in PLATFORM_FILTER_LABELS:
            btn = QPushButton(label)
            btn.setObjectName("libraryFilterChip")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if key == FILTER_PLATFORM_ALL:
                btn.setChecked(True)
            self._platform_group.addButton(btn)
            self._platform_buttons[key] = btn
            self._filter_buttons[key] = btn  # back-compat lookup
            platform_row.addWidget(btn)
            btn.toggled.connect(
                lambda checked, k=key: self._on_platform_filter_toggled(k, checked)
            )
        platform_row.addStretch(1)

        tag_label = QLabel("标签")
        tag_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        platform_row.addWidget(tag_label)
        self.category_combo = QComboBox()
        self.category_combo.setObjectName("librarySortCombo")
        self.category_combo.addItem("全部标签", FILTER_CATEGORY_ALL)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        platform_row.addWidget(self.category_combo)

        sort_label = QLabel("排序")
        sort_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        platform_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("librarySortCombo")
        for key, label in SORT_LABELS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        platform_row.addWidget(self.sort_combo)
        center_layout.addLayout(platform_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.library_host = QWidget()
        self.library_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self.library_layout = FlowLayout(
            self.library_host, margin=4, h_spacing=14, v_spacing=14
        )
        self.scroll.setWidget(self.library_host)
        center_layout.addWidget(self.scroll, stretch=1)
        splitter.addWidget(center)

        # --- Right: detail panel (single instance for the page lifetime) ---
        self.detail_panel = ModDetailPanel()
        self.detail_panel.setMinimumWidth(360)
        self.detail_panel.setMaximumWidth(420)
        self.detail_panel.metadata_saved.connect(self._on_panel_metadata_saved)
        self.detail_panel.tags_saved.connect(self._on_panel_metadata_saved)
        self.detail_panel.deploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "deploy")
        )
        self.detail_panel.redeploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "redeploy")
        )
        self.detail_panel.undeploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "undeploy")
        )
        self.detail_panel.remove_requested.connect(self._on_remove_mod)
        self.detail_panel.offline_page_updated.connect(self._on_offline_page_updated)
        splitter.addWidget(self.detail_panel)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([GAME_PANEL_WIDTH, 700, DETAIL_PANEL_WIDTH])
        root.addWidget(splitter, stretch=1)

        # Empty-state overlay (title + next-step + action)
        self.empty_overlay = QFrame(self.library_host)
        self.empty_overlay.setObjectName("libraryEmptyOverlay")
        empty_layout = QVBoxLayout(self.empty_overlay)
        empty_layout.setContentsMargins(24, 32, 24, 32)
        empty_layout.setSpacing(10)
        empty_layout.addStretch(1)
        self.empty_title = QLabel()
        self.empty_title.setObjectName("libraryEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        self.empty_hint = QLabel()
        self.empty_hint.setObjectName("emptyLabel")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        empty_layout.addWidget(self.empty_hint)
        self.empty_action_btn = QPushButton()
        self.empty_action_btn.setObjectName("libraryEmptyAction")
        self.empty_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty_action_btn.clicked.connect(self._on_empty_action)
        empty_layout.addWidget(
            self.empty_action_btn, alignment=Qt.AlignmentFlag.AlignCenter
        )
        empty_layout.addStretch(1)
        self.empty_overlay.hide()
        self._empty_kind: str | None = None

        # Loading overlay (non-blocking feedback during refresh)
        self.loading_overlay = QFrame(self.scroll)
        self.loading_overlay.setObjectName("libraryLoadingOverlay")
        load_layout = QVBoxLayout(self.loading_overlay)
        load_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("正在加载 Mod 库…")
        self.loading_label.setObjectName("libraryLoadingLabel")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        load_layout.addWidget(self.loading_label)
        self.loading_overlay.hide()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_target_root(self, path: str) -> None:
        self._target_root = path.strip() or str(default_mod_library())
        self.path_hint.setText(f"库路径：{self._target_root}")

    def set_preferred_filter(self, name: str | None) -> None:
        self._pending_game_filter = name

    def current_filter(self) -> str:
        item = self.game_list.currentItem()
        if item is None:
            return ALL_GAMES_LABEL
        key = item.data(GAME_ROLE)
        if key is None or key == "":
            return ALL_GAMES_LABEL
        return str(key)

    def refresh(self) -> None:
        """Reload library listing from disk and rebuild cards (UI-thread safe).

        Read + render only — no network, Steam archive, migration, or sync.
        Shows a loading overlay so the window is never silent during work.
        """
        scroll = self._capture_scroll()
        selected_mid = ""
        if self._selected_card is not None:
            selected_mid = self._selected_card._mod_id() or ""
        self._set_loading(True)
        try:
            root = Path(self._target_root)
            root.mkdir(parents=True, exist_ok=True)
            manager = ModFileManager(root)

            previous = self._current_game_filter
            pending = self._pending_game_filter
            keep_path = self._selected_path
            self._rebuild_game_list(manager, prefer=pending or previous)
            self._render_mod_cards(manager)
            self._refresh_category_combo()

            if keep_path is not None:
                restored = self._card_for_path(keep_path)
                if restored is not None and not restored.isHidden():
                    self._select_card(restored, show_panel=True)
                else:
                    self._clear_selection()
                    self.detail_panel.clear()
            else:
                self.detail_panel.clear()
        finally:
            self._set_loading(False)
            self._restore_scroll_after_layout(
                scroll, focus_mod_id=selected_mid
            )

    def _capture_scroll(self) -> int:
        return int(self.scroll.verticalScrollBar().value())

    def _set_scroll_value(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(int(value), bar.maximum())))

    def _restore_scroll_after_layout(
        self,
        value: int,
        *,
        focus_mod_id: str = "",
    ) -> None:
        """Restore scrollbar after FlowLayout rebuild (next event-loop tick)."""

        def _apply() -> None:
            mid = str(focus_mod_id or "").strip()
            if mid:
                for _index, card in self._card_entries:
                    if card._mod_id() == mid and not card.isHidden():
                        self.scroll.ensureWidgetVisible(card, 16, 16)
                        return
            self._set_scroll_value(value)

        QTimer.singleShot(0, _apply)

    def get_current_game_context(self) -> dict[str, int | str] | None:
        """
        Return the active library game selection.

        ``None`` when ``全部游戏`` is selected (no reliable import target).
        May return ``game_id=0`` when the folder is selected but AppID is unknown.
        """
        name = (self.current_game_name or self._current_game_filter or "").strip()
        if not name or name == ALL_GAMES_LABEL:
            return None
        game_id = int(self.current_game_id or 0)
        if game_id <= 0:
            game_id = self._resolve_game_id(name)
            self.current_game_id = game_id or None
        return {"game_id": int(game_id or 0), "game_name": name}

    def _on_import_mod(self) -> None:
        """Open Mod Import Center; refresh library when import succeeds."""
        from .mod_import_dialog import ModImportDialog

        context = self.get_current_game_context()
        if context is None:
            QMessageBox.warning(
                self,
                "导入 Mod",
                "请先选择目标游戏后再导入 Mod。\n\n"
                "原因：GitHub / Nexus Mod 无法可靠判断所属游戏。",
            )
            return
        if int(context.get("game_id") or 0) <= 0:
            QMessageBox.warning(
                self,
                "导入 Mod",
                f"无法解析游戏「{context.get('game_name')}」的 AppID。\n"
                "请先通过 Steam 同步该游戏后再导入。",
            )
            return

        dialog = ModImportDialog(
            self._target_root,
            parent=self,
            game_context=context,
        )

        def _after_import(_result: object) -> None:
            self.refresh()

        dialog.imported.connect(_after_import)
        dialog.exec()

    def _resolve_game_id(self, game_name: str) -> int:
        """Map a library game folder name to ``games.app_id`` when possible."""
        name = (game_name or "").strip()
        if not name:
            return 0
        try:
            db = get_db()
            for game in db.list_games():
                candidates = {
                    str(getattr(game, "name", "") or "").strip(),
                    str(getattr(game, "folder_name", "") or "").strip(),
                    str(getattr(game, "display_name", "") or "").strip(),
                }
                if any(c.casefold() == name.casefold() for c in candidates if c):
                    return int(game.app_id or 0)
            manager = ModFileManager(self._target_root)
            for folder in manager.list_managed_mods(game_name=name):
                meta = manager.load_metadata(folder)
                if meta is not None and int(meta.app_id or 0) > 0:
                    return int(meta.app_id)
                mid = str(meta.published_file_id if meta else "").strip()
                if mid.isdigit():
                    info = db.get_mod_display_info(mid)
                    if info is not None and int(info.app_id or 0) > 0:
                        return int(info.app_id)
        except Exception:  # noqa: BLE001
            logger.debug("resolve game id failed for %s", name, exc_info=True)
        return 0

    def _set_current_game_context(self, game_name: str | None) -> None:
        name = (game_name or "").strip() or None
        if name == ALL_GAMES_LABEL:
            name = None
        self.current_game_name = name
        self._current_game_filter = name
        self.current_game_id = self._resolve_game_id(name) if name else None

    def _on_remove_mod(self, mod_id: str) -> None:
        """Handle DetailPanel remove_requested after user already confirmed."""
        mid = str(mod_id or "").strip()
        if not mid:
            return
        try:
            from services.mod_remove import ModRemover

            result = ModRemover(self._target_root).remove_mod(mid)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "移除失败", str(exc))
            return
        if not result.get("success"):
            QMessageBox.warning(
                self, "移除失败", str(result.get("error") or "未知错误")
            )
            return
        self.detail_panel.clear()
        self.refresh()

    def _on_offline_page_updated(self, mod_path: object) -> None:
        """Refresh only the affected card after a single-mod offline download."""
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is not None:
            card.refresh_display()
            self._refresh_mod_ui(card._mod_id())

    def run_deploy_audit(self) -> None:
        """
        Startup / on-demand scan of mods marked deployed.

        Existence checks only — never deletes or redeploys.
        """
        from services.deploy_audit import anomalies_only, scan_deployed_mods

        try:
            results = scan_deployed_mods(self._target_root, db=get_db())
        except Exception as exc:  # noqa: BLE001
            logger.warning("deploy audit failed: %s", exc)
            return
        self._deploy_audit = {r.mod_id: r for r in results}
        bad = anomalies_only(results)
        if bad:
            self.deploy_audit_banner.setText(
                f"部署一致性：发现 {len(bad)} 个异常（源缺失或清单/目标损坏）。"
                "不会自动修复，请打开详情查看。"
            )
            self.deploy_audit_banner.show()
        else:
            self.deploy_audit_banner.hide()
            self.deploy_audit_banner.clear()
        if self._selected_card is not None:
            self._apply_audit_to_panel(self._selected_card._mod_id())

    def _apply_audit_to_panel(self, mod_id: str) -> None:
        result = self._deploy_audit.get(str(mod_id))
        if result is None:
            self.detail_panel.set_audit_hint("", "")
            return
        self.detail_panel.set_audit_hint(
            getattr(result, "status", ""),
            getattr(result, "reason", ""),
        )

    def _set_loading(self, loading: bool) -> None:
        self._loading = bool(loading)
        self.refresh_btn.setEnabled(not loading)
        self.search_box.setEnabled(not loading)
        self.sort_combo.setEnabled(not loading)
        for btn in self._filter_buttons.values():
            btn.setEnabled(not loading)
        if loading:
            self.loading_overlay.setGeometry(self.scroll.rect())
            self.loading_overlay.show()
            self.loading_overlay.raise_()
        else:
            self.loading_overlay.hide()
        QApplication.processEvents()

    # ------------------------------------------------------------------
    # Search / status filter / sort
    # ------------------------------------------------------------------

    def _on_search_or_filter_changed(self, *_args) -> None:
        self._apply_view_filter()

    def _on_status_filter_toggled(self, key: str, checked: bool) -> None:
        if not checked:
            return
        self._status_filter = key
        self._apply_view_filter()

    def _on_platform_filter_toggled(self, key: str, checked: bool) -> None:
        if not checked:
            return
        self._platform_filter = key
        self._apply_view_filter()

    def _on_category_changed(self, _index: int = 0) -> None:
        data = self.category_combo.currentData()
        self._category_filter = str(data or FILTER_CATEGORY_ALL)
        self._apply_view_filter()

    def _refresh_category_combo(self) -> None:
        current = self._category_filter
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("全部标签", FILTER_CATEGORY_ALL)
        try:
            tags = get_db().list_all_category_tags()
        except Exception:  # noqa: BLE001
            tags = []
        for tag in tags:
            self.category_combo.addItem(tag, tag)
        # restore selection
        idx = 0
        for i in range(self.category_combo.count()):
            if str(self.category_combo.itemData(i) or "") == current:
                idx = i
                break
        self.category_combo.setCurrentIndex(idx)
        self.category_combo.blockSignals(False)
        self._category_filter = str(
            self.category_combo.currentData() or FILTER_CATEGORY_ALL
        )

    def _on_sort_changed(self, _index: int) -> None:
        self._sort_mode = str(self.sort_combo.currentData() or SORT_MTIME)
        self._apply_view_filter()

    def _build_filter_index(
        self,
        folder: Path,
        meta: ModMetadata,
        manager: ModFileManager,
        db_fields,
        tag_flags=None,
    ) -> ModFilterIndex:
        mid = str(meta.published_file_id or "").strip()
        game_fs = manager.game_name_for_path(folder)
        if db_fields is not None:
            display = db_fields.display_name
            steam = db_fields.steam_name
            notes = db_fields.user_notes
            favorite = db_fields.favorite
            deployed = db_fields.deploy_status == DEPLOY_STATUS_DEPLOYED
            game_db = db_fields.game_name or ""
        else:
            display = meta.effective_title() if meta else folder.name
            steam = (meta.title or "").strip() if meta else ""
            notes = ""
            favorite = False
            deployed = False
            game_db = ""
        game_name = game_db or game_fs or (meta.game_name if meta else "") or ""
        # Include both DB game name and folder name in searchable text
        game_search = " ".join(p for p in (game_db, game_fs) if p)
        invalid = bool(getattr(tag_flags, "invalid", False)) if tag_flags else False
        conflict = bool(getattr(tag_flags, "conflict", False)) if tag_flags else False
        tag_values = ""
        if tag_flags is not None:
            values = getattr(tag_flags, "tag_values", ()) or ()
            reason = getattr(tag_flags, "invalid_reason", "") or ""
            tag_values = " ".join(
                p for p in (*values, reason) if str(p).strip()
            )
        platform = "steam"
        source_url = ""
        external_id = ""
        is_invalid = False
        conflict_status = "none"
        enabled = True
        category_tags = ""
        if db_fields is not None:
            platform = getattr(db_fields, "platform", "steam") or "steam"
            source_url = getattr(db_fields, "source_url", "") or ""
            external_id = getattr(db_fields, "external_id", "") or ""
            is_invalid = bool(getattr(db_fields, "is_invalid", False))
            conflict_status = getattr(db_fields, "conflict_status", "none") or "none"
            enabled = bool(getattr(db_fields, "enabled", True))
            category_tags = getattr(db_fields, "category_tags", "") or ""
        # Prefer SQLite lifecycle flags; fall back to legacy tag flags
        invalid = is_invalid or (
            bool(getattr(tag_flags, "invalid", False)) if tag_flags else False
        )
        conflict = (conflict_status in ("conflict", "warning")) or (
            bool(getattr(tag_flags, "conflict", False)) if tag_flags else False
        )
        return ModFilterIndex(
            mod_id=mid,
            display_name=display,
            steam_name=steam,
            notes=notes,
            game_name=game_search or game_name,
            favorite=favorite,
            deployed=deployed,
            has_offline=offline_page_exists(folder),
            mtime=folder_mtime(folder),
            sort_name=display or steam or folder.name,
            invalid=invalid,
            conflict=conflict,
            tag_values=tag_values,
            platform=platform,
            source_url=source_url,
            external_id=external_id,
            is_invalid=is_invalid or invalid,
            conflict_status=conflict_status,
            enabled=enabled,
            category_tags=category_tags,
        )

    def _apply_view_filter(self) -> None:
        """Reorder / show-hide cards only — never recreates DetailPanel."""
        detail_id = id(self.detail_panel)

        # Detach cards from flow layout without destroying them
        while self.library_layout.count():
            item = self.library_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is None or widget is self.empty_overlay:
                continue
            widget.hide()
            widget.setParent(self.library_host)

        query = self.search_box.text()
        visible_cards = filter_and_sort(
            self._card_entries,
            query=query,
            filter_key=self._status_filter,
            platform_key=self._platform_filter,
            category_key=self._category_filter,
            sort_mode=self._sort_mode,
        )

        if self._selected_card is not None and self._selected_card not in visible_cards:
            self._clear_selection()
            self.detail_panel.clear()

        if not self._card_entries:
            game = self._current_game_filter
            if game:
                self._show_empty(
                    EMPTY_GAME,
                    title=f"「{game}」下还没有 Mod",
                    hint="切换到「全部游戏」，或到「同步中心」同步该游戏的 Workshop Mod。",
                    action="切换到全部游戏",
                )
            else:
                self._show_empty(
                    EMPTY_LIBRARY,
                    title="Mod 库为空",
                    hint="请先在「同步中心」同步创意工坊 Mod。\n"
                    "存储结构：mod / 游戏英文名 / Mod名称 / .info",
                    action="前往同步中心",
                )
            self.count_label.setText("0 个")
            assert id(self.detail_panel) == detail_id
            return

        if not visible_cards:
            self._show_empty(
                EMPTY_SEARCH,
                title="没有符合条件的 Mod",
                hint="尝试清除搜索关键字，或切换过滤条件为「全部」。",
                action="清除搜索与过滤",
            )
            self.count_label.setText("0 个")
            assert id(self.detail_panel) == detail_id
            return

        self.empty_overlay.hide()
        self._empty_kind = None
        for card in visible_cards:
            assert isinstance(card, ModCardWidget)
            self._reveal_card(card)

        self.count_label.setText(f"{len(visible_cards)} 个")
        self.library_layout.invalidate()
        self.library_host.updateGeometry()
        self.scroll.updateGeometry()
        assert id(self.detail_panel) == detail_id

    def _reveal_card(self, card: ModCardWidget) -> None:
        """Put ``card`` into the flow layout, then show — never parentless show()."""
        # addWidget reparents onto library_host; do this BEFORE show().
        self.library_layout.addWidget(card)
        if card.parent() is None:
            logger.warning(
                "[UI BUG] ModCardWidget has no parent before show path=%s",
                card.managed_path,
            )
            return
        card.show()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def on_mod_selected(self, mod_path: object) -> None:
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is None or card.isHidden():
            return
        self._select_card(card, show_panel=True)

    def _select_card(self, card: ModCardWidget, *, show_panel: bool) -> None:
        if self._selected_card is not None and self._selected_card is not card:
            self._selected_card.remove_selected_style()
        self._selected_card = card
        self._selected_path = card.managed_path
        card.add_selected_style()
        if show_panel:
            self._sync_peer_mods_to_panel(exclude=card._mod_id())
            self.detail_panel.show_mod(card.managed_path)
            self._apply_audit_to_panel(card._mod_id())

    def _sync_peer_mods_to_panel(self, *, exclude: str = "") -> None:
        peers: list[dict] = []
        for index, card in self._card_entries:
            mid = card._mod_id() or index.mod_id
            if not mid or mid == exclude:
                continue
            peers.append(
                {
                    "mod_id": mid,
                    "title": index.display_name or mid,
                    "platform": getattr(index, "platform", "steam") or "steam",
                    "game_name": (index.game_name or "").split()[0]
                    if index.game_name
                    else "",
                }
            )
        self.detail_panel.set_peer_mods(peers)

    def _clear_selection(self) -> None:
        if self._selected_card is not None:
            self._selected_card.remove_selected_style()
        self._selected_card = None
        self._selected_path = None

    def _card_for_path(self, path: Path) -> ModCardWidget | None:
        try:
            target = path.resolve()
        except OSError:
            target = path
        for card in self._cards:
            try:
                if card.managed_path.resolve() == target:
                    return card
            except OSError:
                if card.managed_path == path:
                    return card
        return None

    def _rebuild_card_index(
        self,
        folder: Path,
        meta: ModMetadata,
        manager: ModFileManager,
    ) -> ModFilterIndex:
        mid = str(meta.published_file_id or "").strip()
        fields_map = get_db().get_mods_search_fields([mid]) if mid.isdigit() else {}
        tag_map = get_db().get_mods_tag_flags([mid]) if mid.isdigit() else {}
        return self._build_filter_index(
            folder,
            meta,
            manager,
            fields_map.get(mid),
            tag_flags=tag_map.get(mid),
        )

    def _on_panel_metadata_saved(self, mod_path: object) -> None:
        """Refresh only the affected card + panel — do not rescan the library."""
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is not None:
            card.refresh_display()
            # Rebuild index for this card so search/favorite filters stay correct
            manager = ModFileManager(self._target_root)
            meta = manager.load_metadata(path) or card.metadata or ModMetadata(
                published_file_id=path.name if path.name.isdigit() else "",
                title="",
                managed_path=str(path),
            )
            new_index = self._rebuild_card_index(path, meta, manager)
            for i, (_old, c) in enumerate(self._card_entries):
                if c is card:
                    self._card_entries[i] = (new_index, card)
                    break
            self._apply_view_filter()
            if self._selected_card is card and not card.isHidden():
                self._sync_peer_mods_to_panel(exclude=card._mod_id())
                self.detail_panel.show_mod(path)

    # ------------------------------------------------------------------
    # Deploy (QThread — never call ModDeployer on the UI thread)
    # ------------------------------------------------------------------

    def _on_deploy_action(self, mod_id: str, action: str = "deploy") -> None:
        mid = str(mod_id).strip()
        if not mid.isdigit():
            self.detail_panel.apply_deploy_failure("缺少有效的 Mod ID。")
            return
        if self._deploy_worker is not None and self._deploy_worker.isRunning():
            return

        # Phase 5: user-tag warnings — hint only, never blocks deploy.
        if action in ("deploy", "redeploy"):
            self._apply_tag_deploy_hint(mid)

        self._deploy_mod_id = mid
        worker = DeployWorker(
            mid,
            library_root=self._target_root,
            parent=self,
            action=action,  # type: ignore[arg-type]
        )
        worker.deploy_started.connect(
            lambda a=action: self._on_deploy_started(a)
        )
        worker.deploy_finished.connect(self._on_deploy_finished)
        worker.deploy_failed.connect(self._on_deploy_failed)
        worker.finished.connect(self._on_deploy_thread_finished)
        self._deploy_worker = worker
        worker.start()

    def _on_remove_mod(self, mod_id: str) -> None:
        mid = str(mod_id).strip()
        if not mid:
            return
        from services.mod_remove import ModRemover

        result = ModRemover(self._target_root, db=get_db()).remove_mod(mid)
        if not result.get("success"):
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "删除失败",
                str(result.get("error") or "未知错误"),
            )
            return
        self.detail_panel.clear()
        self.refresh()

    def _apply_tag_deploy_hint(self, mod_id: str) -> None:
        hints: list[str] = []
        try:
            flags = get_db().get_mods_tag_flags([mod_id]).get(str(mod_id))
        except Exception:  # noqa: BLE001
            flags = None
        if flags is not None:
            if flags.invalid:
                reason = (flags.invalid_reason or "").strip()
                hints.append(
                    "该 Mod 已标记为失效"
                    + (f"（{reason}）" if reason else "")
                )
            if flags.conflict:
                hints.append("该 Mod 存在冲突标记")
        self.detail_panel.set_tag_deploy_hint("；".join(hints))

    def _on_deploy_requested(self, mod_id: str) -> None:
        self._on_deploy_action(mod_id, "deploy")

    def _on_deploy_started(self, action: str = "deploy") -> None:
        self.detail_panel.set_deploy_busy(True, action=action)

    def _on_deploy_finished(self, result: object) -> None:
        data = result if isinstance(result, dict) else {"success": False, "error": str(result)}
        mid = self._deploy_mod_id or str(data.get("mod_id") or "")
        self.detail_panel.apply_deploy_result(data)
        # Status stays in DetailPanel — no modal dialogs.
        focus = mid if data.get("success") else ""
        self._refresh_mod_ui(mid, focus_mod_id=focus)

    def _on_deploy_failed(self, error: str) -> None:
        self.detail_panel.apply_deploy_failure(error)
        self._refresh_mod_ui(self._deploy_mod_id or "")

    def _on_deploy_thread_finished(self) -> None:
        self._deploy_worker = None
        self._deploy_mod_id = None

    def _refresh_mod_ui(self, mod_id: str, *, focus_mod_id: str = "") -> None:
        """Update only the matching card + detail panel (no library.refresh)."""
        if not mod_id:
            return
        scroll = self._capture_scroll()
        for i, (index, card) in enumerate(self._card_entries):
            if card._mod_id() != str(mod_id):
                continue
            card.refresh_display()
            manager = ModFileManager(self._target_root)
            meta = manager.load_metadata(card.managed_path) or card.metadata
            if meta is None:
                meta = ModMetadata(
                    published_file_id=mod_id,
                    title="",
                    managed_path=str(card.managed_path),
                )
            fields_map = get_db().get_mods_search_fields([mod_id])
            tag_map = get_db().get_mods_tag_flags([mod_id])
            self._card_entries[i] = (
                self._build_filter_index(
                    card.managed_path,
                    meta,
                    manager,
                    fields_map.get(mod_id),
                    tag_flags=tag_map.get(mod_id),
                ),
                card,
            )
            self._apply_view_filter()
            if self._selected_card is card and not card.isHidden():
                if not self.detail_panel._deploy_busy:
                    self._sync_peer_mods_to_panel(exclude=card._mod_id())
                    self.detail_panel.show_mod(card.managed_path)
            self._restore_scroll_after_layout(
                scroll, focus_mod_id=focus_mod_id or ""
            )
            break
        else:
            self._restore_scroll_after_layout(
                scroll, focus_mod_id=focus_mod_id or ""
            )

    # ------------------------------------------------------------------
    # Game list / cards
    # ------------------------------------------------------------------

    def _rebuild_game_list(
        self,
        manager: ModFileManager,
        prefer: str | None = None,
    ) -> None:
        self.game_list.blockSignals(True)
        self.game_list.clear()

        total = len(manager.list_managed_mods())
        all_item = QListWidgetItem(f"{ALL_GAMES_LABEL}  ·  {total}")
        all_item.setData(GAME_ROLE, "")
        self.game_list.addItem(all_item)

        for name in manager.list_games():
            count = len(manager.list_managed_mods(game_name=name))
            item = QListWidgetItem(f"{name}  ·  {count}")
            item.setData(GAME_ROLE, name)
            self.game_list.addItem(item)

        prefer_key = prefer or ""
        if prefer_key == ALL_GAMES_LABEL:
            prefer_key = ""

        target_row = 0
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            if item is None:
                continue
            key = item.data(GAME_ROLE) or ""
            if key == prefer_key:
                target_row = i
                break

        self.game_list.setCurrentRow(target_row)
        current = self.game_list.currentItem()
        key = (current.data(GAME_ROLE) if current else "") or ""
        self._set_current_game_context(key or None)
        self._pending_game_filter = None
        self.game_list.blockSignals(False)

    def _on_game_item_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        key = ""
        if current is not None:
            key = current.data(GAME_ROLE) or ""
        self._set_current_game_context(key or None)
        manager = ModFileManager(self._target_root)
        self._clear_selection()
        self.detail_panel.clear()
        self._render_mod_cards(manager)
        self.filter_changed.emit(key or ALL_GAMES_LABEL)

    def _render_mod_cards(self, manager: ModFileManager) -> None:
        self._clear_cards()
        game = self._current_game_filter
        folders = manager.list_managed_mods(game_name=game)

        if not folders:
            self._apply_view_filter()
            return

        metas: list[tuple[Path, ModMetadata]] = []
        mod_ids: list[str] = []
        for folder in folders:
            meta = manager.load_metadata(folder)
            if meta is None:
                meta = ModMetadata(
                    published_file_id=folder.name if folder.name.isdigit() else "",
                    title="",
                    managed_path=str(folder),
                )
            manager.enrich_title_from_db(meta)
            metas.append((folder, meta))
            if str(meta.published_file_id).isdigit():
                mod_ids.append(str(meta.published_file_id))

        try:
            fields_map = get_db().get_mods_search_fields(mod_ids)
        except Exception:  # noqa: BLE001
            fields_map = {}
        try:
            tag_flags_map = get_db().get_mods_tag_flags(mod_ids)
        except Exception:  # noqa: BLE001
            tag_flags_map = {}

        for folder, meta in metas:
            mid = str(meta.published_file_id or "")
            # Always parent to library_host — never parent=None then show().
            card = ModCardWidget(folder, metadata=meta, parent=self.library_host)
            card.hide()  # stay invisible until _reveal_card / layout
            card.selection_requested.connect(self.on_mod_selected)
            card.edit_requested.connect(self._on_card_edit_requested)
            card.deploy_requested.connect(self._on_deploy_requested)
            card.open_folder_requested.connect(self._on_card_open_folder)
            card.open_steam_requested.connect(self._on_card_open_steam)
            card.favorite_toggle_requested.connect(self._on_card_favorite_toggle)
            index = self._build_filter_index(
                folder,
                meta,
                manager,
                fields_map.get(mid),
                tag_flags=tag_flags_map.get(mid),
            )
            self._cards.append(card)
            self._card_entries.append((index, card))

        self._apply_view_filter()

    def _clear_cards(self) -> None:
        self._clear_selection()
        while self.library_layout.count():
            item = self.library_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.empty_overlay:
                # hide BEFORE setParent(None) — otherwise Qt promotes to top-level
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for _index, card in self._card_entries:
            card.hide()
            if card.parent() is not None:
                card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._card_entries.clear()

    def _show_empty(
        self,
        kind: str,
        *,
        title: str,
        hint: str,
        action: str,
    ) -> None:
        self._empty_kind = kind
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.empty_action_btn.setText(action)
        self.empty_action_btn.setVisible(True)
        self.empty_overlay.setGeometry(self.library_host.rect())
        self.empty_overlay.show()
        self.empty_overlay.raise_()

    def _on_empty_action(self) -> None:
        kind = self._empty_kind
        if kind == EMPTY_SEARCH:
            self.search_box.clear()
            if FILTER_ALL in self._filter_buttons:
                self._filter_buttons[FILTER_ALL].setChecked(True)
            self._status_filter = FILTER_ALL
            self._apply_view_filter()
        elif kind == EMPTY_GAME:
            if self.game_list.count() > 0:
                self.game_list.setCurrentRow(0)
        elif kind == EMPTY_LIBRARY:
            self.request_open_sync.emit()

    def _on_card_edit_requested(self, mod_path: object) -> None:
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is None:
            return
        self._select_card(card, show_panel=True)
        self.detail_panel.enter_edit()

    def _on_card_open_folder(self, mod_path: object) -> None:
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is not None:
            self._select_card(card, show_panel=True)
        self.detail_panel._open_folder()

    def _on_card_open_steam(self, mod_path: object) -> None:
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is not None:
            self._select_card(card, show_panel=True)
        self.detail_panel._open_steam()

    def _on_card_favorite_toggle(self, mod_id: str) -> None:
        mid = str(mod_id).strip()
        if not mid.isdigit():
            return
        try:
            info = get_db().get_mod_display_info(mid)
            current = bool(info.favorite) if info else False
            get_db().update_mod_user_metadata(
                mid,
                {
                    "display_name": (info.user_display_name if info else ""),
                    "custom_description": (info.custom_description if info else ""),
                    "user_notes": (info.user_notes if info else ""),
                    "favorite": not current,
                },
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "收藏失败", str(exc))
            return
        # Refresh only this card + index (no full library rescan)
        for i, (_idx, card) in enumerate(self._card_entries):
            if card._mod_id() != mid:
                continue
            card.refresh_display()
            manager = ModFileManager(self._target_root)
            meta = manager.load_metadata(card.managed_path) or card.metadata
            if meta is None:
                meta = ModMetadata(
                    published_file_id=mid,
                    title="",
                    managed_path=str(card.managed_path),
                )
            fields = get_db().get_mods_search_fields([mid])
            tags = get_db().get_mods_tag_flags([mid])
            self._card_entries[i] = (
                self._build_filter_index(
                    card.managed_path,
                    meta,
                    manager,
                    fields.get(mid),
                    tag_flags=tags.get(mid),
                ),
                card,
            )
            self._apply_view_filter()
            if self._selected_card is card and not card.isHidden():
                self.detail_panel.show_mod(card.managed_path)
            break

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.empty_overlay.isVisible():
            self.empty_overlay.setGeometry(self.library_host.rect())
        if self.loading_overlay.isVisible():
            self.loading_overlay.setGeometry(self.scroll.rect())
