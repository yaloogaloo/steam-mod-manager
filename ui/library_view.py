"""Mod Library view — game filter + card grid + detail panel."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QCoreApplication, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
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
    FILTER_OFFLINE_MISSING,
    FILTER_PLATFORM_ALL,
    SORT_LABELS,
    SORT_MTIME,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
    filter_and_sort,
    folder_mtime,
    offline_page_exists,
)
from .mod_card import CARD_WIDTH, ModCardWidget
from .mod_detail_panel import ModDetailPanel

logger = logging.getLogger(__name__)

ALL_GAMES_LABEL = "全部游戏"
GAME_PANEL_MIN = 120
GAME_PANEL_MAX = 180
GAME_PANEL_WIDTH = 140
DETAIL_PANEL_MIN = 350
DETAIL_PANEL_PREFERRED = 360
DETAIL_PANEL_MAX = 420
# Default splitter sizes: slim game column, wide Mod grid, compact detail
SPLITTER_DEFAULT_SIZES = (140, 720, 360)
# FlowLayout wraps by width — reserve room for 4 cards + spacing + chrome
LIBRARY_CARDS_PER_ROW = 4
LIBRARY_CARD_H_SPACING = 8
LIBRARY_FLOW_MARGIN = 2
LIBRARY_CENTER_MIN_WIDTH = (
    LIBRARY_CARDS_PER_ROW * CARD_WIDTH
    + (LIBRARY_CARDS_PER_ROW - 1) * LIBRARY_CARD_H_SPACING
    + 2 * LIBRARY_FLOW_MARGIN
    + 24
)
GAME_ROLE = Qt.ItemDataRole.UserRole
GAME_ID_ROLE = Qt.ItemDataRole.UserRole + 1
GAME_CATEGORY_ROLE = Qt.ItemDataRole.UserRole + 2

EMPTY_LIBRARY = "empty_library"
EMPTY_GAME = "empty_game"
EMPTY_SEARCH = "empty_search"


class _GameFilterRow(QWidget):
    """Steam-sidebar row: optional chevron + game name + weak secondary count."""

    def __init__(
        self,
        name: str,
        count: int,
        *,
        show_count: bool = True,
        indent: bool = False,
        expandable: bool = False,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("gameFilterRow")
        self.setMinimumHeight(28)
        self.expandable = bool(expandable)
        layout = QHBoxLayout(self)
        left = 8 + (14 if indent else 0)
        layout.setContentsMargins(left, 4, 8, 4)
        layout.setSpacing(6)
        self.chevron_label = QLabel("")
        self.chevron_label.setObjectName("gameListChevron")
        self.chevron_label.setFixedWidth(12)
        if self.expandable:
            layout.addWidget(self.chevron_label)
            self.set_expanded(expanded)
        else:
            self.chevron_label.hide()
        self.name_label = QLabel(name)
        self.name_label.setObjectName("gameListName")
        self.count_label = QLabel(str(count))
        self.count_label.setObjectName("gameListCount")
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.name_label, stretch=1)
        if show_count:
            layout.addWidget(self.count_label)
        else:
            self.count_label.hide()

    def set_expanded(self, expanded: bool) -> None:
        if not self.expandable:
            return
        self.chevron_label.setText("▾" if expanded else "▸")
        self.chevron_label.setToolTip("收起分类" if expanded else "展开分类")


class ModLibraryView(QWidget):
    """View B: browse managed mods under the local library (3-column workspace)."""

    filter_changed = Signal(str)
    request_open_sync = Signal()  # optional: MainWindow may ignore

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: list[ModCardWidget] = []
        self._card_entries: list[tuple[ModFilterIndex, ModCardWidget]] = []
        self._card_cache: dict[str, ModCardWidget] = {}
        self._card_create_count = 0
        self._card_reuse_count = 0
        self._selected_card: ModCardWidget | None = None
        self._selected_path: Path | None = None
        self._selected_cards: list[ModCardWidget] = []
        self._selection_anchor: ModCardWidget | None = None
        self._last_clicked_index = 0
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
        self._sidebar_category: str | None = None
        self._expanded_games: set[str] = set()
        self._sort_mode = SORT_MTIME
        self._loading = False
        self._deploy_audit: dict[str, object] = {}
        self._splitter_defaults_applied = False

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # --- D-1 Header: title + count + compact actions ---
        header = QHBoxLayout()
        header.setSpacing(10)
        self._page_title = QLabel("Mod 库")
        self._page_title.setObjectName("pageTitle")
        self.count_label = QLabel("0 Mods")
        self.count_label.setObjectName("subtitleLabel")
        self.import_btn = QPushButton("导入 Mod")
        self.import_btn.setObjectName("libraryHeaderButton")
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.import_btn.setToolTip("导入单个 Mod，或批量导入父目录下的多个 Mod")
        self._import_menu = QMenu(self.import_btn)
        self._import_menu.setObjectName("libraryImportMenu")
        act_single = QAction("导入单个 Mod（文件/压缩包）", self._import_menu)
        act_single.triggered.connect(self._on_import_single_mod)
        act_batch = QAction("批量导入目录（多 Mod）", self._import_menu)
        act_batch.triggered.connect(self._on_import_batch_directory)
        self._import_menu.addAction(act_single)
        self._import_menu.addAction(act_batch)
        self.import_btn.setMenu(self._import_menu)
        self._batch_import_worker = None
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setObjectName("libraryHeaderButton")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.refresh_btn.setToolTip("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._page_title)
        header.addWidget(self.count_label)
        header.addStretch(1)
        header.addWidget(self.import_btn)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        # D-2: anomaly banner — shown only when deploy audit finds issues
        self.deploy_audit_banner = QLabel("")
        self.deploy_audit_banner.setObjectName("warningBanner")
        self.deploy_audit_banner.setWordWrap(True)
        self.deploy_audit_banner.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.deploy_audit_banner.hide()
        root.addWidget(self.deploy_audit_banner)

        # D-2: library path — kept for API/tooltip; never in first-screen layout
        self.path_hint = QLabel("", self)
        self.path_hint.setObjectName("pathHintLabel")
        self.path_hint.setWordWrap(True)
        self.path_hint.hide()

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(3)
        self.splitter.setObjectName("librarySplitter")

        # --- D-3 Left: Steam-style game filter (stable width band) ---
        game_panel = QFrame()
        game_panel.setObjectName("gameFilterPanel")
        game_panel.setMinimumWidth(GAME_PANEL_MIN)
        game_panel.setMaximumWidth(GAME_PANEL_MAX)
        game_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        game_layout = QVBoxLayout(game_panel)
        game_layout.setContentsMargins(6, 8, 6, 8)
        game_layout.setSpacing(4)

        self.game_list = QListWidget()
        self.game_list.setObjectName("gameList")
        self.game_list.setSpacing(2)
        self.game_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.game_list.customContextMenuRequested.connect(
            self._on_game_list_context_menu
        )
        self.game_list.currentItemChanged.connect(self._on_game_item_changed)
        self.game_list.itemClicked.connect(self._on_game_item_clicked)
        game_layout.addWidget(self.game_list)
        self.splitter.addWidget(game_panel)

        # --- D-4 Center: filter zone ≤ 2 rows + dense card grid ---
        center = QWidget()
        center.setObjectName("libraryCenter")
        center.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        center.setMinimumWidth(LIBRARY_CENTER_MIN_WIDTH)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 0, 4, 0)
        center_layout.setSpacing(6)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("librarySearchBox")
        self.search_box.setPlaceholderText(
            "搜索显示名 / Steam 名 / 备注 / Mod ID / 游戏名…"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumHeight(32)
        self.search_box.textChanged.connect(self._on_search_or_filter_changed)
        center_layout.addWidget(self.search_box)

        # --- Responsive filter toolbar (FlowLayout wraps; chips never compress) ---
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[str, QPushButton] = {}

        self._status_bar = QWidget()
        self._status_bar.setObjectName("libraryFilterBar")
        self._status_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        status_flow = FlowLayout(
            self._status_bar, margin=0, h_spacing=6, v_spacing=6
        )
        for key, label in STATUS_FILTER_LABELS:
            btn = self._make_filter_chip(label)
            btn.setCheckable(True)
            if key == FILTER_ALL:
                btn.setChecked(True)
            self._filter_group.addButton(btn)
            self._filter_buttons[key] = btn
            btn.toggled.connect(
                lambda checked, k=key: self._on_status_filter_toggled(k, checked)
            )
            # Keep offline-missing for filter API / tests; not shown in toolbar.
            if key == FILTER_OFFLINE_MISSING:
                btn.setParent(self)
                btn.hide()
            else:
                status_flow.addWidget(btn)
        center_layout.addWidget(self._status_bar)

        self._platform_group = QButtonGroup(self)
        self._platform_group.setExclusive(True)
        self._platform_buttons: dict[str, QPushButton] = {}
        self._platform_bar = QWidget()
        self._platform_bar.setObjectName("libraryFilterBar")
        self._platform_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self._platform_flow = FlowLayout(
            self._platform_bar, margin=0, h_spacing=6, v_spacing=6
        )
        center_layout.addWidget(self._platform_bar)
        self._rebuild_platform_filter_bar()

        # Tag / sort — own flow row so they wrap as whole groups, never overlap chips
        self._meta_bar = QWidget()
        self._meta_bar.setObjectName("libraryFilterBar")
        self._meta_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        meta_flow = FlowLayout(self._meta_bar, margin=0, h_spacing=10, v_spacing=6)

        tag_group = QWidget()
        tag_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        tag_row = QHBoxLayout(tag_group)
        tag_row.setContentsMargins(0, 0, 0, 0)
        tag_row.setSpacing(6)
        tag_label = QLabel("标签")
        tag_label.setObjectName("fieldCaption")
        tag_row.addWidget(tag_label)
        self.category_combo = QComboBox()
        self.category_combo.setObjectName("librarySortCombo")
        self.category_combo.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.category_combo.setMinimumWidth(110)
        self.category_combo.addItem("全部标签", FILTER_CATEGORY_ALL)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        tag_row.addWidget(self.category_combo)
        meta_flow.addWidget(tag_group)

        sort_group = QWidget()
        sort_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sort_row = QHBoxLayout(sort_group)
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_row.setSpacing(6)
        sort_label = QLabel("排序")
        sort_label.setObjectName("fieldCaption")
        sort_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("librarySortCombo")
        self.sort_combo.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.sort_combo.setMinimumWidth(110)
        for key, label in SORT_LABELS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        sort_row.addWidget(self.sort_combo)
        meta_flow.addWidget(sort_group)
        center_layout.addWidget(self._meta_bar)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("libraryScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.library_host = QWidget()
        self.library_host.setObjectName("libraryHost")
        self.library_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        # D-5: tighter card grid density
        self.library_layout = FlowLayout(
            self.library_host,
            margin=LIBRARY_FLOW_MARGIN,
            h_spacing=LIBRARY_CARD_H_SPACING,
            v_spacing=8,
        )
        # Keep host min-height in sync with flow content so the scrollbar
        # range shrinks when switching to a smaller Mod set.
        self.library_layout.heightChanged.connect(self._on_library_flow_height)
        self.scroll.setWidget(self.library_host)
        center_layout.addWidget(self.scroll, stretch=1)
        self._shortcut_select_all = QShortcut(QKeySequence.StandardKey.SelectAll, self)
        self._shortcut_select_all.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._shortcut_select_all.activated.connect(self.select_all_mods)
        self.splitter.addWidget(center)

        # --- Right: detail panel (single instance for the page lifetime) ---
        self.detail_panel = ModDetailPanel()
        # Cap width so the column cannot overflow the window / clip footer actions.
        self.detail_panel.setMinimumWidth(DETAIL_PANEL_MIN)
        self.detail_panel.setMaximumWidth(DETAIL_PANEL_MAX)
        self.detail_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self.detail_panel.metadata_saved.connect(self._on_panel_metadata_saved)
        self.detail_panel.tags_saved.connect(self._on_panel_metadata_saved)
        self.detail_panel.batch_platform_saved.connect(self._on_batch_platform_saved)
        self.detail_panel.deploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "deploy")
        )
        self.detail_panel.redeploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "redeploy")
        )
        self.detail_panel.undeploy_requested.connect(
            lambda mid: self._on_deploy_action(mid, "undeploy")
        )
        self.detail_panel.offline_page_updated.connect(self._on_offline_page_updated)
        self.splitter.addWidget(self.detail_panel)

        # Center absorbs flex; detail keeps min width and may grow (never collapse)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes(list(SPLITTER_DEFAULT_SIZES))
        root.addWidget(self.splitter, stretch=1)

        # D-7: Empty-state overlay (productized)
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

        # D-8: Loading overlay — center scroll area only (not whole page)
        self.loading_overlay = QFrame(self.scroll.viewport())
        self.loading_overlay.setObjectName("libraryLoadingOverlay")
        load_layout = QVBoxLayout(self.loading_overlay)
        load_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Loading mods...")
        self.loading_label.setObjectName("libraryLoadingLabel")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        load_layout.addWidget(self.loading_label)
        self.loading_overlay.hide()

    @staticmethod
    def _make_filter_chip(label: str) -> QPushButton:
        """Filter chip with Fixed size — FlowLayout wraps instead of compressing."""
        btn = QPushButton(label)
        btn.setObjectName("libraryFilterChip")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        btn.setMinimumHeight(28)
        # sizeHint from text+style; Fixed policy prevents QLayout squeeze.
        btn.adjustSize()
        return btn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_target_root(self, path: str) -> None:
        self._target_root = path.strip() or str(default_mod_library())
        hint = f"库路径：{self._target_root}"
        self.path_hint.setText(hint)
        self.path_hint.hide()  # D-2: never consume first-screen height
        self._page_title.setToolTip(hint)
        self.refresh_btn.setToolTip(hint)

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
        keep_path = self._selected_path
        keep_mod_id = (
            self._selected_card._mod_id() if self._selected_card is not None else ""
        )
        focus_id = keep_mod_id
        self._set_loading(True)
        try:
            root = Path(self._target_root)
            root.mkdir(parents=True, exist_ok=True)
            manager = ModFileManager(root)

            previous = self._current_game_filter
            pending = self._pending_game_filter
            self._rebuild_game_list(manager, prefer=pending or previous)
            self._render_mod_cards(manager)
            self._refresh_category_combo()

            if keep_path is not None:
                restored = self._card_for_path(keep_path)
                if restored is not None and not restored.isHidden():
                    self._select_card(restored, show_panel=True)
                    focus_id = restored._mod_id() or keep_mod_id
                else:
                    self._clear_selection()
                    self.detail_panel.clear()
            else:
                self.detail_panel.clear()
        finally:
            self._set_loading(False)
            self._restore_scroll_after_layout(scroll, focus_mod_id=focus_id)

    def _capture_scroll(self) -> int:
        return int(self.scroll.verticalScrollBar().value())

    def _set_scroll_value(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(int(value), bar.maximum())))

    def _on_library_flow_height(self, height: int) -> None:
        """Shrink/grow library_host with FlowLayout content (scrollbar range)."""
        self.library_host.setMinimumHeight(max(0, int(height)))

    def _sync_library_host_size(self) -> None:
        """Force scroll host height to match current flow content width."""
        vp = self.scroll.viewport()
        viewport_w = int(vp.width()) if vp is not None else 0
        viewport_h = int(vp.height()) if vp is not None else 0
        if viewport_w <= 0:
            viewport_w = max(int(self.library_host.width()), 200)
        content_h = int(self.library_layout.heightForWidth(viewport_w))
        self.library_host.setMinimumHeight(max(0, content_h))
        self.library_layout.invalidate()
        self.library_host.updateGeometry()
        self.scroll.updateGeometry()
        # QScrollArea + widgetResizable may keep a stale tall geometry after
        # switching to fewer cards — explicitly shrink to content (or viewport).
        if vp is not None and self.scroll.widget() is self.library_host:
            needed = max(content_h, viewport_h)
            if self.library_host.height() > needed:
                self.library_host.resize(viewport_w, needed)

    def _card_for_mod_id(self, mod_id: str) -> ModCardWidget | None:
        mid = str(mod_id or "").strip()
        if not mid:
            return None
        for _index, card in self._card_entries:
            if card._mod_id() == mid and not card.isHidden():
                return card
        return None

    def _restore_scroll_after_layout(
        self,
        value: int,
        *,
        focus_mod_id: str = "",
    ) -> None:
        """Restore scrollbar / focus mod after FlowLayout rebuild settles."""

        def _apply() -> None:
            mid = str(focus_mod_id or "").strip()
            card = self._card_for_mod_id(mid) if mid else None
            if card is not None:
                # Prefer locating the target mod when it still exists.
                if self._selected_card is not card:
                    self._select_card(card, show_panel=False)
                self.scroll.ensureWidgetVisible(card, 16, 16)
                return
            self._set_scroll_value(value)

        # Immediate + deferred: filter rebuild can clamp scrollbar to bottom
        # before FlowLayout finishes computing final geometry.
        _apply()
        QTimer.singleShot(0, _apply)
        QTimer.singleShot(50, _apply)

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

    def _require_import_game_context(self) -> dict[str, int | str] | None:
        """Validate current game selection for Nexus / GitHub import."""
        context = self.get_current_game_context()
        if context is None:
            QMessageBox.warning(
                self,
                "导入 Mod",
                "请先选择目标游戏后再导入 Mod。\n\n"
                "原因：GitHub / Nexus Mod 无法可靠判断所属游戏。",
            )
            return None
        if int(context.get("game_id") or 0) <= 0:
            QMessageBox.warning(
                self,
                "导入 Mod",
                f"无法解析游戏「{context.get('game_name')}」的 AppID。\n"
                "请先在「游戏部署」中为该游戏填写有效的 Steam AppID。",
            )
            return None
        return context

    def _on_import_mod(self) -> None:
        """Backward-compatible entry: open the single-Mod import dialog."""
        self._on_import_single_mod()

    def _on_import_single_mod(self) -> None:
        """Option A: existing single Mod / archive import dialog (asks for links)."""
        from .mod_import_dialog import ModImportDialog

        context = self._require_import_game_context()
        if context is None:
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

    def _on_import_batch_directory(self) -> None:
        """
        Option B: pick a parent folder → silent multi-Mod directory import.

        Forces ``is_batch_mode=True`` so the pipeline skips source-URL prompts.
        """
        from core.mod_platform import PLATFORM_NEXUS
        from services.importers.directory_batch import discover_mod_directories
        from services.importers.import_settings import (
            resolve_import_start_directory,
            set_last_import_directory,
        )
        from ui.import_thread import ImportWorker

        context = self._require_import_game_context()
        if context is None:
            return

        if self._batch_import_worker is not None and self._batch_import_worker.isRunning():
            QMessageBox.information(self, "导入进行中", "请等待当前批量导入完成。")
            return

        start = resolve_import_start_directory()
        parent = QFileDialog.getExistingDirectory(
            self,
            "选择包含多个 Mod 子目录的父目录",
            start,
        )
        if not parent:
            return
        set_last_import_directory(parent)

        mod_dirs = discover_mod_directories(parent)
        if len(mod_dirs) <= 1:
            QMessageBox.warning(
                self,
                "批量导入",
                "未检测到多个独立 Mod 子目录。\n\n"
                "请选择包含多个 Mod 文件夹的父目录；"
                "单个 Mod 请使用「导入单个 Mod」。",
            )
            return

        params = {
            "folder": parent,
            "source_path": "",
            "use_archive": False,
            "archive_paths": [],
            "nexus_url": "",
            "nexus_id": "",
            "title": "",
            "cover_source": "",
            "offline_html_path": "",
            "offline_clean": True,
            "is_batch_mode": True,
            "context": dict(context),
            "game_id": int(context.get("game_id") or 0),
            "game_name": str(context.get("game_name") or ""),
            "app_id": int(context.get("game_id") or 0),
        }

        progress = QProgressDialog(
            f"正在批量导入 {len(mod_dirs)} 个 Mod…",
            "取消",
            0,
            0,
            self,
        )
        progress.setWindowTitle("批量导入目录")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        worker = ImportWorker(
            platform=PLATFORM_NEXUS,
            library_root=self._target_root,
            params=params,
            parent=self,
        )
        self._batch_import_worker = worker

        def _on_progress(message: str) -> None:
            progress.setLabelText(message or "正在导入…")

        def _on_ok(result: object) -> None:
            progress.close()
            self._batch_import_worker = None
            from services.importers.importer_base import ImportResult

            assert isinstance(result, ImportResult)
            imported = int(result.imported_count or 0) or 1
            skipped = int(result.skipped_count or 0)
            extra = f"\n跳过：{skipped} 个" if skipped else ""
            QMessageBox.information(
                self,
                "批量导入完成",
                f"成功导入 {imported} 个 Mod\n"
                f"目标游戏：{context.get('game_name')}{extra}",
            )
            self.refresh()

        def _on_err(error: str) -> None:
            progress.close()
            self._batch_import_worker = None
            QMessageBox.warning(self, "批量导入失败", error or "未知错误")

        def _on_cancel() -> None:
            if worker.isRunning():
                worker.requestInterruption()

        progress.canceled.connect(_on_cancel)
        worker.progress_changed.connect(_on_progress)
        worker.import_finished.connect(_on_ok)
        worker.import_failed.connect(_on_err)
        worker.start()

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

    def _set_current_game_context(
        self,
        game_name: str | None,
        *,
        game_id: int | None = None,
    ) -> None:
        name = (game_name or "").strip() or None
        if name == ALL_GAMES_LABEL:
            name = None
        self.current_game_name = name
        self._current_game_filter = name
        if game_id is not None and int(game_id) > 0:
            self.current_game_id = int(game_id)
        else:
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
            # D-8: center viewport only — avoid whole-page cover / flash
            vp = self.scroll.viewport()
            self.loading_overlay.setGeometry(vp.rect())
            self.loading_overlay.show()
            self.loading_overlay.raise_()
            QApplication.processEvents()
        else:
            self.loading_overlay.hide()

    # ------------------------------------------------------------------
    # Search / status filter / sort
    # ------------------------------------------------------------------

    def _collect_active_platform_sources(self) -> list[str]:
        from core.mod_platform import PLATFORM_STEAM, normalize_platform
        from ui.platform_labels import platform_badge_label

        seen: set[str] = set()
        for index, _card in self._card_entries:
            plat = normalize_platform(getattr(index, "platform", "") or PLATFORM_STEAM)
            if plat:
                seen.add(plat)
        return sorted(seen, key=lambda p: platform_badge_label(p).casefold())

    def _rebuild_platform_filter_bar(self) -> None:
        """Rebuild platform chips from platforms present in the current Mod list."""
        from ui.platform_labels import platform_badge_label

        active_sources = self._collect_active_platform_sources()
        current = self._platform_filter

        for btn in list(self._platform_buttons.values()):
            self._platform_group.removeButton(btn)
            btn.deleteLater()
        self._platform_buttons.clear()

        status_keys = {k for k, _ in STATUS_FILTER_LABELS}
        self._filter_buttons = {
            k: v for k, v in self._filter_buttons.items() if k in status_keys
        }

        while self._platform_flow.count():
            self._platform_flow.takeAt(0)

        all_btn = self._make_filter_chip("全部平台")
        all_btn.setCheckable(True)
        self._platform_group.addButton(all_btn)
        self._platform_buttons[FILTER_PLATFORM_ALL] = all_btn
        self._filter_buttons[FILTER_PLATFORM_ALL] = all_btn
        self._platform_flow.addWidget(all_btn)
        all_btn.toggled.connect(
            lambda checked, k=FILTER_PLATFORM_ALL: self._on_platform_filter_toggled(
                k, checked
            )
        )

        for plat in active_sources:
            label = platform_badge_label(plat)
            btn = self._make_filter_chip(label)
            btn.setCheckable(True)
            self._platform_group.addButton(btn)
            self._platform_buttons[plat] = btn
            self._platform_flow.addWidget(btn)
            btn.toggled.connect(
                lambda checked, k=plat: self._on_platform_filter_toggled(k, checked)
            )

        if current == FILTER_PLATFORM_ALL or current not in self._platform_buttons:
            all_btn.blockSignals(True)
            all_btn.setChecked(True)
            all_btn.blockSignals(False)
            self._platform_filter = FILTER_PLATFORM_ALL
        else:
            pick = self._platform_buttons[current]
            pick.blockSignals(True)
            pick.setChecked(True)
            pick.blockSignals(False)

        self._platform_bar.adjustSize()

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
            steam = db_fields.steam_name
            # Prefer raw user override when available; else resolved display_name.
            db_display = str(
                getattr(db_fields, "user_display_name", None)
                or db_fields.display_name
                or ""
            ).strip()
            notes = db_fields.user_notes
            favorite = db_fields.favorite
            deployed = db_fields.deploy_status == DEPLOY_STATUS_DEPLOYED
            game_db = db_fields.game_name or ""
        else:
            steam = (meta.title or "").strip() if meta else ""
            db_display = ""
            notes = ""
            favorite = False
            deployed = False
            game_db = ""
        from ui.library_query import resolve_mod_library_title

        display = resolve_mod_library_title(
            metadata_display_name=(meta.json_display_name if meta else "") or "",
            metadata_title=(meta.title if meta else "") or "",
            db_display_name=db_display,
            db_steam_name=steam,
            folder_name=folder.name,
        )
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
        # Re-parent/show churn collapses scroll range → jumps to bottom without this.
        scroll = self._capture_scroll()
        self.scroll.setUpdatesEnabled(False)
        self.library_host.setUpdatesEnabled(False)
        try:
            self.library_layout.setEnabled(False)
            # Detach cards from flow layout without destroying them
            while self.library_layout.count():
                item = self.library_layout.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is None or widget is self.empty_overlay:
                    continue
                widget.hide()

            query = self.search_box.text()
            category_key = (
                self._sidebar_category
                if self._sidebar_category
                else self._category_filter
            )
            visible_cards = filter_and_sort(
                self._card_entries,
                query=query,
                filter_key=self._status_filter,
                platform_key=self._platform_filter,
                category_key=category_key,
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
                        title=f'No mods in "{game}"',
                        hint="Switch to All Games, or import / sync mods for this game.",
                        action="Show all games",
                    )
                else:
                    self._show_empty(
                        EMPTY_LIBRARY,
                        title="No mods found",
                        hint="Import a mod to start building your library.",
                        action="Import Mod",
                    )
                self.count_label.setText("0 Mods")
                self._sync_library_host_size()
                assert id(self.detail_panel) == detail_id
                return

            if not visible_cards:
                self._show_empty(
                    EMPTY_SEARCH,
                    title="No matching mods",
                    hint="Try clearing the search box or resetting status / platform filters.",
                    action="Clear filters",
                )
                self.count_label.setText("0 Mods")
                self._sync_library_host_size()
                assert id(self.detail_panel) == detail_id
                return

            self.empty_overlay.hide()
            self._empty_kind = None
            for card in visible_cards:
                assert isinstance(card, ModCardWidget)
                self._reveal_card(card)

            self.count_label.setText(f"{len(visible_cards)} Mods")
            self.library_layout.invalidate()
            self._sync_library_host_size()
            assert id(self.detail_panel) == detail_id
        finally:
            self.library_layout.setEnabled(True)
            self.library_host.setUpdatesEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            self._set_scroll_value(scroll)

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
        visible = self._visible_cards()
        try:
            current_index = visible.index(card)
        except ValueError:
            return
        modifiers = QApplication.keyboardModifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if shift:
            anchor = self._selection_anchor
            if anchor is not None and anchor in visible:
                start_index = visible.index(anchor)
            else:
                start_index = self._last_clicked_index
                if visible:
                    start_index = max(0, min(start_index, len(visible) - 1))
            min_idx = min(start_index, current_index)
            max_idx = max(start_index, current_index)
            self._selected_cards = visible[min_idx : max_idx + 1]
            self._sync_selection_styles()
            self._apply_multi_or_single_panel()
        elif ctrl:
            self._toggle_card_selection(card)
            self._last_clicked_index = current_index
        else:
            self._select_card(card, show_panel=True)
            self._last_clicked_index = current_index

    def _visible_cards(self) -> list[ModCardWidget]:
        """Visible mod cards in FlowLayout physical order (filter + sort)."""
        cards: list[ModCardWidget] = []
        for i in range(self.library_layout.count()):
            item = self.library_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if not isinstance(widget, ModCardWidget) or widget.isHidden():
                continue
            cards.append(widget)
        return cards

    def select_all_mods(self) -> None:
        """Select every visible mod card (Ctrl+A)."""
        visible = self._visible_cards()
        if not visible:
            return
        self._selected_cards = list(visible)
        self._selection_anchor = visible[0]
        self._last_clicked_index = 0
        self._selected_card = visible[-1]
        self._selected_path = visible[-1].managed_path
        self._sync_selection_styles()
        self._apply_multi_or_single_panel()

    def _sync_selection_styles(self) -> None:
        selected = set(self._selected_cards)
        for card in self._cards:
            if card in selected:
                card.add_selected_style()
            else:
                card.remove_selected_style()

    def _apply_multi_or_single_panel(self) -> None:
        cards = [c for c in self._selected_cards if not c.isHidden()]
        if not cards:
            self.detail_panel.clear()
            return
        if len(cards) == 1:
            card = cards[0]
            self._selected_card = card
            self._selected_path = card.managed_path
            self._sync_peer_mods_to_panel(exclude=card._mod_id())
            self.detail_panel.show_mod(card.managed_path)
            self._apply_audit_to_panel(card._mod_id())
            return
        # Multi-select: detail shows batch edit + optional offline save.
        self._selected_card = cards[-1]
        self._selected_path = cards[-1].managed_path
        ids = [c._mod_id() for c in cards if c._mod_id()]
        plat = "steam"
        entries: list[tuple[str, object, str]] = []
        for card in cards:
            mid = card._mod_id()
            if not mid:
                continue
            card_plat = "steam"
            for index, c in self._card_entries:
                if c is card:
                    card_plat = getattr(index, "platform", "steam") or "steam"
                    break
            if card is cards[-1]:
                plat = card_plat
            entries.append((mid, card.managed_path, card_plat))
        self.detail_panel.show_batch_selection(
            ids,
            game_name=str(self.current_game_name or "").strip(),
            game_id=int(self.current_game_id or 0),
            platform=plat,
            entries=entries,
        )

    def _select_card(self, card: ModCardWidget, *, show_panel: bool) -> None:
        self._selected_cards = [card]
        self._selection_anchor = card
        self._selected_card = card
        self._selected_path = card.managed_path
        self._sync_selection_styles()
        if show_panel:
            self._apply_multi_or_single_panel()

    def _toggle_card_selection(self, card: ModCardWidget) -> None:
        if card in self._selected_cards:
            self._selected_cards = [c for c in self._selected_cards if c is not card]
            if self._selection_anchor is card:
                self._selection_anchor = (
                    self._selected_cards[-1] if self._selected_cards else None
                )
        else:
            self._selected_cards.append(card)
            self._selection_anchor = card
        self._sync_selection_styles()
        if not self._selected_cards:
            self._selected_card = None
            self._selected_path = None
            self.detail_panel.clear()
            return
        self._apply_multi_or_single_panel()

    def _prepare_card_context_menu(self, card: ModCardWidget) -> None:
        """Ensure right-click target is part of the current selection."""
        if card in self._selected_cards:
            return
        self._select_card(card, show_panel=False)
        visible = self._visible_cards()
        try:
            self._last_clicked_index = visible.index(card)
        except ValueError:
            pass

    def _on_batch_set_category(self, category: str) -> None:
        """Apply category to every selected mod; sync SQLite + metadata.json."""
        targets = [c for c in self._selected_cards if not c.isHidden()]
        if not targets:
            return
        label = str(category or "").strip()
        db = get_db()
        from services.info_sidecar import write_sidecar_for_mod

        for card in targets:
            mid = card._mod_id()
            if not mid:
                continue
            try:
                db.set_mod_category(mid, label)
                write_sidecar_for_mod(card.managed_path, mid)
            except Exception:  # noqa: BLE001
                continue
            tags = db.get_category_tags(mid)
            cat_text = " ".join(tags)
            for i, (index, c) in enumerate(self._card_entries):
                if c is card:
                    self._card_entries[i] = (replace(index, category_tags=cat_text), c)
                    break
            card.refresh_display()
            if len(targets) == 1 and self._selected_card is card:
                self.detail_panel._fill_category_tags(mid)

        self._refresh_category_combo()
        self._apply_view_filter()
        self._sync_selection_styles()
        self._apply_multi_or_single_panel()

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
        for card in self._selected_cards:
            card.remove_selected_style()
        if self._selected_card is not None and self._selected_card not in self._selected_cards:
            self._selected_card.remove_selected_style()
        self._selected_cards = []
        self._selection_anchor = None
        self._selected_card = None
        self._selected_path = None

    def _on_batch_platform_saved(self, mod_ids: object) -> None:
        """Refresh cards after batch source-only save."""
        ids = {str(m).strip() for m in (mod_ids or [])}
        try:
            from services.file_ops import ModFileManager
            from services.info_sidecar import write_sidecar_for_mod

            mgr = ModFileManager(self._target_root)
            for mid in ids:
                folder = mgr.find_by_published_id(mid)
                if folder is not None:
                    write_sidecar_for_mod(folder, mid)
        except Exception:  # noqa: BLE001
            pass
        for card in self._cards:
            mid = card._mod_id()
            if mid in ids:
                card.refresh_display()
        # Keep multi-select panel state with updated platform label.
        if len(self._selected_cards) > 1:
            self._apply_multi_or_single_panel()
        elif self._selected_card is not None:
            self.detail_panel.show_mod(self._selected_card.managed_path)

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
        manager = ModFileManager(self._target_root)
        meta = manager.load_metadata(path)
        if card is None and meta is not None and meta.published_file_id:
            card = self._card_for_mod_id(str(meta.published_file_id))
        if card is not None:
            if meta is None:
                meta = card.metadata or ModMetadata(
                    published_file_id=path.name if path.name.isdigit() else "",
                    title="",
                    managed_path=str(path),
                )
            meta.managed_path = str(path)
            meta.local_path = str(path)
            card.rebind(path, meta)
            # Rebuild index for this card so search/favorite filters stay correct
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
            lambda a=action: self._on_deploy_started(a),
            Qt.ConnectionType.QueuedConnection,
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
                hints.append("该 Mod 已标记存在冲突")
        try:
            for w in get_db().check_relationship_deploy_warnings(mod_id):
                msg = str(w.get("message") or "").strip()
                if msg:
                    hints.append(msg)
        except Exception:  # noqa: BLE001
            pass
        text = "；".join(hints)
        if text:
            text = text + "。仍可继续部署，请人工确认。"
        self.detail_panel.set_tag_deploy_hint(text)

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
        selected_id = (
            self._selected_card._mod_id() if self._selected_card is not None else ""
        )
        # Deploy success → prefer the deployed mod; otherwise keep selection.
        anchor_id = str(focus_mod_id or selected_id or mod_id).strip()
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
            self._restore_scroll_after_layout(scroll, focus_mod_id=anchor_id)
            break
        else:
            self._restore_scroll_after_layout(scroll, focus_mod_id=anchor_id)

    # ------------------------------------------------------------------
    # Game list / cards
    # ------------------------------------------------------------------

    def _rebuild_game_list(
        self,
        manager: ModFileManager,
        prefer: str | None = None,
    ) -> None:
        """
        Build the game sidebar from SQLite-configured games (primary),
        merged with any on-disk library folders not yet in the DB.

        Does **not** require Steam workshop sync / ``workshop_path``.
        """
        from core.sanitize import sanitize_folder_name

        self.game_list.blockSignals(True)
        self.game_list.clear()

        total = len(manager.list_managed_mods())
        self._add_game_list_item(ALL_GAMES_LABEL, total, key="", game_id=0)

        # key (library folder) -> (display_name, app_id, mod_count)
        entries: dict[str, tuple[str, int, int]] = {}

        try:
            db_games = [g for g in get_db().list_games() if int(g.app_id or 0) > 0]
        except Exception:  # noqa: BLE001
            logger.debug("list_games from DB failed", exc_info=True)
            db_games = []

        for game in db_games:
            app_id = int(game.app_id)
            display = (game.display_name or "").strip() or f"App_{app_id}"
            folder = (game.folder_name or "").strip() or sanitize_folder_name(
                display, fallback=f"App_{app_id}"
            )
            count = len(manager.list_managed_mods(game_name=folder))
            # Prefer DB identity when the same folder appears twice.
            entries[folder] = (display, app_id, count)

        # Keep filesystem-only folders (legacy / empty dirs) that are not in DB.
        for name in manager.list_games():
            if name in entries:
                continue
            count = len(manager.list_managed_mods(game_name=name))
            entries[name] = (name, 0, count)

        for key in sorted(entries.keys(), key=str.lower):
            display, app_id, count = entries[key]
            categories: list[str] = []
            if app_id > 0:
                try:
                    categories = list(get_db().list_game_categories(app_id))
                except Exception:  # noqa: BLE001
                    logger.debug("list_game_categories failed", exc_info=True)
                    categories = []
            self._add_game_list_item(
                display,
                count,
                key=key,
                game_id=app_id,
                expandable=bool(categories),
                expanded=key in self._expanded_games,
            )
            for cat in categories:
                cat_count = self._count_mods_for_category(key, cat, manager)
                self._add_game_list_item(
                    cat,
                    cat_count,
                    key=key,
                    game_id=app_id,
                    category=cat,
                    indent=True,
                )

        prefer_key = prefer or ""
        if prefer_key == ALL_GAMES_LABEL:
            prefer_key = ""
        prefer_category = self._sidebar_category or ""
        if prefer_category and prefer_key:
            self._expanded_games.add(prefer_key)

        target_row = 0
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            if item is None:
                continue
            key = item.data(GAME_ROLE) or ""
            cat = str(item.data(GAME_CATEGORY_ROLE) or "").strip()
            if prefer_category and cat == prefer_category and key == prefer_key:
                target_row = i
                break
            if not prefer_category and key == prefer_key:
                target_row = i
                break

        self.game_list.setCurrentRow(target_row)
        current = self.game_list.currentItem()
        key = (current.data(GAME_ROLE) if current else "") or ""
        gid = int(current.data(GAME_ID_ROLE) or 0) if current else 0
        self._set_current_game_context(key or None, game_id=gid or None)
        self._pending_game_filter = None
        self._sync_category_row_visibility()
        self.game_list.blockSignals(False)

    def _count_mods_for_category(
        self,
        game_key: str,
        category: str,
        manager: ModFileManager,
    ) -> int:
        label = str(category or "").strip()
        if not game_key or not label:
            return 0
        count = 0
        try:
            db = get_db()
            for folder in manager.list_managed_mods(game_name=game_key):
                meta = manager.load_metadata(folder)
                mid = str(meta.published_file_id or "") if meta else ""
                if not mid.isdigit():
                    continue
                tags = db.get_category_tags(mid)
                if tags and str(tags[0]).strip() == label:
                    count += 1
        except Exception:  # noqa: BLE001
            return 0
        return count

    def _add_game_list_item(
        self,
        name: str,
        count: int,
        *,
        key: str,
        game_id: int = 0,
        category: str = "",
        indent: bool = False,
        expandable: bool = False,
        expanded: bool = False,
    ) -> None:
        """Steam-sidebar row via item widget only (empty item text avoids ghost paint)."""
        # Empty DisplayRole — QListWidgetItem text + setItemWidget stacked = 重影.
        item = QListWidgetItem()
        item.setData(GAME_ROLE, key)
        item.setData(GAME_ID_ROLE, int(game_id or 0))
        item.setData(GAME_CATEGORY_ROLE, str(category or ""))
        item.setToolTip(f"{name}  ·  {count}" if not category else f"{name}  ·  {count}")
        row = _GameFilterRow(
            name,
            count,
            show_count=True,
            indent=indent or bool(category),
            expandable=expandable and not category,
            expanded=expanded,
        )
        item.setSizeHint(row.sizeHint())
        self.game_list.addItem(item)
        self.game_list.setItemWidget(item, row)

    def _sync_category_row_visibility(self) -> None:
        """Hide category rows unless their parent game is expanded."""
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            if item is None:
                continue
            key = str(item.data(GAME_ROLE) or "")
            cat = str(item.data(GAME_CATEGORY_ROLE) or "").strip()
            widget = self.game_list.itemWidget(item)
            if cat:
                item.setHidden(bool(key) and key not in self._expanded_games)
                continue
            if isinstance(widget, _GameFilterRow) and widget.expandable:
                widget.set_expanded(key in self._expanded_games)

    def _toggle_game_expanded(self, game_key: str) -> None:
        key = str(game_key or "").strip()
        if not key:
            return
        if key in self._expanded_games:
            self._expanded_games.discard(key)
        else:
            self._expanded_games.add(key)
        self._sync_category_row_visibility()

    def _on_game_item_clicked(self, item: QListWidgetItem | None) -> None:
        """Toggle category fold when a primary game row is clicked."""
        if item is None:
            return
        category = str(item.data(GAME_CATEGORY_ROLE) or "").strip()
        if category:
            return
        key = str(item.data(GAME_ROLE) or "")
        if not key:
            return
        widget = self.game_list.itemWidget(item)
        if isinstance(widget, _GameFilterRow) and not widget.expandable:
            return
        self._toggle_game_expanded(key)

    def _on_game_list_context_menu(self, pos) -> None:
        item = self.game_list.itemAt(pos)
        if item is None:
            return
        key = str(item.data(GAME_ROLE) or "")
        gid = int(item.data(GAME_ID_ROLE) or 0)
        category = str(item.data(GAME_CATEGORY_ROLE) or "").strip()
        if not key or gid <= 0 or category:
            return

        menu = QMenu(self)
        menu.setObjectName("libraryImportMenu")
        act_add = menu.addAction("新增分类")
        chosen = menu.exec(self.game_list.mapToGlobal(pos))
        if chosen is not act_add:
            return

        name, ok = QInputDialog.getText(self, "新增分类", "分类名称：")
        if not ok:
            return
        label = str(name or "").strip()
        if not label:
            return
        try:
            if not get_db().add_game_category(gid, label):
                return
        except Exception:  # noqa: BLE001
            logger.debug("add_game_category failed", exc_info=True)
            return
        self._expanded_games.add(key)
        self._rebuild_game_list(ModFileManager(self._target_root), prefer=key)

    def _on_game_item_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        key = ""
        gid = 0
        category = ""
        if current is not None:
            key = current.data(GAME_ROLE) or ""
            gid = int(current.data(GAME_ID_ROLE) or 0)
            category = str(current.data(GAME_CATEGORY_ROLE) or "").strip()
        self._set_current_game_context(key or None, game_id=gid or None)

        if category:
            prev_key = ""
            if _previous is not None:
                prev_key = str(_previous.data(GAME_ROLE) or "")
            self._sidebar_category = category
            if prev_key != key:
                manager = ModFileManager(self._target_root)
                self._clear_selection()
                self.detail_panel.clear()
                self._render_mod_cards(manager)
                self._sync_library_host_size()
                self._set_scroll_value(0)
            else:
                self._apply_view_filter()
            return

        self._sidebar_category = None
        manager = ModFileManager(self._target_root)
        self._clear_selection()
        self.detail_panel.clear()
        self._render_mod_cards(manager)
        # Game switch: never keep the previous game's scroll offset / range.
        self._sync_library_host_size()
        self._set_scroll_value(0)
        self.filter_changed.emit(key or ALL_GAMES_LABEL)

    def _render_mod_cards(self, manager: ModFileManager) -> None:
        self._detach_active_cards()
        game = self._current_game_filter
        folders = manager.list_managed_mods(game_name=game)

        if not folders:
            self._cards = []
            self._card_entries = []
            self._prune_stale_card_cache(set(), game)
            self._rebuild_platform_filter_bar()
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
            meta.managed_path = str(folder)
            meta.local_path = str(folder)
            # Read-only sidecar overlay for display — never sync DB during render.
            try:
                from services.info_sidecar import load_info_sidecar

                side = load_info_sidecar(folder)
                if side is not None:
                    mid_hint = (
                        side.published_file_id
                        or str(meta.published_file_id or "")
                    )
                    if side.display_name:
                        meta.json_display_name = side.display_name
                    if side.description and not meta.description:
                        meta.description = side.description
                    if side.cover_path and not meta.cover_path:
                        meta.cover_path = side.cover_path
                    if side.offline_page_path and not meta.offline_page_path:
                        meta.offline_page_path = side.offline_page_path
                    if mid_hint.isdigit() and not str(
                        meta.published_file_id or ""
                    ).isdigit():
                        meta.published_file_id = mid_hint
            except Exception:  # noqa: BLE001
                pass
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

        game_cats: list[str] = []
        try:
            if self.current_game_id and int(self.current_game_id) > 0:
                game_cats = get_db().list_game_categories(int(self.current_game_id))
        except Exception:  # noqa: BLE001
            game_cats = []

        created = 0
        reused = 0
        keep_keys: set[str] = set()
        cards: list[ModCardWidget] = []
        entries: list[tuple[ModFilterIndex, ModCardWidget]] = []

        for folder, meta in metas:
            mid = str(meta.published_file_id or "")
            key = self._card_cache_key(folder)
            keep_keys.add(key)
            card = self._card_cache.get(key)
            if card is None:
                card = ModCardWidget(folder, metadata=meta, parent=self.library_host)
                card.hide()
                self._connect_card_signals(card)
                self._card_cache[key] = card
                created += 1
            else:
                card.rebind(folder, meta)
                card.hide()
                reused += 1
            card.set_category_options(game_cats)
            index = self._build_filter_index(
                folder,
                meta,
                manager,
                fields_map.get(mid),
                tag_flags=tag_flags_map.get(mid),
            )
            cards.append(card)
            entries.append((index, card))

        self._cards = cards
        self._card_entries = entries
        self._card_create_count = created
        self._card_reuse_count = reused

        # Drop missing paths always; within the active game, drop renamed leftovers.
        # Other games' cached cards stay for reuse on switch.
        self._prune_stale_card_cache(keep_keys, game)

        self._rebuild_platform_filter_bar()
        self._apply_view_filter()

    @staticmethod
    def _card_cache_key(folder: Path) -> str:
        try:
            return str(Path(folder).resolve())
        except OSError:
            return str(Path(folder))

    def _connect_card_signals(self, card: ModCardWidget) -> None:
        card.selection_requested.connect(self.on_mod_selected)
        card.edit_requested.connect(self._on_card_edit_requested)
        card.deploy_requested.connect(self._on_deploy_requested)
        card.open_folder_requested.connect(self._on_card_open_folder)
        card.open_steam_requested.connect(self._on_card_open_steam)
        card.favorite_toggle_requested.connect(self._on_card_favorite_toggle)
        card.context_menu_opening.connect(
            lambda c=card: self._prepare_card_context_menu(c)
        )
        card.set_category_requested.connect(self._on_batch_set_category)

    def _detach_active_cards(self) -> None:
        """Hide / detach from layout without destroying widgets (card cache reuse)."""
        self._last_clicked_index = 0
        self._clear_selection()
        while self.library_layout.count():
            item = self.library_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.empty_overlay:
                widget.hide()
        for card in self._cards:
            card.hide()
        self.library_host.setMinimumHeight(0)
        self.library_layout.invalidate()
        self._sync_library_host_size()

    def _drop_cache_key(self, key: str) -> None:
        card = self._card_cache.pop(key, None)
        if card is None:
            return
        card.hide()
        card.deleteLater()

    def _prune_stale_card_cache(
        self, keep_keys: set[str], game: str | None
    ) -> None:
        """
        Prune deleted/renamed paths without wiping other games' reuse cache.

        - Always remove cache entries whose folder no longer exists.
        - When a game is selected, also remove entries under that game not in
          *keep_keys* (covers in-game renames / deletes).
        - On All Games (game is None), remove every key not in *keep_keys*.
        """
        game_name = str(game or "").strip()
        for key in list(self._card_cache.keys()):
            if key in keep_keys:
                continue
            path = Path(key)
            if not path.is_dir():
                self._drop_cache_key(key)
                continue
            if not game_name:
                self._drop_cache_key(key)
                continue
            # Same game folder but not in current dataset → renamed/deleted leftover.
            try:
                if path.parent.name == game_name:
                    self._drop_cache_key(key)
            except Exception:  # noqa: BLE001
                pass

    def _prune_card_cache(self, keep_keys: set[str]) -> None:
        for key in list(self._card_cache.keys()):
            if key in keep_keys:
                continue
            self._drop_cache_key(key)
    def _clear_cards(self) -> None:
        """Clear active lists and destroy cached cards (full wipe)."""
        self._detach_active_cards()
        for card in list(self._card_cache.values()):
            card.hide()
            card.deleteLater()
        self._card_cache.clear()
        self._cards.clear()
        self._card_entries.clear()
        self.library_host.setMinimumHeight(0)
        self.library_layout.invalidate()
        self._sync_library_host_size()

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
            if FILTER_PLATFORM_ALL in self._platform_buttons:
                self._platform_buttons[FILTER_PLATFORM_ALL].setChecked(True)
            self._platform_filter = FILTER_PLATFORM_ALL
            if self.category_combo.count() > 0:
                self.category_combo.setCurrentIndex(0)
            self._category_filter = FILTER_CATEGORY_ALL
            self._sidebar_category = None
            self._apply_view_filter()
        elif kind == EMPTY_GAME:
            if self.game_list.count() > 0:
                self.game_list.setCurrentRow(0)
        elif kind == EMPTY_LIBRARY:
            self._on_import_mod()

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

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._splitter_defaults_applied:
            return
        # Apply defaults once the page has a real width (do not override user drags later)
        total = max(self.splitter.width(), sum(SPLITTER_DEFAULT_SIZES))
        left = GAME_PANEL_WIDTH
        # Prefer a 4-card Mod grid; detail keeps its minimum and may grow with leftover.
        right = max(
            DETAIL_PANEL_MIN,
            min(DETAIL_PANEL_PREFERRED, total - left - LIBRARY_CENTER_MIN_WIDTH),
        )
        center = max(LIBRARY_CENTER_MIN_WIDTH, total - left - right)
        self.splitter.setSizes([left, center, right])
        self._splitter_defaults_applied = True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.empty_overlay.isVisible():
            self.empty_overlay.setGeometry(self.library_host.rect())
        if self.loading_overlay.isVisible():
            self.loading_overlay.setGeometry(self.scroll.viewport().rect())
