"""Mod Library view — game filter + card grid + detail panel."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import DEPLOY_STATUS_DEPLOYED, get_db, updated_at_to_mtime
from core.models import ModMetadata
from core.paths import default_mod_library
from services.file_ops import ModFileManager
from services.crash_trace import log_exception, traced

from .deploy_thread import DeployWorker
from .flow_layout import FlowLayout
from .library_query import (
    FILTER_ALL,
    FILTER_CATEGORY_ALL,
    FILTER_DEPLOYMENT_RECORD,
    FILTER_PLATFORM_ALL,
    SORT_LABELS,
    SORT_MTIME,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
    coerce_filter_selection,
    collect_category_labels,
    compute_record_relative_status,
    filter_sort_entries,
    matches_search,
    projection_requires_view_recompute,
)
from .library_viewport import (
    clamp_scroll_y,
    compute_viewport_window,
    estimate_total_height,
)
from .collection_card import (
    COLLECTION_CREATE_CACHE_KEY,
    CollectionCardData,
    CollectionCardWidget,
    CollectionCreateCard,
    collection_cache_key,
)
from .mod_card import CARD_WIDTH, ModCardWidget
from .mod_detail_panel import ModDetailPanel

logger = logging.getLogger(__name__)

ALL_GAMES_LABEL = "全部游戏"
GAME_PANEL_MIN = 140
GAME_PANEL_MAX = 220
GAME_PANEL_WIDTH = 168
DETAIL_PANEL_MIN = 350
DETAIL_PANEL_PREFERRED = 360
DETAIL_PANEL_MAX = 420
# Default splitter sizes: slim game column, wide Mod grid, compact detail
SPLITTER_DEFAULT_SIZES = (140, 720, 360)
LIBRARY_ACTION_BTN_W = 118
LIBRARY_ACTION_BTN_H = 28
# FlowLayout wraps by width — reserve room for 4 cards + spacing + chrome
LIBRARY_CARDS_PER_ROW = 4
# Cap ModCardWidget retention. Scrolling must not keep one QWidget per Mod.
LIBRARY_CARD_CACHE_BUDGET = 96
LIBRARY_SCROLL_SYNC_MS = 16
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
EMPTY_COLLECTION = "empty_collection"

# Independent Library work mode — not a status Filter, not WH3 Sorting Mode.
COLLECTION_MODE_NORMAL = "normal"
COLLECTION_MODE_LIST = "list"
COLLECTION_MODE_CONTENT = "content"


def _library_load_sync() -> bool:
    """Keep ``refresh()`` synchronous under pytest / ``SMM_LIBRARY_SYNC=1``."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    flag = os.environ.get("SMM_LIBRARY_SYNC", "").strip().lower()
    return flag in {"1", "true", "yes"}


class _GameFilterRow(QWidget):
    """Sidebar row: GameTreeItem (primary). CategoryTreeItem kept for style tests."""

    KIND_GAME = "game"
    KIND_CATEGORY = "category"
    KIND_ALL = "all"
    ROW_HEIGHT = 32

    def __init__(
        self,
        name: str,
        count: int,
        *,
        kind: str = "game",
        show_count: bool = True,
        indent: bool = False,
        expandable: bool = False,
        expanded: bool = False,
        game_status: str = "",
        overall_status: str = "",
        status_tip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = str(kind or self.KIND_GAME)
        if self.kind == self.KIND_CATEGORY:
            self.setObjectName("CategoryTreeItem")
        else:
            self.setObjectName("GameTreeItem")
        self.setMinimumHeight(self.ROW_HEIGHT)
        self.setMaximumHeight(self.ROW_HEIGHT)
        self.expandable = bool(expandable)
        layout = QHBoxLayout(self)
        left = 8 + (14 if indent else 0)
        layout.setContentsMargins(left, 4, 8, 4)
        layout.setSpacing(6)

        self.chevron_label = QLabel("")
        self.chevron_label.setObjectName("gameListChevron")
        self.chevron_label.setFixedWidth(12)
        self.chevron_label.hide()

        self.icon_label = QLabel("")
        self.icon_label.setObjectName(
            "categoryTreeIcon" if self.kind == self.KIND_CATEGORY else "gameTreeIcon"
        )
        self.icon_label.setFixedWidth(18)
        self.icon_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self.icon_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self.icon_label)

        self.name_label = QLabel("")
        self.name_label.setObjectName(
            "categoryTreeName" if self.kind == self.KIND_CATEGORY else "gameTreeName"
        )
        self.name_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # Ignored: shrink below full-text sizeHint so the count column is never clipped.
        self.name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.name_label.setMinimumWidth(0)
        self.name_label.setWordWrap(False)
        self.count_label = QLabel(str(count))
        self.count_label.setObjectName(
            "categoryTreeCount" if self.kind == self.KIND_CATEGORY else "gameTreeCount"
        )
        count_w = max(
            self.count_label.fontMetrics().horizontalAdvance("9999"),
            self.count_label.fontMetrics().horizontalAdvance(str(count)),
        )
        self.count_label.setMinimumWidth(count_w)
        self.count_label.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.name_label, stretch=1)
        if show_count:
            layout.addWidget(self.count_label, stretch=0)
        else:
            self.count_label.hide()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self._base_name = str(name or "")
        self._overall_status = str(overall_status or "").strip()
        self._status_tip = str(status_tip or "").strip()
        self.apply_status(
            game_status=game_status,
            overall_status=self._overall_status,
            status_tip=self._status_tip,
        )

    def apply_status(
        self,
        *,
        game_status: str = "",
        overall_status: str = "",
        status_tip: str = "",
    ) -> None:
        from services.game_status import (
            OVERALL_HEALTHY,
            OVERALL_MISSING,
            leading_icon_for_overall,
        )
        from services.library_status import GAME_STATUS_MISSING_FOLDER

        overall = str(overall_status or "").strip()
        if not overall:
            if str(game_status or "").strip() == GAME_STATUS_MISSING_FOLDER:
                overall = OVERALL_MISSING
            else:
                overall = OVERALL_HEALTHY
        self._overall_status = overall
        tip = str(status_tip or "").strip()
        self._status_tip = tip

        self.name_label.setText(self._base_name)

        if self.kind == self.KIND_CATEGORY:
            self.icon_label.setText(
                leading_icon_for_overall(overall, kind=self.kind)
            )
            if tip:
                self.setToolTip(tip)
            return

        if self.kind == self.KIND_ALL:
            self.icon_label.setText("📚")
            return

        self.icon_label.setText("🎮")
        if tip:
            self.setToolTip(tip)
            self.icon_label.setToolTip(tip)
        elif overall == OVERALL_MISSING:
            miss_tip = "Mod目录不存在\n但备份数据仍存在"
            self.setToolTip(miss_tip)
            self.icon_label.setToolTip(miss_tip)
        else:
            self.setToolTip("")
            self.icon_label.setToolTip("")
        self._apply_name_elide()

    def _apply_name_elide(self) -> None:
        width = max(0, int(self.name_label.width()))
        if width <= 1:
            self.name_label.setText(self._base_name)
            return
        self.name_label.setText(
            self.name_label.fontMetrics().elidedText(
                self._base_name, Qt.TextElideMode.ElideRight, width
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_name_elide()

    def set_game_status(self, game_status: str) -> None:
        self.apply_status(
            game_status=game_status,
            overall_status=self._overall_status,
            status_tip=self._status_tip,
        )

    def set_expanded(self, expanded: bool) -> None:
        if not self.expandable:
            return
        self.chevron_label.setText("▾" if expanded else "▸")
        self.chevron_label.setToolTip("收起分类" if expanded else "展开分类")


class ModLibraryView(QWidget):
    """View B: browse managed mods under the local library (3-column workspace)."""

    filter_changed = Signal(str)
    request_open_sync = Signal()  # optional: MainWindow may ignore
    _reconcile_idle = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: list[ModCardWidget] = []
        self._card_entries: list[tuple[ModFilterIndex, ModCardWidget]] = []
        # Layer-1 rows for the active game (no QWidget). Viewport binds a window.
        self._game_row_entries: list[tuple[ModFilterIndex, object]] = []
        self._filtered_row_entries: list[tuple[ModFilterIndex, object]] = []
        self._card_cache: dict[str, ModCardWidget] = {}
        self._viewport_top_spacer: QWidget | None = None
        self._viewport_bottom_spacer: QWidget | None = None
        self._cards_host: QWidget | None = None
        self._viewport_last_width: int = 0
        self._viewport_syncing = False
        self._card_create_count = 0
        self._card_reuse_count = 0
        self._selected_card: ModCardWidget | None = None
        self._selected_path: Path | None = None  # display bind only — not entity identity
        self._selected_mod_id: str = ""
        # Selection authority = mod_id (survives viewport card rebind).
        self._selected_mod_ids: list[str] = []
        self._selection_anchor_mod_id: str = ""
        # Viewport-bound mirrors only (never the sole Shift anchor).
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
        self._deploy_watchdog = QTimer(self)
        self._deploy_watchdog.setSingleShot(True)
        self._deploy_watchdog.setInterval(180_000)
        self._deploy_watchdog.timeout.connect(self._on_deploy_watchdog_timeout)
        self._status_filter = FILTER_ALL
        self._category_filter = FILTER_CATEGORY_ALL
        self._sidebar_category: str | None = None
        self._expanded_games: set[str] = set()
        self._sort_mode = SORT_MTIME
        self._loading = False
        self._splitter_defaults_applied = False
        self._load_worker = None
        self._load_gen = 0
        self._library_load_pending = False
        self._library_snapshot = None
        self._pending_restore: dict | None = None
        # _snapshot_dirty means the cached library snapshot is stale and needs
        # rebuilding before a fresh snapshot is required.
        #
        # It does NOT mean that every game switch must rebuild the entire library.
        # Game switches filter the warm snapshot in memory when dirty is False.
        self._snapshot_dirty = False
        self._pending_game_status_line = ""
        self._last_filter_sig: tuple | None = None
        self._game_list_fp: tuple | None = None
        # Deployment record is a status filter peer — not a parallel “mode”.
        self._deployment_record_id: int | None = None
        self._deployment_record_name: str | None = None
        self._cached_record_mod_ids: frozenset[str] | None = None
        self._wh3_sort_mode = False
        self._wh3_display_numbers: dict[str, int] = {}
        self._collection_mode = COLLECTION_MODE_NORMAL
        self._current_collection_id: int | None = None
        self._collection_list_entries: list[CollectionCardData | None] = []
        self._collection_cards: list[QWidget] = []
        self._collection_card_cache: dict[str, QWidget] = {}
        self._force_scroll_zero = False
        self._suppress_collection_toggle = False
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_view_filter)
        self._cover_sched = QTimer(self)
        self._cover_sched.setSingleShot(True)
        self._cover_sched.setInterval(40)
        self._cover_sched.timeout.connect(self._load_viewport_covers)
        self._scroll_sync_timer = QTimer(self)
        self._scroll_sync_timer.setSingleShot(True)
        self._scroll_sync_timer.setInterval(LIBRARY_SCROLL_SYNC_MS)
        self._scroll_sync_timer.timeout.connect(self._on_library_scroll_covers)
        self._viewport_live_cards: list[ModCardWidget] = []
        self._viewport_clamp_timer = QTimer(self)
        self._viewport_clamp_timer.setSingleShot(True)
        self._viewport_clamp_timer.setInterval(0)
        self._viewport_clamp_timer.timeout.connect(self._clamp_viewport_after_layout)
        self._pending_projection: dict[str, str] = {}
        self._projection_coalesce = QTimer(self)
        self._projection_coalesce.setSingleShot(True)
        self._projection_coalesce.setInterval(0)
        self._projection_coalesce.timeout.connect(self._flush_projection_patches)

        self._build_ui()
        self._reconcile_idle.connect(
            self._flush_pending_library_load,
            Qt.ConnectionType.QueuedConnection,
        )
        self._reconcile_idle.connect(
            self._resync_projections_after_reconcile,
            Qt.ConnectionType.QueuedConnection,
        )
        if not _library_load_sync():
            try:
                from services.library_reconcile import add_reconcile_idle_listener

                add_reconcile_idle_listener(self._on_reconcile_idle)
            except Exception:  # noqa: BLE001
                logger.debug("reconcile idle listener not registered", exc_info=True)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Compact header lives in the center pane (not above the splitter)
        # so the game list top-aligns with primary nav.
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
        self.import_btn.setToolTip(
            "导入单个 Mod，批量导入目录，或批量导入离线 HTML 页面"
        )
        self._import_menu = QMenu(self.import_btn)
        self._import_menu.setObjectName("libraryImportMenu")
        act_single = QAction("导入单个 Mod（文件/压缩包）", self._import_menu)
        act_single.triggered.connect(self._on_import_single_mod)
        act_batch = QAction("批量导入目录（多 Mod）", self._import_menu)
        act_batch.triggered.connect(self._on_import_batch_directory)
        act_batch_html = QAction("批量导入离线页面（多 HTML）", self._import_menu)
        act_batch_html.triggered.connect(self._on_import_batch_offline_html)
        self._import_menu.addAction(act_single)
        self._import_menu.addAction(act_batch)
        self._import_menu.addAction(act_batch_html)
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
        self.game_panel = game_panel
        game_layout = QVBoxLayout(game_panel)
        game_layout.setContentsMargins(0, 0, 0, 0)
        game_layout.setSpacing(0)

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

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self.btn_collection_back = QPushButton("← 合集")
        self.btn_collection_back.setObjectName("libraryHeaderButton")
        self.btn_collection_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_collection_back.setToolTip("返回合集列表")
        self.btn_collection_back.clicked.connect(self._return_to_collection_list)
        self.btn_collection_back.hide()
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)
        title_col.addWidget(self._page_title)
        title_col.addWidget(self.count_label)
        header.addWidget(self.btn_collection_back, alignment=Qt.AlignmentFlag.AlignTop)
        header.addLayout(title_col, stretch=1)
        header.addWidget(self.import_btn, alignment=Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.refresh_btn, alignment=Qt.AlignmentFlag.AlignTop)
        center_layout.addLayout(header)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("librarySearchBox")
        self.search_box.setPlaceholderText(
            "搜索显示名 / Steam 名 / 备注 / Workspace ID / 游戏名…"
        )
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumHeight(32)
        self.search_box.textChanged.connect(self._on_search_text_changed)
        center_layout.addWidget(self.search_box)

        # Filter column (chips + category/sort) is independent of Mode column.
        # Mode stack height must never move chips or _meta_bar.
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[str, QPushButton] = {}

        self._library_toolbar = QWidget()
        self._library_toolbar.setObjectName("libraryToolbar")
        self._library_toolbar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        toolbar_row = QHBoxLayout(self._library_toolbar)
        toolbar_row.setContentsMargins(0, 0, 0, 0)
        toolbar_row.setSpacing(8)
        toolbar_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._filter_column = QWidget(self._library_toolbar)
        self._filter_column.setObjectName("libraryFilterColumn")
        self._filter_column.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        left_col = QVBoxLayout(self._filter_column)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(6)
        left_col.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._status_chips = QWidget(self._filter_column)
        self._status_chips.setObjectName("libraryFilterBar")
        # Alias: chips row only — never wraps Mode buttons.
        self._status_bar = self._status_chips
        self._status_chips.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        status_flow = FlowLayout(
            self._status_chips, margin=0, h_spacing=6, v_spacing=6
        )
        for key, label in STATUS_FILTER_LABELS:
            btn = self._make_filter_chip(label, parent=self._status_chips)
            btn.setCheckable(True)
            if key == FILTER_ALL:
                btn.setChecked(True)
            self._filter_group.addButton(btn)
            self._filter_buttons[key] = btn
            btn.toggled.connect(
                lambda checked, k=key: self._on_status_filter_toggled(k, checked)
            )
            status_flow.addWidget(btn)
        left_col.addWidget(self._status_chips, 0)

        # Tag / sort — own flow row so they wrap as whole groups, never overlap chips
        self._meta_bar = QWidget(self._filter_column)
        self._meta_bar.setObjectName("libraryFilterBar")
        self._meta_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        meta_flow = FlowLayout(self._meta_bar, margin=0, h_spacing=10, v_spacing=6)

        tag_group = QWidget()
        tag_group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        tag_row = QHBoxLayout(tag_group)
        tag_row.setContentsMargins(0, 0, 0, 0)
        tag_row.setSpacing(6)
        tag_label = QLabel("分类")
        tag_label.setObjectName("fieldCaption")
        tag_row.addWidget(tag_label)
        self.category_combo = QComboBox()
        self.category_combo.setObjectName("librarySortCombo")
        self.category_combo.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.category_combo.setMinimumWidth(110)
        self.category_combo.addItem("全部分类", FILTER_CATEGORY_ALL)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        tag_row.addWidget(self.category_combo)
        self.btn_add_game_type = QPushButton("新增类型")
        self.btn_add_game_type.setObjectName("libraryHeaderButton")
        self.btn_add_game_type.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_game_type.setToolTip("为当前游戏新增 Mod 类型（不会出现在游戏列表中）")
        self.btn_add_game_type.clicked.connect(self._on_add_game_type)
        tag_row.addWidget(self.btn_add_game_type)
        self.btn_delete_game_type = QPushButton("删除类型")
        self.btn_delete_game_type.setObjectName("libraryHeaderButton")
        self.btn_delete_game_type.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_game_type.setToolTip("删除当前游戏的类型定义，并解除所有 Mod 对该类型的绑定")
        self.btn_delete_game_type.clicked.connect(self._on_delete_game_type)
        tag_row.addWidget(self.btn_delete_game_type)
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
        left_col.addWidget(self._meta_bar, 0)

        self._record_actions = QWidget(self._library_toolbar)
        self._record_actions.setObjectName("libraryRecordActions")
        self._record_actions.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum
        )
        record_col = QVBoxLayout(self._record_actions)
        record_col.setContentsMargins(0, 0, 0, 0)
        record_col.setSpacing(4)
        record_col.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.btn_deployment_record = self._make_library_action_button(
            "💾 部署记录 ▼", parent=self._record_actions
        )
        self.btn_deployment_record.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.btn_deployment_record.setToolTip("部署记录筛选与管理")
        self._deployment_record_menu = QMenu(self.btn_deployment_record)
        self.btn_deployment_record.setMenu(self._deployment_record_menu)
        self._deployment_record_menu.aboutToShow.connect(
            self._rebuild_deployment_record_menu
        )
        record_col.addWidget(self.btn_deployment_record, 0)

        self.btn_wh3_sort_mode = self._make_library_action_button(
            "排序模式", parent=self._record_actions
        )
        self.btn_wh3_sort_mode.setCheckable(True)
        self.btn_wh3_sort_mode.setToolTip(
            "进入已安装 Mod 的 Load Order 排序工作区"
        )
        self.btn_wh3_sort_mode.toggled.connect(self._on_wh3_sort_mode_toggled)
        self.btn_wh3_sort_mode.hide()
        record_col.addWidget(self.btn_wh3_sort_mode, 0)

        self.btn_collection_mode = self._make_library_action_button(
            "合集模式", parent=self._record_actions
        )
        self.btn_collection_mode.setCheckable(True)
        self.btn_collection_mode.setToolTip("进入合集列表工作区")
        self.btn_collection_mode.toggled.connect(self._on_collection_mode_toggled)
        record_col.addWidget(self.btn_collection_mode, 0)

        toolbar_row.addWidget(self._filter_column, 1, Qt.AlignmentFlag.AlignTop)
        toolbar_row.addWidget(
            self._record_actions, 0, Qt.AlignmentFlag.AlignTop
        )
        center_layout.addWidget(self._library_toolbar)
        self._apply_filter_row_height()

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
        # Vertical virtualization pads must NOT be FlowLayout peers — that
        # steals the first card slot (first row shows N-1 mods). Structure:
        #   VBox[ top_pad | cards_host(FlowLayout) | bottom_pad ]
        self._library_vlayout = QVBoxLayout(self.library_host)
        self._library_vlayout.setContentsMargins(0, 0, 0, 0)
        self._library_vlayout.setSpacing(0)

        self._viewport_top_spacer = QWidget(self.library_host)
        self._viewport_top_spacer.setObjectName("libraryViewportTopPad")
        self._viewport_top_spacer.setFixedHeight(0)
        self._viewport_top_spacer.hide()
        self._library_vlayout.addWidget(self._viewport_top_spacer)

        self._cards_host = QWidget(self.library_host)
        self._cards_host.setObjectName("libraryCardsHost")
        self._cards_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        # D-5: tighter card grid density — cards only (no scroll pads).
        self.library_layout = FlowLayout(
            self._cards_host,
            margin=LIBRARY_FLOW_MARGIN,
            h_spacing=LIBRARY_CARD_H_SPACING,
            v_spacing=8,
        )
        self._library_vlayout.addWidget(self._cards_host, stretch=0)

        self._viewport_bottom_spacer = QWidget(self.library_host)
        self._viewport_bottom_spacer.setObjectName("libraryViewportBottomPad")
        self._viewport_bottom_spacer.setFixedHeight(0)
        self._viewport_bottom_spacer.hide()
        self._library_vlayout.addWidget(self._viewport_bottom_spacer)

        # Keep host min-height in sync with flow content so the scrollbar
        # range shrinks when switching to a smaller Mod set.
        self.library_layout.heightChanged.connect(self._on_library_flow_height)
        self.scroll.setWidget(self.library_host)
        self.scroll.verticalScrollBar().valueChanged.connect(
            self._on_library_scroll_value
        )
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
        from services.mod_projection_events import subscribe_mod_changed
        from services.size_observation import subscribe_size_projection

        subscribe_mod_changed(self.on_mod_changed)
        subscribe_size_projection(self.on_size_projection)
        self.destroyed.connect(self._unsubscribe_mod_projection)
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
        self.empty_overlay.setFrameShape(QFrame.Shape.NoFrame)
        self.empty_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.empty_overlay.setAutoFillBackground(False)
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
        # ARCHITECTURE RULE: this overlay must stay a child of the scroll
        # viewport. Never reparent or show it as a top-level window (Import Mod
        # orphan float accident class). See ui.window_lifecycle.
        self.loading_overlay = QFrame(self.scroll.viewport())
        self.loading_overlay.setObjectName("libraryLoadingOverlay")
        self.loading_overlay.setFrameShape(QFrame.Shape.NoFrame)
        # Prevent Windows/native palette from painting a solid white slab.
        self.loading_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.loading_overlay.setAutoFillBackground(False)
        self.loading_overlay.setStyleSheet(
            "background-color: transparent; border: none;"
        )
        load_layout = QVBoxLayout(self.loading_overlay)
        load_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Loading mods...")
        self.loading_label.setObjectName("libraryLoadingLabel")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.loading_label.setAutoFillBackground(False)
        self.loading_label.setStyleSheet(
            "background-color: transparent; border: none;"
        )
        load_layout.addWidget(self.loading_label)
        self.loading_overlay.hide()
        self._sync_type_manage_buttons()

    @staticmethod
    def _make_filter_chip(label: str, parent: QWidget | None = None) -> QPushButton:
        """Filter chip with Fixed size — FlowLayout wraps instead of compressing."""
        btn = QPushButton(label, parent)
        btn.setObjectName("libraryFilterChip")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        btn.setMinimumHeight(28)
        # sizeHint from text+style; Fixed policy prevents QLayout squeeze.
        # Only adjustSize when already parented — never map a parentless button.
        if parent is not None:
            btn.adjustSize()
        else:
            btn.resize(btn.sizeHint())
        return btn

    @staticmethod
    def _make_library_action_button(
        label: str, *, parent: QWidget | None = None
    ) -> QToolButton:
        """Compact right-side action (部署记录 / 排序模式) — shared size & style."""
        btn = QToolButton(parent)
        btn.setObjectName("libraryActionButton")
        btn.setText(label)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(LIBRARY_ACTION_BTN_W, LIBRARY_ACTION_BTN_H)
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

    @traced("ModLibraryView.refresh")
    def refresh(self, *, force: bool = True, reconcile: bool | None = None) -> None:
        """Reload library listing from disk and rebuild cards (UI-thread safe).

        Read + render only — no network, Steam archive, migration, or sync.
        Shows a loading overlay so the window is never silent during work.

        *force*:
          - ``True`` (Refresh button / import): rebuild snapshot from disk
          - ``False`` (nav back to Library): reuse warm ``ModLibraryCache`` when present

        *reconcile*:
          - ``None`` / ``False`` / ``True``: ignored for scheduling. Library is a
            read projection and must not start Reconcile. Startup schedules
            Reconcile from main_window; Refresh only reloads the DB index.

        Optional background Presence Reconcile may run after a forced refresh
        so LIVE/MISS is computed without clicking a card. Identity Reconcile
        is never scheduled here. Full-library L2 scan is never scheduled here.
        """
        del reconcile
        do_reconcile = False
        self._refresh_t0 = time.perf_counter()
        scroll = self._capture_scroll()
        keep_mod_id = str(self._selected_mod_id or "").strip()
        if not keep_mod_id and self._selected_card is not None:
            keep_mod_id = self._selected_card._mod_id()
        self._pending_restore = {
            "scroll": scroll,
            "mod_id": keep_mod_id,
        }
        self._set_loading(True)
        root = Path(self._target_root)
        root.mkdir(parents=True, exist_ok=True)
        # ARCHITECTURE RULE: Library must not call Reconcile (even async).
        # Reconcile is a consistency worker owned by startup / explicit ops.
        if force:
            try:
                from services.startup_io_trace import log_io_event

                log_io_event("library_refresh", "start", force=1)
            except Exception:  # noqa: BLE001
                pass
            try:
                from services.presence_reconcile import schedule_presence_reconcile

                game_folder = ""
                current = str(self._current_game_filter or "").strip()
                if current and current != ALL_GAMES_LABEL:
                    game_folder = current
                # Presence Reconcile is not Identity Reconcile. Library may
                # schedule it so MISS is computed without a card click.
                schedule_presence_reconcile(
                    root, game_folder=game_folder or None
                )
            except Exception:  # noqa: BLE001
                logger.debug("presence reconcile schedule failed", exc_info=True)
            try:
                from services.size_observation import schedule_library_size_refresh

                # Background size only — SQL dirty/force enqueue, never walk here.
                schedule_library_size_refresh(
                    force=True,
                    game_id=int(self.current_game_id or 0) or None,
                )
            except Exception:  # noqa: BLE001
                logger.debug("library size refresh schedule failed", exc_info=True)

        if not force:
            try:
                from services.mod_library_cache import get_library_cache

                cache = get_library_cache()
                snap = cache.peek_snapshot(root)
                if snap is not None:
                    self._library_load_pending = False
                    self._apply_library_snapshot(snap)
                    self._finish_library_load()
                    self._note_refresh_handler(force)
                    return
            except Exception:  # noqa: BLE001
                log_exception("ModLibraryView.refresh.soft_cache")
                logger.debug("soft library cache miss", exc_info=True)

        if _library_load_sync():
            try:
                from services.mod_library_cache import get_library_cache

                snapshot = get_library_cache().load_snapshot(root, force=True)
                self._apply_library_snapshot(snapshot)
            finally:
                self._library_load_pending = False
                self._finish_library_load()
            self._note_refresh_handler(force)
            return

        self._library_load_pending = True
        del do_reconcile
        # ARCHITECTURE RULE: do not defer Loading Mods until reconcile/identity
        # finishes. Snapshot load reads existing entities; reconcile is background
        # consistency only (scheduled outside Library).
        self._flush_pending_library_load()
        self._note_refresh_handler(force)

    def _note_refresh_handler(self, force: bool) -> None:
        try:
            from services.library_perf_metrics import get_library_perf_metrics

            t0 = float(getattr(self, "_refresh_t0", 0.0) or 0.0)
            ms = (time.perf_counter() - t0) * 1000.0 if t0 else 0.0
            get_library_perf_metrics().note("refresh_handler_ms", round(ms, 2))
            get_library_perf_metrics().note("refresh_handler_force", int(bool(force)))
        except Exception:  # noqa: BLE001
            pass

    def _start_library_worker(self, root: Path, *, force: bool = True) -> None:
        from ui.library_load_thread import LibraryLoadWorker

        old = self._load_worker
        if old is not None:
            try:
                old.loaded.disconnect()
                old.failed.disconnect()
            except Exception:  # noqa: BLE001
                pass
            old.requestInterruption()
            self._load_worker = None
        self._load_gen += 1
        gen = self._load_gen
        worker = LibraryLoadWorker(
            root, generation=gen, force=force, parent=self
        )
        worker.loaded.connect(lambda snap, g=gen: self._on_library_loaded(snap, g))
        worker.failed.connect(lambda msg, g=gen: self._on_library_failed(msg, g))
        self._load_worker = worker
        worker.start()

    def cancel_pending_library_load(self) -> None:
        """Drop a deferred snapshot if Library is no longer the active page."""
        if not self._library_load_pending:
            return
        self._library_load_pending = False
        running = self._load_worker is not None and self._load_worker.isRunning()
        if not running:
            self._set_loading(False)

    def library_load_is_running(self) -> bool:
        worker = self._load_worker
        return worker is not None and worker.isRunning()

    def shutdown_workers(self) -> None:
        """Stop LibraryLoadWorker and pending cover tasks. Does not change lazy-load policy."""
        self._library_load_pending = False
        worker = self._load_worker
        if worker is not None and worker.isRunning():
            try:
                worker.requestInterruption()
            except Exception:  # noqa: BLE001
                pass
            worker.wait(3000)
        self._cancel_all_pending_covers()
        try:
            from services.cover_loader import CoverLoaderManager

            mgr = CoverLoaderManager._instance
            if mgr is not None:
                pool = getattr(mgr, "_pool", None)
                if pool is not None:
                    pool.clear()
                    pool.waitForDone(2000)
        except Exception:  # noqa: BLE001
            logger.debug("cover pool drain failed", exc_info=True)

    def _on_reconcile_idle(self) -> None:
        self._reconcile_idle.emit()

    @Slot()
    def _resync_projections_after_reconcile(self) -> None:
        """Main-thread Projection rebind for path rebinds — no full Library refresh."""
        try:
            from services.library_reconcile import take_projection_touch_ids
            from services.mod_projection_events import notify_mod_changed
        except Exception:  # noqa: BLE001
            return
        for mid in take_projection_touch_ids():
            notify_mod_changed(mid)

    @Slot()
    def _flush_pending_library_load(self) -> None:
        if _library_load_sync():
            self._library_load_pending = False
            return
        try:
            from services.startup_io_trace import log_io_event

            log_io_event(
                "library_load",
                "flush",
                pending=int(self._library_load_pending),
            )
        except Exception:  # noqa: BLE001
            pass
        if not self._library_page_wants_snapshot():
            if self._library_load_pending:
                self._library_load_pending = False
                running = (
                    self._load_worker is not None and self._load_worker.isRunning()
                )
                if not running:
                    self._set_loading(False)
            return
        if self._load_worker is not None and self._load_worker.isRunning():
            self._library_load_pending = False
            return
        self._library_load_pending = False
        self._start_library_worker(Path(self._target_root), force=True)

    def _library_page_wants_snapshot(self) -> bool:
        # Nav-away calls cancel_pending_library_load(); do not use isVisible()
        # here — the stacked page can report hidden during early show().
        return bool(self._library_load_pending)

    @traced("ModLibraryView._on_library_loaded")
    def _on_library_loaded(self, snapshot, generation: int) -> None:
        t0 = time.perf_counter()
        if int(generation) != self._load_gen:
            return
        try:
            self._apply_library_snapshot(snapshot)
        finally:
            self._finish_library_load()
            try:
                from services.library_perf_metrics import get_library_perf_metrics

                get_library_perf_metrics().note(
                    "apply_on_gui_ms",
                    round((time.perf_counter() - t0) * 1000.0, 2),
                )
            except Exception:  # noqa: BLE001
                pass

    def _on_library_failed(self, message: str, generation: int) -> None:
        if int(generation) != self._load_gen:
            return
        logger.warning("library load failed: %s", message)
        self._set_loading(False)
        self._pending_restore = None

    @traced("ModLibraryView._apply_library_snapshot")
    def _apply_library_snapshot(self, snapshot) -> None:
        from services.ui_block_trace import ui_block_phase

        t0 = time.perf_counter()
        self._library_snapshot = snapshot
        self._snapshot_dirty = False
        manager = ModFileManager(self._target_root)
        previous = self._current_game_filter
        pending = self._pending_game_filter
        with ui_block_phase(
            "library_rebuild_game_list",
            cards=int(getattr(snapshot, "total_count", 0) or 0),
        ):
            t_game = time.perf_counter()
            self._rebuild_game_list(manager, prefer=pending or previous, snapshot=snapshot)
            game_ms = (time.perf_counter() - t_game) * 1000.0
        with ui_block_phase(
            "library_render_mod_cards",
            cards=int(getattr(snapshot, "total_count", 0) or 0),
        ):
            t_render = time.perf_counter()
            self._render_mod_cards(manager, force_reload=False)
            render_ms = (time.perf_counter() - t_render) * 1000.0
        try:
            from services.library_perf_metrics import get_library_perf_metrics
            from services.ui_block_trace import log_ui_block
            from services.perf_stage import log_perf_stage

            total_ms = (time.perf_counter() - t0) * 1000.0
            get_library_perf_metrics().note(
                "apply_snapshot_ms",
                round(total_ms, 2),
            )
            get_library_perf_metrics().note("apply_rebuild_game_list_ms", round(game_ms, 2))
            get_library_perf_metrics().note("apply_render_mod_cards_ms", round(render_ms, 2))
            log_ui_block(
                "library_apply_snapshot",
                total_ms,
                game_ms=round(game_ms, 1),
                render_ms=round(render_ms, 1),
                cards=int(getattr(snapshot, "total_count", 0) or 0),
            )
            log_perf_stage(
                "snapshot_apply",
                total_ms,
                game_ms=round(game_ms, 1),
                render_ms=round(render_ms, 1),
                card_data=int(getattr(snapshot, "total_count", 0) or 0),
                widgets_created=int(getattr(self, "_card_create_count", 0) or 0),
                widgets_cached=len(self._card_cache),
                viewport_cards=len(self._cards),
            )
        except Exception:  # noqa: BLE001
            pass

    @traced("ModLibraryView._finish_library_load")
    def _finish_library_load(self) -> None:
        pending = self._pending_restore or {}
        keep_mod_id = str(pending.get("mod_id") or "").strip()
        scroll = int(pending.get("scroll") or 0)
        focus_id = keep_mod_id
        if keep_mod_id:
            restored = self._card_for_mod_id(keep_mod_id)
            if restored is not None and not restored.isHidden():
                self._select_card(restored, show_panel=True)
                focus_id = restored._mod_id() or keep_mod_id
            else:
                self._clear_selection()
                self.detail_panel.clear()
        else:
            self.detail_panel.clear()
        self._set_loading(False)
        self._restore_scroll_after_layout(scroll, focus_mod_id=focus_id)
        self._pending_restore = None

    def _unsubscribe_mod_projection(self, *_args: object) -> None:
        try:
            from services.mod_projection_events import unsubscribe_mod_changed

            unsubscribe_mod_changed(self.on_mod_changed)
        except Exception:  # noqa: BLE001
            pass
        try:
            from services.size_observation import unsubscribe_size_projection

            unsubscribe_size_projection(self.on_size_projection)
        except Exception:  # noqa: BLE001
            pass

    def _mark_card_stale(self, card) -> None:
        """Drop card Projection bind only — never force a full Library rebuild."""
        card._card_data = None

    @traced("ModLibraryView.on_mod_changed")
    def on_mod_changed(self, mod_id: str) -> None:
        """
        Projection → Viewport sink for ``notify_mod_changed(mod_id)``.

        Cache already ran ``refresh_projection``. UI work is coalesced into one
        pass so a burst of ids cannot each force filter/sort/viewport rebuild.
        """
        self._queue_projection_patch(mod_id, kind="full")

    def on_size_projection(self, obs) -> None:
        """Size observation → card/index only. Never ``notify_mod_changed``."""
        mid = str(getattr(obs, "internal_id", "") or "").strip()
        self._queue_projection_patch(mid, kind="size")

    def _queue_projection_patch(self, mod_id: str, *, kind: str) -> None:
        mid = str(mod_id or "").strip()
        if not mid:
            return
        prev = self._pending_projection.get(mid)
        if prev == "full":
            kind = "full"
        self._pending_projection[mid] = kind
        timer = getattr(self, "_projection_coalesce", None)
        if timer is None:
            self._flush_projection_patches()
            return
        if not timer.isActive():
            timer.start()

    def _flush_projection_patches(self) -> None:
        pending = dict(self._pending_projection)
        self._pending_projection.clear()
        if not pending:
            return
        if self._is_collection_list_mode():
            return
        from services.mod_library_cache import (
            card_data_to_metadata,
            get_library_cache,
        )

        t0 = time.perf_counter()
        cache = get_library_cache()
        needs_view = False
        any_full = False
        selected_refresh: str | None = None
        query = self.search_box.text() if hasattr(self, "search_box") else ""
        filter_key = FILTER_ALL
        if not self._is_collection_content_mode():
            filter_key = self._status_filter
        category_key = (
            self._sidebar_category
            if self._sidebar_category
            else self._category_filter
        )
        recorded_ids = self._resolve_record_mod_ids()
        deployed_only = bool(
            getattr(self, "_wh3_sort_mode", False)
        ) and self._is_load_order_sort_game()

        old_indexes: dict[str, ModFilterIndex] = {}
        for index, _payload in self._game_row_entries:
            mid = str(getattr(index, "mod_id", "") or "")
            if mid:
                old_indexes[mid] = index

        patches: dict[str, tuple[str, object]] = {}
        for mid, kind in pending.items():
            if kind == "full":
                any_full = True
            patched = cache.get_card_data(mid)
            if patched is None and kind == "full":
                try:
                    patched = cache.refresh_projection(mid)
                except Exception:  # noqa: BLE001
                    log_exception(
                        "ModLibraryView._flush_projection_patches.refresh_projection",
                        mod_id=mid,
                    )
                    patched = None
            if patched is None:
                if kind == "full":
                    self._snapshot_dirty = True
                continue
            patches[mid] = (kind, patched)

        peek = None
        try:
            peek = cache.peek_snapshot(self._target_root)
        except Exception:  # noqa: BLE001
            peek = None
        if peek is not None:
            self._library_snapshot = peek
        elif self._library_snapshot is not None and patches:
            replaced = {
                mid: patched for mid, (_kind, patched) in patches.items()
            }
            cards = [
                replaced.get(str(c.id), c) for c in self._library_snapshot.cards
            ]
            self._library_snapshot.cards[:] = cards
            self._library_snapshot.total_count = len(cards)

        if patches:
            new_indexes = {
                mid: self._filter_index_from_card_data(patched)
                for mid, (_kind, patched) in patches.items()
            }

            def _patch_all(rows: list[tuple[ModFilterIndex, object]]):
                out: list[tuple[ModFilterIndex, object]] = []
                for index, payload in rows:
                    mid = str(getattr(index, "mod_id", "") or "")
                    hit = patches.get(mid)
                    if hit is None:
                        out.append((index, payload))
                        continue
                    out.append((new_indexes[mid], hit[1]))
                return out

            self._game_row_entries = _patch_all(self._game_row_entries)
            self._filtered_row_entries = _patch_all(self._filtered_row_entries)

            bound = {
                card._mod_id(): (i, card)
                for i, (_index, card) in enumerate(self._card_entries)
            }
            for mid, (kind, patched) in patches.items():
                new_index = new_indexes[mid]
                old_index = old_indexes.get(mid)
                hit = bound.get(mid)
                if hit is not None:
                    i, card = hit
                    meta = card_data_to_metadata(patched)
                    folder = card.managed_path
                    patched_path = str(patched.managed_path or "").strip()
                    if patched_path:
                        folder = Path(patched_path)
                    card.rebind(folder, meta, card_data=patched)
                    self._card_entries[i] = (new_index, card)
                    if self._selected_card is card:
                        self._selected_path = card.managed_path
                        self._selected_mod_id = mid
                if projection_requires_view_recompute(
                    old_index,
                    new_index,
                    query=query,
                    filter_key=filter_key,
                    platform_key=FILTER_PLATFORM_ALL,
                    category_key=category_key,
                    sort_mode=self._sort_mode,
                    record_mod_ids=recorded_ids,
                    deployed_only=deployed_only,
                ):
                    needs_view = True
                selected = self._selected_card
                if (
                    kind == "full"
                    and selected is not None
                    and selected._mod_id() == mid
                    and not selected.isHidden()
                    and not getattr(self.detail_panel, "_deploy_busy", False)
                ):
                    selected_refresh = mid

        if needs_view:
            self._last_filter_sig = None
            self._apply_view_filter()
        if any_full or needs_view:
            try:
                self._schedule_visible_covers()
            except Exception:  # noqa: BLE001
                log_exception(
                    "ModLibraryView._flush_projection_patches.schedule_visible_covers"
                )
        if selected_refresh:
            selected = self._selected_card
            if (
                selected is not None
                and selected._mod_id() == selected_refresh
                and not selected.isHidden()
                and not getattr(self.detail_panel, "_deploy_busy", False)
            ):
                self._sync_peer_mods_to_panel(exclude=selected_refresh)
                self.detail_panel.show_mod(
                    selected.managed_path,
                    mod_id=selected_refresh,
                    game_id=int(self.current_game_id or 0),
                    game_name=str(self.current_game_name or "").strip(),
                )
        try:
            from services.library_perf_metrics import get_library_perf_metrics

            get_library_perf_metrics().note(
                "projection_flush_ms",
                round((time.perf_counter() - t0) * 1000.0, 2),
            )
            get_library_perf_metrics().note("projection_patched", len(patches))
        except Exception:  # noqa: BLE001
            pass

    def _sync_projection_content_status(self, mod_id: str) -> None:
        """Refresh full projection after content_status persistence."""
        mid = str(mod_id or "").strip()
        if not mid:
            return
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)

    def _sync_projection_list_sort_fields(self, mod_id: str) -> None:
        """Refresh full projection after metadata/sort field persistence."""
        mid = str(mod_id or "").strip()
        if not mid or not mid.isdigit():
            return
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)

    def _capture_scroll(self) -> int:
        return int(self.scroll.verticalScrollBar().value())

    def _viewport_metrics(self) -> tuple[int, int]:
        vp = self.scroll.viewport()
        viewport_w = int(vp.width()) if vp is not None else int(self.library_host.width())
        viewport_h = int(vp.height()) if vp is not None else 600
        if viewport_w <= 0:
            viewport_w = 800
        if viewport_h <= 0:
            viewport_h = 600
        return viewport_w, viewport_h

    def _recompute_viewport_geometry(self, item_count: int) -> tuple[int, int, int]:
        """Apply host height for the current filtered count; return w/h/total."""
        viewport_w, viewport_h = self._viewport_metrics()
        total_h = estimate_total_height(item_count, viewport_w)
        self.library_host.setMinimumHeight(max(0, total_h))
        self.library_host.updateGeometry()
        self.scroll.updateGeometry()
        return viewport_w, viewport_h, total_h

    def _clamp_scroll_for_item_count(self, scroll_y: int, item_count: int) -> int:
        viewport_w, viewport_h = self._viewport_metrics()
        return clamp_scroll_y(
            scroll_y,
            item_count,
            viewport_w,
            viewport_h,
        )

    def _legalize_viewport_scroll(
        self,
        scroll_y: int,
        item_count: int | None = None,
    ) -> int:
        """Recompute geometry, clamp ``scroll_y``, and apply it to the bar."""
        n = (
            self._viewport_item_count()
            if item_count is None
            else max(0, int(item_count))
        )
        viewport_w, viewport_h, _total_h = self._recompute_viewport_geometry(n)
        legal = clamp_scroll_y(
            scroll_y,
            n,
            viewport_w,
            viewport_h,
        )
        if int(self._capture_scroll()) != int(legal):
            self._set_scroll_value(legal)
        return legal

    def _schedule_viewport_scroll_clamp(self) -> None:
        timer = getattr(self, "_viewport_clamp_timer", None)
        if timer is None:
            self._clamp_viewport_after_layout()
            return
        timer.start()

    def _clamp_viewport_after_layout(self) -> None:
        """Resize/layout may change columns and max_scroll — clamp then rebind."""
        if getattr(self, "_viewport_syncing", False):
            return
        n = self._viewport_item_count()
        raw = self._capture_scroll()
        legal = self._legalize_viewport_scroll(raw, n)
        if n and raw != legal:
            self._sync_viewport_cards(scroll_y=legal)

    def _set_scroll_value(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(int(value), bar.maximum())))

    def _on_library_flow_height(self, height: int) -> None:
        """Keep host tall enough for virtualized total content, not just the window."""
        vp = self.scroll.viewport()
        viewport_w = int(vp.width()) if vp is not None else int(self.library_host.width())
        if viewport_w <= 0:
            viewport_w = 800
        estimated = estimate_total_height(self._viewport_item_count(), viewport_w)
        top_h = (
            int(self._viewport_top_spacer.height())
            if self._viewport_top_spacer is not None
            else 0
        )
        bot_h = (
            int(self._viewport_bottom_spacer.height())
            if self._viewport_bottom_spacer is not None
            else 0
        )
        stacked = top_h + max(0, int(height)) + bot_h
        self.library_host.setMinimumHeight(max(0, stacked, estimated))
        self._schedule_visible_covers()

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
        key = self._card_cache_key(Path("."), mod_id=mid)
        cached = self._card_cache.get(key) if key else None
        if cached is not None and str(cached._mod_id() or "") == mid:
            return cached
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
            n_vis = len(self._filtered_row_entries)
            self._set_scroll_value(self._clamp_scroll_for_item_count(value, n_vis))

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

    @traced("ModLibraryView._on_import_single_mod")
    def _on_import_single_mod(self) -> None:
        """Option A: existing single Mod / archive import dialog (asks for links)."""
        # ARCHITECTURE RULE: Import Mod once spawned orphan Qt floats when
        # dialog controls became visible without parents, and when library
        # refresh/overlays ran under a still-modal dialog. Ownership first —
        # never paper over with hide/close. See ui.window_lifecycle.
        from ui.mod_import_dialog import ModImportDialog
        from ui.window_lifecycle import exec_dialog

        context = self._require_import_game_context()
        if context is None:
            return

        dialog = ModImportDialog(
            self._target_root,
            parent=self,
            game_context=context,
        )

        imported_ok = False

        def _mark_imported(_result: object) -> None:
            nonlocal imported_ok
            imported_ok = True

        dialog.imported.connect(_mark_imported)
        exec_dialog(dialog)
        # Refresh only after the modal closes — never drive library overlays
        # while another top-level dialog still owns the event loop.
        if imported_ok:
            self.refresh(force=True, reconcile=False)

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
        from ui.window_lifecycle import register_toplevel

        register_toplevel(progress)
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
            self.refresh(force=True, reconcile=False)

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

    def _on_import_batch_offline_html(self) -> None:
        """Batch-import multiple offline HTML/MHTML pages as Nexus Mods."""
        context = self._require_import_game_context()
        if context is None:
            return

        from core.mod_platform import PLATFORM_NEXUS
        from services.importers.offline_html_batch import normalize_offline_html_paths
        from services.importers.import_settings import (
            resolve_import_start_directory,
            set_last_import_directory,
        )
        from ui.import_thread import ImportWorker

        if self._batch_import_worker is not None and self._batch_import_worker.isRunning():
            QMessageBox.information(self, "导入进行中", "请等待当前批量导入完成。")
            return

        start = resolve_import_start_directory()
        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "选择离线页面（可多选）",
            start,
            "Offline Web Page (*.html *.htm *.mhtml *.mht);;所有文件 (*.*)",
        )
        if not chosen:
            return
        set_last_import_directory(chosen[0])
        html_paths = normalize_offline_html_paths(chosen)
        if not html_paths:
            QMessageBox.warning(self, "批量导入", "未选择有效的离线页面文件。")
            return

        params = {
            "folder": "",
            "source_path": "",
            "use_archive": False,
            "archive_paths": [],
            "nexus_url": "",
            "nexus_id": "",
            "title": "",
            "cover_source": "",
            "offline_html_path": "",
            "offline_html_paths": [str(p) for p in html_paths],
            "offline_clean": True,
            "is_batch_mode": False,
            "context": dict(context),
            "game_id": int(context.get("game_id") or 0),
            "game_name": str(context.get("game_name") or ""),
            "app_id": int(context.get("game_id") or 0),
        }

        progress = QProgressDialog(
            f"正在批量导入 {len(html_paths)} 个离线页面…",
            "取消",
            0,
            0,
            self,
        )
        from ui.window_lifecycle import register_toplevel

        register_toplevel(progress)
        progress.setWindowTitle("批量导入离线页面")
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
            from services.importers.importer_base import ImportResult

            assert isinstance(result, ImportResult)
            imported = int(result.imported_count or 0)
            skipped = int(result.skipped_count or 0)
            failed = int(getattr(result, "failed_count", 0) or 0)
            summary = (
                f"导入完成\n\n成功：{imported}\n跳过：{skipped}\n失败：{failed}"
            )
            progress.setLabelText(summary)
            progress.close()
            self._batch_import_worker = None
            self.refresh(force=True, reconcile=False)

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
            # DB-first fallback: match ModListItem game_folder (no FS resolve).
            for row in db.list_mod_list_items(game_folder=name):
                gid = int(row.get("game_id") or 0)
                if gid > 0:
                    return gid
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
        prev_game_id = self.current_game_id
        self.current_game_name = name
        self._current_game_filter = name
        if game_id is not None and int(game_id) > 0:
            self.current_game_id = int(game_id)
        else:
            self.current_game_id = self._resolve_game_id(name) if name else None
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and prev_game_id != self.current_game_id
        ):
            self._set_library_status_filter(FILTER_ALL)
        if prev_game_id != self.current_game_id and self._is_collection_workspace():
            self._exit_collection_mode(apply_filter=False, restore_chips=True)
        self._sync_wh3_activation_bar()
        self._sync_collection_mode_button()
        self._sync_type_manage_buttons()
        self._refresh_game_header()

    def _lookup_game_status_summary(self, game_folder: str | None):
        key = str(game_folder or "").strip()
        if not key:
            return None
        snap = self._library_snapshot
        if snap is None:
            return None
        for g in snap.games:
            if g.folder == key:
                return getattr(g, "status_summary", None)
        return None

    def _refresh_game_header(self) -> None:
        """Reuse existing page title / count labels for game status (no new page)."""
        if self._is_collection_content_mode():
            self._sync_collection_header()
            return
        from services.game_status import format_status_tooltip, header_status_line

        name = (self.current_game_name or "").strip()
        if not name:
            self._page_title.setText("Mod 库")
            return
        summary = self._lookup_game_status_summary(name)
        display = name
        snap = self._library_snapshot
        if snap is not None:
            for g in snap.games:
                if g.folder == name:
                    display = str(g.display or name)
                    break
        self._page_title.setText(f"🎮 {display}")
        install_warn = ""
        try:
            from services.deploy_status import install_path_missing

            gid = int(self.current_game_id or 0)
            if gid > 0:
                cfg = get_db().get_game_deploy_config(gid)
                if cfg is not None and install_path_missing(cfg.install_path):
                    install_warn = "⚠ 游戏目录不存在"
        except Exception:  # noqa: BLE001
            install_warn = ""
        if summary is not None:
            status_line = header_status_line(summary)
            tip = format_status_tooltip(summary)
            if install_warn:
                tip = (tip + "\n" if tip else "") + install_warn
                status_line = (
                    f"{install_warn} · {status_line}" if status_line else install_warn
                )
            if status_line:
                self._page_title.setToolTip(tip or status_line)
            # count_label is updated in _apply_view_filter; stash status for merge
            self._pending_game_status_line = status_line
        else:
            self._pending_game_status_line = install_warn
            tip = f"库路径：{self._target_root}"
            if install_warn:
                tip = install_warn + "\n" + tip
            self._page_title.setToolTip(tip)

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
        """Refresh projection after offline archive — internal_id only."""
        raw = str(mod_path or "").strip()
        mid = raw if raw.isdigit() else ""
        if not mid:
            mid = str(
                getattr(self.detail_panel, "current_mod_id", lambda: "")() or ""
            ).strip()
        if not mid:
            mid = str(self._selected_mod_id or "").strip()
        if not mid:
            return
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)
        scroll = self._capture_scroll()
        self._restore_scroll_after_layout(scroll, focus_mod_id=mid)

    def _set_loading(self, loading: bool) -> None:
        self._loading = bool(loading)
        self.refresh_btn.setEnabled(not loading)
        self.search_box.setEnabled(not loading)
        self.sort_combo.setEnabled(not loading)
        for btn in self._filter_buttons.values():
            btn.setEnabled(not loading)
        if hasattr(self, "btn_collection_mode"):
            self.btn_collection_mode.setEnabled(
                (not loading) and int(self.current_game_id or 0) > 0
            )
        if loading:
            # D-8: center viewport only — avoid whole-page cover / flash
            from ui.popup_trace import log_popup

            log_popup("libraryLoadingOverlay.show")
            vp = self.scroll.viewport()
            self.loading_overlay.setGeometry(vp.rect())
            self.loading_overlay.show()
            self.loading_overlay.raise_()
        else:
            self.loading_overlay.hide()

    # ------------------------------------------------------------------
    # Search / status filter / sort
    # ------------------------------------------------------------------

    def _on_search_text_changed(self, *_args) -> None:
        """Debounce search typing — avoid layout thrash per keystroke."""
        self._search_debounce.start()

    def _on_search_or_filter_changed(self, *_args) -> None:
        self._search_debounce.stop()
        self._apply_view_filter()

    def _resolve_record_mod_ids(self) -> frozenset[str] | None:
        """Load recorded ids only when status filter is DEPLOYMENT_RECORD."""
        if self._status_filter != FILTER_DEPLOYMENT_RECORD:
            return None
        rid = self._deployment_record_id
        if rid is None:
            return frozenset()
        if self._cached_record_mod_ids is not None:
            return self._cached_record_mod_ids
        from services import deployment_record as dr

        ids = frozenset(dr.get_record_mod_ids(rid))
        self._cached_record_mod_ids = ids
        return ids

    def _sync_record_overlays(self) -> None:
        """
        Relative badges exist only while FILTER_DEPLOYMENT_RECORD is active.

        Must run independently of filter_sig early-return, and must clear the
        card cache (reused widgets), not only current ``_card_entries``.
        """
        if self._status_filter != FILTER_DEPLOYMENT_RECORD:
            self._clear_all_record_overlays()
            return
        recorded = self._resolve_record_mod_ids()
        if recorded is None:
            self._clear_all_record_overlays()
            return
        seen: set[int] = set()
        for index, card in self._card_entries:
            card.set_record_relative_status(
                compute_record_relative_status(index, recorded)
            )
            seen.add(id(card))
        for card in list(getattr(self, "_card_cache", {}).values()):
            if id(card) not in seen:
                card.clear_record_overlay()

    def _clear_all_record_overlays(self) -> None:
        for card in list(getattr(self, "_card_cache", {}).values()):
            card.clear_record_overlay()
        for _index, card in self._card_entries:
            card.clear_record_overlay()

    def _update_deployment_record_button_label(self) -> None:
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and self._deployment_record_name
        ):
            self.btn_deployment_record.setText(
                f"💾 {self._deployment_record_name} ▼"
            )
        else:
            self.btn_deployment_record.setText("💾 部署记录 ▼")

    def _is_wh3_current_game(self) -> bool:
        from services.wh3_activation import is_wh3_activation_app

        if is_wh3_activation_app(
            self.current_game_id or 0,
            self.current_game_name or "",
        ):
            return True
        if not self._current_game_filter:
            return False
        ids = [
            str(getattr(index, "mod_id", "") or "")
            for index, _payload in self._game_row_entries
            if str(getattr(index, "mod_id", "") or "").isdigit()
        ]
        if not ids:
            return False
        try:
            app_ids = get_db().get_mods_app_ids(ids)
        except Exception:  # noqa: BLE001
            return False
        return any(is_wh3_activation_app(app) for app in app_ids.values())

    def _is_stellaris_current_game(self) -> bool:
        from services.stellaris_activation import is_stellaris_activation_app

        if is_stellaris_activation_app(
            self.current_game_id or 0,
            self.current_game_name or "",
        ):
            return True
        if not self._current_game_filter:
            return False
        ids = [
            str(getattr(index, "mod_id", "") or "")
            for index, _payload in self._game_row_entries
            if str(getattr(index, "mod_id", "") or "").isdigit()
        ]
        if not ids:
            return False
        try:
            app_ids = get_db().get_mods_app_ids(ids)
        except Exception:  # noqa: BLE001
            return False
        return any(is_stellaris_activation_app(app) for app in app_ids.values())

    def _is_load_order_sort_game(self) -> bool:
        return self._is_wh3_current_game() or self._is_stellaris_current_game()

    def _wh3_library_root(self) -> Path:
        return Path(self._target_root)

    def _persist_wh3_activation_state(self) -> None:
        """After deploy/remove: append/drop load-order ids and rewrite used_mods.txt.

        Never copies ``.pack`` files.
        """
        if self._is_stellaris_current_game():
            from services.stellaris_activation import (
                load_saved_order,
                persist_load_order,
                sync_stellaris_launcher,
            )

            try:
                persist_load_order(load_saved_order())
                sync_stellaris_launcher()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Stellaris activation persist after inventory change failed",
                    exc_info=True,
                )
            return
        if not self._is_wh3_current_game():
            return
        from services.wh3_activation import (
            load_saved_order,
            persist_load_order,
            sync_used_mods_txt,
        )

        try:
            persist_load_order(
                load_saved_order(), library_root=self._wh3_library_root()
            )
            sync_used_mods_txt(library_root=self._wh3_library_root())
        except Exception:  # noqa: BLE001
            logger.debug("WH3 activation persist after inventory change failed", exc_info=True)

    def _filter_chip_row_height(self) -> int:
        """Single Filter-chip row height. Independent of Mode column."""
        h = 0
        for btn in self._filter_buttons.values():
            h = max(h, int(btn.sizeHint().height()), int(btn.minimumHeight() or 0))
        return max(h, 28)

    def _apply_filter_row_height(self) -> None:
        """Cap Filter chips to one row of chip height — never Mode stack height.

        `_status_chips` uses FlowLayout height-for-width. A narrow pass can
        request ~200px; capping to chip height prevents a phantom gap above 分类
        without coupling to the Mode column.
        """
        h = self._filter_chip_row_height()
        self._status_chips.setMaximumHeight(h)
        self._status_chips.updateGeometry()
        if getattr(self, "_filter_column", None) is not None:
            self._filter_column.updateGeometry()

    def _sync_wh3_activation_bar(self) -> None:
        if not hasattr(self, "btn_wh3_sort_mode"):
            return
        visible = self._is_load_order_sort_game()
        self.btn_wh3_sort_mode.setVisible(visible)
        stellaris = self._is_stellaris_current_game()
        if visible and stellaris:
            self.btn_wh3_sort_mode.setToolTip(
                "进入 Stellaris 已启用 Mod 的 Load Order 排序工作区"
            )
        elif visible:
            self.btn_wh3_sort_mode.setToolTip(
                "进入 WH3 已部署 Mod 的 Load Order 排序工作区"
            )
        if getattr(self, "_record_actions", None) is not None:
            self._record_actions.updateGeometry()
        if not visible and getattr(self, "_wh3_sort_mode", False):
            self._exit_wh3_sort_mode(apply_filter=False)

    def _on_wh3_sort_mode_toggled(self, checked: bool) -> None:
        if getattr(self, "_suppress_wh3_sort_toggle", False):
            return
        if checked and self._is_collection_workspace():
            self._exit_collection_mode(apply_filter=False, restore_chips=True)
        if checked and not self._is_load_order_sort_game():
            self._exit_wh3_sort_mode(apply_filter=False)
            return
        self._wh3_sort_mode = bool(checked)
        self._last_filter_sig = None
        self._apply_view_filter()

    def _exit_wh3_sort_mode(self, *, apply_filter: bool = True) -> None:
        self._wh3_sort_mode = False
        if hasattr(self, "btn_wh3_sort_mode") and self.btn_wh3_sort_mode.isChecked():
            self._suppress_wh3_sort_toggle = True
            try:
                self.btn_wh3_sort_mode.setChecked(False)
            finally:
                self._suppress_wh3_sort_toggle = False
        if apply_filter:
            self._last_filter_sig = None
            self._apply_view_filter()

    def _is_collection_list_mode(self) -> bool:
        return str(getattr(self, "_collection_mode", COLLECTION_MODE_NORMAL)) == (
            COLLECTION_MODE_LIST
        )

    def _is_collection_content_mode(self) -> bool:
        return str(getattr(self, "_collection_mode", COLLECTION_MODE_NORMAL)) == (
            COLLECTION_MODE_CONTENT
        )

    def _is_collection_workspace(self) -> bool:
        return self._is_collection_list_mode() or self._is_collection_content_mode()

    def _sync_collection_mode_button(self) -> None:
        if not hasattr(self, "btn_collection_mode"):
            return
        has_game = int(self.current_game_id or 0) > 0
        self.btn_collection_mode.setEnabled(has_game and not self._loading)
        if not has_game and self._is_collection_workspace():
            self._exit_collection_mode(apply_filter=False, restore_chips=True)

    def _dim_status_filter_chips(self) -> None:
        """Grey all five status chips without changing ``_status_filter``."""
        self._suppress_filter_toggle = True
        try:
            self._filter_group.setExclusive(False)
            for sk, _label in STATUS_FILTER_LABELS:
                btn = self._filter_buttons.get(sk)
                if btn is not None:
                    btn.setChecked(False)
        finally:
            self._suppress_filter_toggle = False

    def _restore_status_filter_chips(self) -> None:
        if self._status_filter == FILTER_DEPLOYMENT_RECORD:
            return
        if not self._filter_group.exclusive():
            self._filter_group.setExclusive(True)
        btn = self._filter_buttons.get(self._status_filter)
        if btn is not None and not btn.isChecked():
            self._suppress_filter_toggle = True
            try:
                btn.setChecked(True)
            finally:
                self._suppress_filter_toggle = False

    def _on_collection_mode_toggled(self, checked: bool) -> None:
        if getattr(self, "_suppress_collection_toggle", False):
            return
        if checked:
            self._enter_collection_mode()
        else:
            self._exit_collection_mode(apply_filter=True, restore_chips=True)

    def _enter_collection_mode(self) -> None:
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            QMessageBox.information(
                self, "合集模式", "请先选择一个具体游戏再进入合集模式。"
            )
            self._suppress_collection_toggle = True
            try:
                self.btn_collection_mode.setChecked(False)
            finally:
                self._suppress_collection_toggle = False
            return
        if getattr(self, "_wh3_sort_mode", False):
            self._exit_wh3_sort_mode(apply_filter=False)
        self._collection_mode = COLLECTION_MODE_LIST
        self._current_collection_id = None
        if hasattr(self, "btn_collection_mode") and not self.btn_collection_mode.isChecked():
            self._suppress_collection_toggle = True
            try:
                self.btn_collection_mode.setChecked(True)
            finally:
                self._suppress_collection_toggle = False
        self._clear_selection()
        self.detail_panel.clear()
        self._dim_status_filter_chips()
        self._force_scroll_zero = True
        self._last_filter_sig = None
        self._sync_collection_header()
        self._apply_view_filter()

    def _open_collection_content(self, collection_id: int) -> None:
        """Collection List → Collection Content. Never treats Collection as a Mod."""
        try:
            cid = int(collection_id)
        except (TypeError, ValueError):
            return
        if cid <= 0:
            return
        from services import collection as coll

        rec = coll.get_collection(cid)
        gid = int(self.current_game_id or 0)
        if rec is None or int(rec.app_id or 0) != gid:
            return
        if getattr(self, "_wh3_sort_mode", False):
            self._exit_wh3_sort_mode(apply_filter=False)
        self._collection_mode = COLLECTION_MODE_CONTENT
        self._current_collection_id = cid
        if hasattr(self, "btn_collection_mode") and not self.btn_collection_mode.isChecked():
            self._suppress_collection_toggle = True
            try:
                self.btn_collection_mode.setChecked(True)
            finally:
                self._suppress_collection_toggle = False
        self._clear_selection()
        self.detail_panel.clear()
        self._dim_status_filter_chips()
        self._force_scroll_zero = True
        self._last_filter_sig = None
        self._sync_collection_header()
        self._apply_view_filter()

    def _return_to_collection_list(self) -> None:
        """Collection Content → Collection List. Does not enter WH3 or change Filters."""
        if not self._is_collection_content_mode():
            return
        self._collection_mode = COLLECTION_MODE_LIST
        self._current_collection_id = None
        self._clear_selection()
        self.detail_panel.clear()
        self._dim_status_filter_chips()
        self._force_scroll_zero = True
        self._last_filter_sig = None
        self._sync_collection_header()
        self._apply_view_filter()

    def _sync_collection_header(self) -> None:
        content = self._is_collection_content_mode()
        if hasattr(self, "btn_collection_back"):
            self.btn_collection_back.setVisible(content)
        if not content:
            return
        from services import collection as coll

        rec = coll.get_collection(int(self._current_collection_id or 0))
        name = rec.name if rec is not None else "合集"
        self._page_title.setText(str(name or "合集"))

    def _exit_collection_mode(
        self, *, apply_filter: bool = True, restore_chips: bool = True
    ) -> None:
        if not self._is_collection_workspace() and not (
            hasattr(self, "btn_collection_mode") and self.btn_collection_mode.isChecked()
        ):
            return
        self._collection_mode = COLLECTION_MODE_NORMAL
        self._current_collection_id = None
        if hasattr(self, "btn_collection_mode") and self.btn_collection_mode.isChecked():
            self._suppress_collection_toggle = True
            try:
                self.btn_collection_mode.setChecked(False)
            finally:
                self._suppress_collection_toggle = False
        self._clear_selection()
        self.detail_panel.clear()
        if restore_chips:
            self._restore_status_filter_chips()
        self._force_scroll_zero = True
        self._clear_collection_card_cache()
        if hasattr(self, "btn_collection_back"):
            self.btn_collection_back.hide()
        self._refresh_game_header()
        if apply_filter:
            self._last_filter_sig = None
            self._apply_view_filter()

    def _wh3_order_fingerprint(self) -> tuple[str, ...]:
        if not getattr(self, "_wh3_sort_mode", False):
            return ()
        if self._is_stellaris_current_game():
            from services.stellaris_activation import resolved_load_order

            try:
                return tuple(resolved_load_order())
            except Exception:  # noqa: BLE001
                logger.debug("Stellaris load-order fingerprint failed", exc_info=True)
                return ()
        from services.wh3_activation import resolved_load_order

        try:
            return tuple(
                resolved_load_order(library_root=self._wh3_library_root())
            )
        except Exception:  # noqa: BLE001
            logger.debug("WH3 load-order fingerprint failed", exc_info=True)
            return ()

    def _apply_wh3_sort_order(self) -> None:
        if not getattr(self, "_wh3_sort_mode", False):
            return
        if self._is_stellaris_current_game():
            from services.stellaris_activation import (
                canon_internal_id,
                display_numbers,
                load_saved_order,
                persist_load_order,
            )

            order = persist_load_order(load_saved_order())
            rank = {mid: i for i, mid in enumerate(order)}
            self._filtered_row_entries.sort(
                key=lambda pair: rank.get(
                    canon_internal_id(getattr(pair[0], "mod_id", "") or ""),
                    10**9,
                )
            )
            visible = [
                canon_internal_id(getattr(pair[0], "mod_id", "") or "")
                for pair in self._filtered_row_entries
            ]
            self._wh3_display_numbers = display_numbers(visible)
            return
        if not self._is_wh3_current_game():
            return
        from services.wh3_activation import (
            canon_internal_id,
            display_numbers,
            load_saved_order,
            persist_load_order,
        )

        order = persist_load_order(
            load_saved_order(), library_root=self._wh3_library_root()
        )
        self._wh3_display_numbers = display_numbers(order)
        rank = {mid: i for i, mid in enumerate(order)}
        self._filtered_row_entries.sort(
            key=lambda pair: rank.get(
                canon_internal_id(getattr(pair[0], "mod_id", "") or ""),
                10**9,
            )
        )

    def _bind_wh3_sort_card(self, card, data) -> None:
        from services.wh3_activation import canon_internal_id

        sort_mode = bool(getattr(self, "_wh3_sort_mode", False)) and (
            self._is_wh3_current_game() or self._is_stellaris_current_game()
        )
        number = 0
        if sort_mode:
            mid = canon_internal_id(getattr(data, "id", "") or "")
            numbers = getattr(self, "_wh3_display_numbers", {}) or {}
            number = int(numbers.get(mid, 0) or 0)
        card.set_wh3_sort_mode(sort_mode, number=number)

    def _on_wh3_sort_drop(self, source_id: str, target_id: str) -> None:
        if not getattr(self, "_wh3_sort_mode", False):
            return
        if self._is_stellaris_current_game():
            from services.stellaris_activation import apply_card_drop, sync_stellaris_launcher

            apply_card_drop(source_id, target_id)
            try:
                sync_stellaris_launcher()
            except Exception:  # noqa: BLE001
                logger.debug("Stellaris launcher sync after sort failed", exc_info=True)
            self._last_filter_sig = None
            self._apply_view_filter()
            return
        from services.wh3_activation import apply_card_drop, sync_used_mods_txt

        apply_card_drop(
            source_id,
            target_id,
            library_root=self._wh3_library_root(),
        )
        try:
            sync_used_mods_txt(library_root=self._wh3_library_root())
        except Exception:  # noqa: BLE001
            logger.debug("WH3 used_mods sync after sort failed", exc_info=True)
        self._last_filter_sig = None
        self._apply_view_filter()

    def _set_library_status_filter(
        self,
        key: str,
        *,
        record_id: int | None = None,
        record_name: str | None = None,
    ) -> None:
        """
        Single status-filter setter (chips ↔ deployment record are mutually exclusive).
        """
        key = str(key or FILTER_ALL).strip() or FILTER_ALL
        if self._is_collection_workspace():
            self._exit_collection_mode(apply_filter=False, restore_chips=False)
        if key == FILTER_DEPLOYMENT_RECORD:
            if getattr(self, "_wh3_sort_mode", False):
                self._exit_wh3_sort_mode(apply_filter=False)
            rid = int(record_id) if record_id is not None else None
            if rid is None:
                return self._set_library_status_filter(FILTER_ALL)
            self._status_filter = FILTER_DEPLOYMENT_RECORD
            self._deployment_record_id = rid
            self._deployment_record_name = (record_name or "").strip() or None
            self._cached_record_mod_ids = None
            # Uncheck status chips only (not platform chips sharing _filter_buttons).
            self._suppress_filter_toggle = True
            try:
                self._filter_group.setExclusive(False)
                for sk, _label in STATUS_FILTER_LABELS:
                    btn = self._filter_buttons.get(sk)
                    if btn is not None:
                        btn.setChecked(False)
            finally:
                self._suppress_filter_toggle = False
        else:
            self._status_filter = key
            self._deployment_record_id = None
            self._deployment_record_name = None
            self._cached_record_mod_ids = None
            if not self._filter_group.exclusive():
                self._filter_group.setExclusive(True)
            btn = self._filter_buttons.get(key)
            if btn is not None and not btn.isChecked():
                self._suppress_filter_toggle = True
                try:
                    btn.setChecked(True)
                finally:
                    self._suppress_filter_toggle = False
        self._update_deployment_record_button_label()
        self._last_filter_sig = None
        self._apply_view_filter()

    def _current_library_deployed_mod_ids(self) -> list[str]:
        """
        Deployed mod ids from the full Library entity layer for this game.

        Must use ``_game_row_entries`` (projection), never ``_card_entries`` /
        viewport window / filtered rows — those are incomplete under virtual
        scroll, search, or status filters.
        """
        from ui.library_query import normalize_record_mod_id

        out: list[str] = []
        seen: set[str] = set()
        for index, _payload in self._game_row_entries:
            if not index.deployed:
                continue
            mid = normalize_record_mod_id(index.mod_id)
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    def _snapshot_mod_ids_for_deployment_record(self) -> list[str] | None:
        """
        Snapshot deployed entity ids for save/update Deployment Record.

        Prefer full ``_game_row_entries`` projection. ``None`` → service uses
        DB ``list_deployed_mod_ids_for_library_game``. Never derive mutations
        from the viewport card pool.
        """
        viewport_count = len(self._card_entries)
        if self._game_row_entries:
            ids = self._current_library_deployed_mod_ids()
            logger.info(
                "[DEPLOYMENT_RECORD_SNAPSHOT] source=game_row_entries "
                "total_candidates=%s deployed_count=%s viewport_count=%s",
                len(self._game_row_entries),
                len(ids),
                viewport_count,
            )
            return ids
        logger.info(
            "[DEPLOYMENT_RECORD_SNAPSHOT] source=database_fallback "
            "total_candidates=0 deployed_count=0 viewport_count=%s",
            viewport_count,
        )
        return None

    def _on_save_deployment_record(self) -> None:
        """Popup: save current deployed set; confirm before same-name overwrite."""
        from services import deployment_record as dr

        gid = int(self.current_game_id or 0)
        if gid <= 0:
            QMessageBox.information(
                self, "部署记录", "请先选择一个具体游戏再保存部署记录。"
            )
            return
        name, ok = QInputDialog.getText(
            self,
            "保存当前环境",
            "记录名称：",
            text=self._deployment_record_name or "",
        )
        if not ok:
            return
        label = str(name or "").strip()
        if not label:
            QMessageBox.warning(self, "保存部署记录", "名称不能为空。")
            return
        existing = dr.find_record_by_name(gid, label)
        if existing is not None and not self._confirm_overwrite_deployment_record(label):
            return
        try:
            record = dr.create_or_update_record(
                gid,
                label,
                mod_ids=self._snapshot_mod_ids_for_deployment_record(),
                game_folder=self._current_game_filter,
                library_root=self._target_root,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存部署记录失败", str(exc))
            return
        QMessageBox.information(
            self,
            "部署记录",
            f"已保存「{record.name}」（{len(dr.get_record_mod_ids(record.id))} 个 Mod）。",
        )
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and self._deployment_record_id is not None
            and int(self._deployment_record_id) == int(record.id)
        ):
            self._cached_record_mod_ids = None
            self._last_filter_sig = None
            self._apply_view_filter()

    def _confirm_overwrite_deployment_record(self, label: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("覆盖记录")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"记录「{label}」已存在")
        box.setInformativeText("当前操作会覆盖原记录 Mod 集合。\n是否继续？")
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        overwrite = box.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is overwrite

    def _pick_deployment_record_name(self, title: str) -> str | None:
        """First-level manage action: pick a record by display name (never show id)."""
        from services import deployment_record as dr

        gid = int(self.current_game_id or 0)
        if gid <= 0:
            QMessageBox.information(self, "部署记录", "请先选择一个具体游戏。")
            return None
        try:
            records = dr.list_records(gid)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, title, str(exc))
            return None
        if not records:
            QMessageBox.information(self, title, "暂无记录。")
            return None
        names = [rec.name for rec in records]
        current = self._deployment_record_name or ""
        start = names.index(current) if current in names else 0
        chosen, ok = QInputDialog.getItem(
            self, title, "选择记录：", names, start, False
        )
        if not ok:
            return None
        label = str(chosen or "").strip()
        return label or None

    def _rebuild_deployment_record_menu(self) -> None:
        """Single Popup: filter peers + first-level manage actions."""
        from services import deployment_record as dr

        menu = self._deployment_record_menu
        menu.clear()

        hdr = menu.addAction("筛选记录")
        hdr.setEnabled(False)

        act_all = menu.addAction("全部")
        act_all.setCheckable(True)
        act_all.setChecked(self._status_filter == FILTER_ALL)
        act_all.triggered.connect(
            lambda: self._set_library_status_filter(FILTER_ALL)
        )

        gid = int(self.current_game_id or 0)
        records = []
        if gid <= 0:
            empty = menu.addAction("（请先选择游戏）")
            empty.setEnabled(False)
        else:
            try:
                records = dr.list_records(gid)
            except Exception:  # noqa: BLE001
                logger.debug("list_records failed", exc_info=True)
                records = []
            if not records:
                empty = menu.addAction("（暂无记录）")
                empty.setEnabled(False)
            else:
                active_id = self._deployment_record_id
                for rec in records:
                    action = menu.addAction(rec.name)
                    action.setCheckable(True)
                    action.setChecked(
                        self._status_filter == FILTER_DEPLOYMENT_RECORD
                        and active_id is not None
                        and int(rec.id) == int(active_id)
                    )
                    action.triggered.connect(
                        lambda _c=False, r=rec: self._set_library_status_filter(
                            FILTER_DEPLOYMENT_RECORD,
                            record_id=int(r.id),
                            record_name=r.name,
                        )
                    )

        menu.addSeparator()
        hdr_m = menu.addAction("记录管理")
        hdr_m.setEnabled(False)
        menu.addAction("保存当前环境...").triggered.connect(
            self._on_save_deployment_record
        )
        menu.addAction("更新记录...").triggered.connect(
            self._on_update_deployment_record_clicked
        )
        menu.addAction("重命名记录...").triggered.connect(
            self._on_rename_deployment_record_clicked
        )
        menu.addAction("删除记录...").triggered.connect(
            self._on_delete_deployment_record_clicked
        )

    def _on_rename_deployment_record_clicked(self) -> None:
        picked = self._pick_deployment_record_name("重命名记录")
        if not picked:
            return
        from services import deployment_record as dr

        gid = int(self.current_game_id or 0)
        rec = dr.find_record_by_name(gid, picked)
        if rec is None:
            return
        self._on_rename_deployment_record(int(rec.id), rec.name)

    def _on_update_deployment_record_clicked(self) -> None:
        picked = self._pick_deployment_record_name("更新记录")
        if not picked:
            return
        self._on_update_deployment_record(picked)

    def _on_delete_deployment_record_clicked(self) -> None:
        picked = self._pick_deployment_record_name("删除记录")
        if not picked:
            return
        from services import deployment_record as dr

        gid = int(self.current_game_id or 0)
        rec = dr.find_record_by_name(gid, picked)
        if rec is None:
            return
        self._on_delete_deployment_record(int(rec.id), rec.name)

    def _on_rename_deployment_record(
        self, record_id: int, current_name: str
    ) -> None:
        from services import deployment_record as dr

        name, ok = QInputDialog.getText(
            self, "重命名部署记录", "新名称：", text=current_name
        )
        if not ok:
            return
        label = str(name or "").strip()
        if not label:
            return
        gid = int(self.current_game_id or 0)
        if gid > 0 and label.casefold() != current_name.casefold():
            clash = dr.find_record_by_name(gid, label)
            if clash is not None and int(clash.id) != int(record_id):
                QMessageBox.warning(
                    self, "重命名失败", f"记录「{label}」已存在。"
                )
                return
        try:
            record = dr.rename_record(record_id, label)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重命名失败", str(exc))
            return
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and self._deployment_record_id is not None
            and int(self._deployment_record_id) == int(record_id)
        ):
            self._deployment_record_name = record.name
            self._update_deployment_record_button_label()

    def _on_update_deployment_record(self, record_name: str) -> None:
        from services import deployment_record as dr

        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return
        confirm = QMessageBox.question(
            self,
            "更新记录",
            f"使用当前已部署 Mod 更新该记录「{record_name}」？",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            record = dr.create_or_update_record(
                gid,
                record_name,
                mod_ids=self._snapshot_mod_ids_for_deployment_record(),
                game_folder=self._current_game_filter,
                library_root=self._target_root,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新失败", str(exc))
            return
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and self._deployment_record_id is not None
            and int(self._deployment_record_id) == int(record.id)
        ):
            self._cached_record_mod_ids = None
            self._last_filter_sig = None
            self._apply_view_filter()
        QMessageBox.information(self, "部署记录", f"已更新「{record.name}」。")

    def _on_delete_deployment_record(
        self, record_id: int, record_name: str
    ) -> None:
        from services import deployment_record as dr

        confirm = QMessageBox.question(
            self,
            "删除部署记录",
            f"删除记录「{record_name}」？\n不会删除 Mod，也不会影响当前部署。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            dr.delete_record(record_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        if (
            self._status_filter == FILTER_DEPLOYMENT_RECORD
            and self._deployment_record_id is not None
            and int(self._deployment_record_id) == int(record_id)
        ):
            self._set_library_status_filter(FILTER_ALL)

    def _on_status_filter_toggled(self, key: str, checked: bool) -> None:
        if getattr(self, "_suppress_filter_toggle", False):
            return
        if not checked:
            return
        # Selecting a chip leaves Collection Mode and any WH3 sort workspace.
        if self._is_collection_workspace():
            self._exit_collection_mode(apply_filter=False, restore_chips=False)
        if getattr(self, "_wh3_sort_mode", False):
            self._exit_wh3_sort_mode(apply_filter=False)
        self._set_library_status_filter(key)

    def _on_category_changed(self, _index: int = 0) -> None:
        data = self.category_combo.currentData()
        if data in (None, "", FILTER_CATEGORY_ALL, FILTER_ALL):
            self._category_filter = FILTER_CATEGORY_ALL
        else:
            self._category_filter = str(data)
        self._sync_type_manage_buttons()
        self._apply_view_filter()

    def _reload_type_catalog(self, *, reconcile: bool = False) -> None:
        """Refresh in-memory type labels for the Library UI.

        Refresh / render must not run orphan reconcile or legacy migrate reports
        on the GUI thread (those are startup / type-CRUD concerns).
        """
        from services.mod_type_catalog import (
            ModTypeCatalogError,
            get_mod_type_catalog,
        )

        catalog = get_mod_type_catalog()
        try:
            catalog.reload(db=None if not reconcile else get_db(), reconcile=reconcile)
        except ModTypeCatalogError:
            logger.warning("mod type catalog reload refused", exc_info=True)

    def _current_game_type_catalog(self) -> list[tuple[int, str]]:
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return []
        try:
            from services.mod_type_catalog import get_mod_type_catalog

            return [
                (t.type_id, t.name)
                for t in get_mod_type_catalog().list_types(gid)
            ]
        except Exception:  # noqa: BLE001
            logger.debug("list type catalog failed", exc_info=True)
            return []

    def _merged_category_options(self, used: list[str] | None = None) -> list[tuple[int, str]]:
        del used  # Filter identity is Type ID; unused names must not become ghost filters.
        return list(self._current_game_type_catalog())

    def _sync_type_manage_buttons(self) -> None:
        if not hasattr(self, "btn_add_game_type"):
            return
        has_game = bool(int(self.current_game_id or 0) > 0)
        self.btn_add_game_type.setEnabled(has_game)
        selected = str(self.category_combo.currentData() or "")
        catalog_ids = {str(tid) for tid, _name in self._current_game_type_catalog()}
        can_delete = (
            has_game
            and selected not in ("", FILTER_CATEGORY_ALL, FILTER_ALL)
            and selected in catalog_ids
        )
        self.btn_delete_game_type.setEnabled(can_delete)

    def _on_add_game_type(self) -> None:
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return
        name, ok = QInputDialog.getText(self, "新增类型", "类型名称：")
        if not ok:
            return
        label = str(name or "").strip()
        if not label:
            return
        from services.mod_type_catalog import (
            ModTypeCatalogError,
            get_mod_type_catalog,
        )

        try:
            created = get_mod_type_catalog().add_type(gid, label)
        except ModTypeCatalogError as exc:
            msg = str(exc)
            if "已存在" in msg:
                QMessageBox.information(self, "新增类型", msg)
            else:
                QMessageBox.warning(self, "新增类型失败", msg)
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "新增类型失败", str(exc))
            return
        self._category_filter = str(created.type_id)
        self._refresh_category_combo()
        self._apply_category_options_to_cards()

    def _on_delete_game_type(self) -> None:
        gid = int(self.current_game_id or 0)
        raw = self.category_combo.currentData()
        label = str(self.category_combo.currentText() or "").strip()
        if gid <= 0 or raw in (None, "", FILTER_CATEGORY_ALL, FILTER_ALL):
            return
        try:
            type_id = int(raw)
        except (TypeError, ValueError):
            return
        confirm = QMessageBox.question(
            self,
            "删除类型",
            f"删除类型「{label}」？\n"
            "所有绑定该类型的 Mod 将变为未分类，不会改绑到其它类型。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        from services.mod_type_catalog import (
            ModTypeCatalogError,
            get_mod_type_catalog,
        )

        try:
            get_mod_type_catalog().delete_type(gid, type_id, get_db())
        except ModTypeCatalogError as exc:
            QMessageBox.warning(self, "删除类型失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除类型失败", str(exc))
            return
        if str(self._category_filter) == str(type_id):
            self._category_filter = FILTER_CATEGORY_ALL
        self._refresh_category_combo()
        self._apply_category_options_to_cards()
        self._apply_view_filter()

    def _apply_category_options_to_cards(self) -> None:
        options = self._merged_category_options()
        self._viewport_category_options = options
        for card in self._cards:
            card.set_category_options(options)

    def _refresh_category_combo(self, available: list[str] | None = None) -> None:
        del available
        types = self._merged_category_options()
        id_keys = [str(tid) for tid, _name in types]
        current = coerce_filter_selection(
            str(self._category_filter or ""), id_keys, all_key=FILTER_CATEGORY_ALL
        )
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("全部分类", FILTER_CATEGORY_ALL)
        for tid, name in types:
            self.category_combo.addItem(name, str(tid))
        idx = 0
        for i in range(self.category_combo.count()):
            if str(self.category_combo.itemData(i) or "") == current:
                idx = i
                break
        self.category_combo.setCurrentIndex(idx)
        self.category_combo.blockSignals(False)
        data = self.category_combo.currentData()
        if data in (None, "", FILTER_CATEGORY_ALL, FILTER_ALL):
            self._category_filter = FILTER_CATEGORY_ALL
        else:
            self._category_filter = str(data)
        self._sync_type_manage_buttons()

    def _on_sort_changed(self, _index: int) -> None:
        self._sort_mode = str(self.sort_combo.currentData() or SORT_MTIME)
        self._apply_view_filter()

    def _apply_view_filter(self) -> None:
        """Reorder / show-hide cards only — never recreates DetailPanel."""
        detail_id = id(self.detail_panel)
        query = self.search_box.text()
        category_key = (
            self._sidebar_category
            if self._sidebar_category
            else self._category_filter
        )
        # Under deployment-record filter, deployed flips must refresh overlays/visibility.
        recorded_ids = self._resolve_record_mod_ids()
        deployed_fp = None
        if self._status_filter == FILTER_DEPLOYMENT_RECORD:
            deployed_fp = tuple(
                sorted(
                    str(index.mod_id)
                    for index, _payload in self._game_row_entries
                    if index.deployed
                )
            )
        sort_member_fp = None
        if getattr(self, "_wh3_sort_mode", False) and self._is_load_order_sort_game():
            sort_member_fp = tuple(
                sorted(
                    str(index.mod_id)
                    for index, _payload in self._game_row_entries
                    if bool(getattr(index, "deployed", False))
                )
            )
        if self._is_collection_list_mode():
            self._prepare_collection_list_entries()
        filter_sig = (
            query,
            self._status_filter,
            FILTER_PLATFORM_ALL,
            category_key,
            self._sort_mode,
            self._current_game_filter or "",
            len(self._game_row_entries),
            self._deployment_record_id,
            None if recorded_ids is None else tuple(sorted(recorded_ids)),
            deployed_fp,
            bool(getattr(self, "_wh3_sort_mode", False)),
            tuple(self._wh3_order_fingerprint()),
            sort_member_fp,
            str(getattr(self, "_collection_mode", COLLECTION_MODE_NORMAL)),
            int(getattr(self, "_current_collection_id", 0) or 0),
            self._collection_list_fingerprint(),
            self._collection_content_fingerprint(),
        )
        if filter_sig == getattr(self, "_last_filter_sig", None):
            # No viewport rebuild — still refresh overlays (deploy flips / stale).
            self._sync_record_overlays()
            return
        self._last_filter_sig = filter_sig
        # Overlay sync must run AFTER viewport rebind (rebind clears overlays).
        # Re-parent/show churn collapses scroll range → jumps to bottom without this.
        # Clamp that captured offset onto the *new* filtered count — never feed a
        # leftover scroll_y from a longer list into compute_viewport_window.
        stale_scroll = (
            0 if getattr(self, "_force_scroll_zero", False) else self._capture_scroll()
        )
        self._force_scroll_zero = False
        self.scroll.setUpdatesEnabled(False)
        self.library_host.setUpdatesEnabled(False)
        restore_scroll = 0
        try:
            self.library_layout.setEnabled(False)
            self._clear_flow_except_overlay()

            if self._is_collection_list_mode():
                selected_mid = str(self._selected_mod_id or "").strip()
                if selected_mid or self._selected_mod_ids:
                    self._clear_selection()
                    self.detail_panel.clear()
                self.empty_overlay.hide()
                self._empty_kind = None
                n_vis = self._viewport_item_count()
                n_collections = max(0, n_vis - 1)
                self.count_label.setText(f"{n_collections} 合集")
                restore_scroll = self._legalize_viewport_scroll(stale_scroll, n_vis)
                self._sync_viewport_cards(scroll_y=restore_scroll)
                self._sync_collection_header()
                self._refresh_game_header()
                assert id(self.detail_panel) == detail_id
            elif self._is_collection_content_mode():
                member_ids = self._prepare_collection_content_entries(
                    query=query,
                    category_key=category_key,
                )
                selected_mid = str(self._selected_mod_id or "").strip()
                if not selected_mid and self._selected_mod_ids:
                    selected_mid = str(self._selected_mod_ids[-1] or "").strip()
                if selected_mid:
                    still = any(
                        str(getattr(index, "mod_id", "") or "") == selected_mid
                        for index, _payload in self._filtered_row_entries
                    )
                    if not still:
                        self._clear_selection()
                        self.detail_panel.clear()
                n_vis = len(self._filtered_row_entries)
                self._sync_collection_header()
                if not self._filtered_row_entries:
                    if member_ids:
                        self._show_empty(
                            EMPTY_COLLECTION,
                            title="此合集中没有匹配的 Mod",
                            hint="试试清空搜索，或返回合集列表。",
                            action="",
                        )
                    else:
                        self._show_empty(
                            EMPTY_COLLECTION,
                            title="此合集暂无 Mod",
                            hint="在普通 Library 中右键 Mod → 设置合集。",
                            action="",
                        )
                    self.count_label.setText("0 Mods")
                    self._cards = []
                    self._card_entries = []
                    restore_scroll = self._legalize_viewport_scroll(0, 0)
                    self._sync_library_host_size()
                    assert id(self.detail_panel) == detail_id
                else:
                    self.empty_overlay.hide()
                    self._empty_kind = None
                    self.count_label.setText(f"{n_vis} Mods")
                    restore_scroll = self._legalize_viewport_scroll(stale_scroll, n_vis)
                    self._sync_viewport_cards(scroll_y=restore_scroll)
                    assert id(self.detail_panel) == detail_id
            elif getattr(self, "_wh3_sort_mode", False) and self._is_stellaris_current_game():
                self._filtered_row_entries = [
                    (index, payload)
                    for index, payload in self._game_row_entries
                    if bool(getattr(index, "deployed", False))
                ]
                if str(query or "").strip():
                    self._filtered_row_entries = [
                        pair
                        for pair in self._filtered_row_entries
                        if matches_search(pair[0], query)
                    ]
                self._apply_wh3_sort_order()
            elif getattr(self, "_wh3_sort_mode", False) and self._is_wh3_current_game():
                self._filtered_row_entries = [
                    (index, payload)
                    for index, payload in self._game_row_entries
                    if bool(getattr(index, "deployed", False))
                ]
                self._apply_wh3_sort_order()
            else:
                self._filtered_row_entries = filter_sort_entries(
                    self._game_row_entries,
                    query=query,
                    filter_key=self._status_filter,
                    platform_key=FILTER_PLATFORM_ALL,
                    category_key=category_key,
                    sort_mode=self._sort_mode,
                    record_mod_ids=recorded_ids,
                )

            if not self._is_collection_workspace():
                selected_mid = str(self._selected_mod_id or "").strip()
                if not selected_mid and self._selected_mod_ids:
                    selected_mid = str(self._selected_mod_ids[-1] or "").strip()
                if selected_mid:
                    still = any(
                        str(getattr(index, "mod_id", "") or "") == selected_mid
                        for index, _payload in self._filtered_row_entries
                    )
                    if not still:
                        self._clear_selection()
                        self.detail_panel.clear()
                elif self._selected_card is not None:
                    selected_mid = self._selected_card._mod_id()
                    still = any(
                        str(getattr(index, "mod_id", "") or "") == selected_mid
                        for index, _payload in self._filtered_row_entries
                    )
                    if not still:
                        self._clear_selection()
                        self.detail_panel.clear()

                if not self._game_row_entries:
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
                    self._clear_all_record_overlays()
                    self._cards = []
                    self._card_entries = []
                    restore_scroll = self._legalize_viewport_scroll(0, 0)
                    self._sync_library_host_size()
                    assert id(self.detail_panel) == detail_id
                elif not self._filtered_row_entries:
                    self._show_empty(
                        EMPTY_SEARCH,
                        title="No matching mods",
                        hint="Try clearing the search box or resetting status / platform filters.",
                        action="Clear filters",
                    )
                    self.count_label.setText("0 Mods")
                    self._clear_all_record_overlays()
                    self._cards = []
                    self._card_entries = []
                    restore_scroll = self._legalize_viewport_scroll(0, 0)
                    self._sync_library_host_size()
                    assert id(self.detail_panel) == detail_id
                else:
                    self.empty_overlay.hide()
                    self._empty_kind = None
                    n_vis = len(self._filtered_row_entries)
                    self.count_label.setText(f"{n_vis} Mods")
                    status_line = str(getattr(self, "_pending_game_status_line", "") or "").strip()
                    if self.current_game_name and status_line:
                        self.count_label.setText(f"{n_vis} Mods  ·  {status_line}")
                    self._refresh_game_header()
                    restore_scroll = self._legalize_viewport_scroll(stale_scroll, n_vis)
                    # Viewport rebind clears card overlays; restore inside _sync_viewport_cards.
                    self._sync_viewport_cards(scroll_y=restore_scroll)
                    assert id(self.detail_panel) == detail_id
        finally:
            self.library_layout.setEnabled(True)
            self.library_host.setUpdatesEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            self._set_scroll_value(restore_scroll)
        self._schedule_viewport_scroll_clamp()
        if not self._is_collection_list_mode():
            self._schedule_visible_covers()

    def _reveal_card(self, card: ModCardWidget) -> None:
        """Put ``card`` into the card FlowLayout, then show — never parentless show()."""
        # addWidget reparents onto cards_host; do this BEFORE show().
        self.library_layout.addWidget(card)
        if card.parent() is None:
            logger.warning(
                "[UI BUG] ModCardWidget has no parent before show path=%s",
                card.managed_path,
            )
            return
        card.show()

    def _on_library_scroll_value(self, *_args) -> None:
        """Debounce scroll → viewport rebind (avoid per-pixel main-thread storms)."""
        timer = getattr(self, "_scroll_sync_timer", None)
        if timer is None:
            self._on_library_scroll_covers()
            return
        timer.start()

    def _on_library_scroll_covers(self, *_args) -> None:
        from services.ui_block_trace import ui_block_phase

        with ui_block_phase("viewport_scroll_sync"):
            self._sync_viewport_cards()
        if not self._is_collection_list_mode():
            self._schedule_visible_covers()

    def _schedule_visible_covers(self) -> None:
        if self._is_collection_list_mode():
            return
        timer = getattr(self, "_cover_sched", None)
        if timer is None:
            self._load_viewport_covers()
            return
        timer.start()

    def iter_viewport_cover_cards(self, *, preload_px: int = 200) -> list:
        """Cards intersecting the scroll viewport (plus a preload band)."""
        shown = [
            card
            for card in self._cards
            if isinstance(card, ModCardWidget) and not card.isHidden()
        ]
        vp = self.scroll.viewport()
        if vp is None or int(vp.height()) <= 1 or int(vp.width()) <= 1:
            cap = LIBRARY_CARDS_PER_ROW * 3
            return shown[:cap]
        band = vp.rect().adjusted(0, -int(preload_px), 0, int(preload_px))
        hit: list[ModCardWidget] = []
        for card in shown:
            top_left = card.mapTo(vp, QPoint(0, 0))
            crect = QRect(top_left, card.size())
            if band.intersects(crect):
                hit.append(card)
        return hit

    def _load_viewport_covers(self) -> None:
        from services.ui_block_trace import log_ui_block

        t0 = time.perf_counter()
        visible = self.iter_viewport_cover_cards()
        visible_set = set(visible)
        submitted = 0
        # Only touch the current viewport pool (``self._cards``), never the
        # whole ``_card_cache``.
        for card in self._cards:
            if not isinstance(card, ModCardWidget) or card.isHidden():
                continue
            if card in visible_set:
                if card.ensure_cover():
                    submitted += 1
            else:
                card.cancel_pending_cover(keep_pixmap=True)
        elapsed = (time.perf_counter() - t0) * 1000.0
        try:
            from services.library_perf_metrics import get_library_perf_metrics

            get_library_perf_metrics().note(
                "cover_schedule_ms",
                round(elapsed, 2),
            )
            get_library_perf_metrics().note("cover_visible", len(visible))
            get_library_perf_metrics().note("cover_submitted", submitted)
        except Exception:  # noqa: BLE001
            pass
        try:
            log_ui_block(
                "cover_viewport_schedule",
                elapsed,
                visible=len(visible),
                submitted=submitted,
                cache=len(self._card_cache),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from services.startup_io_trace import log_io_event

            log_io_event(
                "cover_loader",
                "visible_schedule",
                cards=len(visible),
                submitted=submitted,
            )
        except Exception:  # noqa: BLE001
            pass

    def _cancel_all_pending_covers(self) -> None:
        for card in list(getattr(self, "_card_cache", {}).values()):
            if isinstance(card, ModCardWidget):
                card.cancel_pending_cover(keep_pixmap=True)

    # ------------------------------------------------------------------
    # Selection (mod_id authority — viewport cards are display only)
    # ------------------------------------------------------------------

    def _filtered_mod_ids(self) -> list[str]:
        """Ordered internal ids for the current filter/sort list."""
        out: list[str] = []
        for index, data in self._filtered_row_entries:
            mid = str(
                getattr(index, "mod_id", "") or getattr(data, "id", "") or ""
            ).strip()
            if mid:
                out.append(mid)
        return out

    def _filtered_index_of_mod(self, mod_id: str) -> int:
        mid = str(mod_id or "").strip()
        if not mid:
            return -1
        for i, (index, data) in enumerate(self._filtered_row_entries):
            row_id = str(
                getattr(index, "mod_id", "") or getattr(data, "id", "") or ""
            ).strip()
            if row_id == mid:
                return i
        return -1

    def _filtered_row_for_mod(
        self, mod_id: str
    ) -> tuple[ModFilterIndex, object] | None:
        mid = str(mod_id or "").strip()
        if not mid:
            return None
        for index, data in self._filtered_row_entries:
            row_id = str(
                getattr(index, "mod_id", "") or getattr(data, "id", "") or ""
            ).strip()
            if row_id == mid:
                return index, data
        return None

    def _rematerialize_selection_from_ids(self) -> None:
        """Rebuild viewport card mirrors + styles from ``_selected_mod_ids``."""
        selected = {str(m).strip() for m in self._selected_mod_ids if str(m).strip()}
        cards: list[ModCardWidget] = []
        for card in self._cards:
            mid = str(card._mod_id() or "").strip()
            if mid and mid in selected:
                cards.append(card)
                card.add_selected_style()
            else:
                card.remove_selected_style()
        self._selected_cards = cards
        anchor_mid = str(self._selection_anchor_mod_id or "").strip()
        self._selection_anchor = None
        if anchor_mid:
            for card in cards:
                if card._mod_id() == anchor_mid:
                    self._selection_anchor = card
                    break
            if self._selection_anchor is None:
                for card in self._cards:
                    if card._mod_id() == anchor_mid:
                        self._selection_anchor = card
                        break
        focus = str(self._selected_mod_id or "").strip()
        if not focus and self._selected_mod_ids:
            focus = self._selected_mod_ids[-1]
        self._selected_mod_id = focus
        self._selected_card = None
        self._selected_path = None
        if focus:
            for card in cards:
                if card._mod_id() == focus:
                    self._selected_card = card
                    self._selected_path = card.managed_path
                    break
            if self._selected_card is None:
                row = self._filtered_row_for_mod(focus)
                if row is not None:
                    _index, data = row
                    path = Path(str(getattr(data, "managed_path", "") or ""))
                    self._selected_path = path if str(path) else None

    def _apply_shift_range_selection(self, current_mod_id: str) -> None:
        """Select the full filtered-list range from anchor mod_id → current."""
        ids = self._filtered_mod_ids()
        if not ids:
            return
        cur = str(current_mod_id or "").strip()
        if not cur or cur not in ids:
            return
        anchor = str(self._selection_anchor_mod_id or "").strip()
        if not anchor or anchor not in ids:
            anchor = cur
            self._selection_anchor_mod_id = cur
        start = ids.index(anchor)
        end = ids.index(cur)
        lo, hi = (start, end) if start <= end else (end, start)
        self._selected_mod_ids = ids[lo : hi + 1]
        self._selected_mod_id = cur
        self._last_clicked_index = end
        self._rematerialize_selection_from_ids()
        self._apply_multi_or_single_panel()

    def on_mod_selected(self, mod_id: object) -> None:
        from PySide6.QtWidgets import QToolTip

        try:
            from ui.popup_trace import log_popup

            log_popup("slot:on_mod_selected", detail=str(mod_id))
        except Exception:  # noqa: BLE001
            pass
        # Kill floating tip / toast that Qt pops under the cursor on click.
        QToolTip.hideText()
        mid = str(mod_id or "").strip()
        if not mid:
            return
        modifiers = QApplication.keyboardModifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if shift:
            # Range uses filtered rows — current card need not share viewport with anchor.
            self._apply_shift_range_selection(mid)
            QToolTip.hideText()
            return
        card = self._card_for_mod_id(mid)
        if card is None or card.isHidden():
            return
        visible = self._visible_cards()
        try:
            current_index = visible.index(card)
        except ValueError:
            current_index = self._filtered_index_of_mod(mid)
        if ctrl:
            self._toggle_card_selection(card)
            self._last_clicked_index = current_index
        else:
            self._select_card(card, show_panel=True)
            self._last_clicked_index = current_index
        QToolTip.hideText()

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
        """Select every filtered mod (Ctrl+A) — full list, not viewport window."""
        ids = self._filtered_mod_ids()
        if not ids:
            return
        self._selected_mod_ids = list(ids)
        self._selection_anchor_mod_id = ids[0]
        self._selected_mod_id = ids[-1]
        self._last_clicked_index = 0
        self._rematerialize_selection_from_ids()
        self._apply_multi_or_single_panel()

    def _sync_selection_styles(self) -> None:
        self._rematerialize_selection_from_ids()

    @traced("ModLibraryView._apply_multi_or_single_panel")
    def _apply_multi_or_single_panel(self) -> None:
        ids = [str(m).strip() for m in self._selected_mod_ids if str(m).strip()]
        if not ids:
            self.detail_panel.clear()
            return
        if len(ids) == 1:
            mid = ids[0]
            self._selected_mod_id = mid
            card = self._card_for_mod_id(mid)
            path: Path | None = None
            if card is not None and not card.isHidden():
                self._selected_card = card
                path = card.managed_path
            else:
                self._selected_card = None
                row = self._filtered_row_for_mod(mid)
                if row is not None:
                    path = Path(str(getattr(row[1], "managed_path", "") or ""))
            self._selected_path = path
            if path is None:
                self.detail_panel.clear()
                return
            self._sync_peer_mods_to_panel(exclude=mid)
            self.detail_panel.show_mod(
                path,
                mod_id=mid,
                game_id=int(self.current_game_id or 0),
                game_name=str(self.current_game_name or "").strip(),
            )
            return
        # Multi-select: detail shows batch edit + optional offline save.
        focus = ids[-1]
        self._selected_mod_id = focus
        focus_card = self._card_for_mod_id(focus)
        if focus_card is not None and not focus_card.isHidden():
            self._selected_card = focus_card
            self._selected_path = focus_card.managed_path
        else:
            self._selected_card = None
            row = self._filtered_row_for_mod(focus)
            self._selected_path = (
                Path(str(getattr(row[1], "managed_path", "") or ""))
                if row is not None
                else None
            )
        plat = "steam"
        entries: list[tuple[str, object, str]] = []
        for mid in ids:
            row = self._filtered_row_for_mod(mid)
            if row is None:
                continue
            index, data = row
            card_plat = getattr(index, "platform", "steam") or "steam"
            path = Path(str(getattr(data, "managed_path", "") or ""))
            card = self._card_for_mod_id(mid)
            if card is not None:
                path = card.managed_path
            if mid == focus:
                plat = card_plat
            entries.append((mid, path, card_plat))
        self.detail_panel.show_batch_selection(
            ids,
            game_name=str(self.current_game_name or "").strip(),
            game_id=int(self.current_game_id or 0),
            platform=plat,
            entries=entries,
        )

    @traced("ModLibraryView._select_card")
    def _select_card(self, card: ModCardWidget, *, show_panel: bool) -> None:
        mid = str(card._mod_id() or "").strip()
        self._selected_mod_ids = [mid] if mid else []
        self._selection_anchor_mod_id = mid
        self._selected_mod_id = mid
        self._selected_card = card
        self._selected_path = card.managed_path
        self._selection_anchor = card
        self._rematerialize_selection_from_ids()
        if show_panel:
            self._apply_multi_or_single_panel()

    def _toggle_card_selection(self, card: ModCardWidget) -> None:
        mid = str(card._mod_id() or "").strip()
        if not mid:
            return
        if mid in self._selected_mod_ids:
            self._selected_mod_ids = [m for m in self._selected_mod_ids if m != mid]
            if self._selection_anchor_mod_id == mid:
                self._selection_anchor_mod_id = (
                    self._selected_mod_ids[-1] if self._selected_mod_ids else ""
                )
        else:
            self._selected_mod_ids.append(mid)
            self._selection_anchor_mod_id = mid
        self._selected_mod_id = (
            self._selected_mod_ids[-1] if self._selected_mod_ids else ""
        )
        self._rematerialize_selection_from_ids()
        if not self._selected_mod_ids:
            self._selected_card = None
            self._selected_path = None
            self._selection_anchor = None
            self.detail_panel.clear()
            return
        self._apply_multi_or_single_panel()

    def _prepare_card_context_menu(self, card: ModCardWidget) -> None:
        """Ensure right-click target is part of the current selection."""
        mid = str(card._mod_id() or "").strip()
        if mid and mid in self._selected_mod_ids:
            return
        self._select_card(card, show_panel=False)
        idx = self._filtered_index_of_mod(mid)
        if idx >= 0:
            self._last_clicked_index = idx

    def _on_batch_set_category(self, category: str) -> None:
        """Bind selected Mods to a Type ID (empty clears). Never stores a type name."""
        ids = [str(m).strip() for m in self._selected_mod_ids if str(m).strip()]
        if not ids:
            return
        raw = str(category or "").strip()
        type_id = None
        if raw:
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                return
            type_id = parsed if parsed > 0 else None
        db = get_db()
        from services.mod_projection_events import notify_mod_changed

        for mid in ids:
            try:
                db.set_mod_type_id(mid, type_id)
            except Exception:  # noqa: BLE001
                continue
            notify_mod_changed(mid)
        self._refresh_category_combo()
        self._rematerialize_selection_from_ids()
        self._apply_multi_or_single_panel()

    def _sync_peer_mods_to_panel(self, *, exclude: str = "") -> None:
        """Peer list = full game projection, not the viewport card window."""
        peers: list[dict] = []
        for index, _payload in self._game_row_entries:
            mid = str(getattr(index, "mod_id", "") or "").strip()
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
        for card in self._cards:
            card.remove_selected_style()
        self._selected_cards = []
        self._selected_mod_ids = []
        self._selection_anchor_mod_id = ""
        self._selection_anchor = None
        self._selected_card = None
        self._selected_path = None
        self._selected_mod_id = ""

    def _on_batch_platform_saved(self, mod_ids: object) -> None:
        """Projection rebind after batch source-only save — no full Library refresh."""
        ids = {str(m).strip() for m in (mod_ids or []) if str(m).strip()}
        try:
            from services.info_sidecar import write_sidecar_for_mod
            from services.path_lifecycle import resolve_managed_folder

            for mid in ids:
                resolved = resolve_managed_folder(
                    mid, library_root=self._target_root, db=None
                )
                folder = resolved.path
                if folder is not None and folder.is_dir():
                    write_sidecar_for_mod(folder, mid)
        except Exception:  # noqa: BLE001
            pass
        from services.mod_projection_events import notify_mod_changed

        for mid in ids:
            notify_mod_changed(mid)
        # notify rebind clears relative overlay; restore if Record Filter active.
        self._sync_record_overlays()
        # Keep multi-select panel state with updated platform label.
        if len(self._selected_mod_ids) > 1:
            self._apply_multi_or_single_panel()
        elif self._selected_card is not None:
            self.detail_panel.show_mod(
                self._selected_card.managed_path,
                mod_id=self._selected_card._mod_id() or None,
                game_id=int(self.current_game_id or 0),
                game_name=str(self.current_game_name or "").strip(),
            )

    def _card_for_path(self, path: Path) -> ModCardWidget | None:
        """Deprecated path lookup — prefer :meth:`_card_for_mod_id`.

        Kept only for transitional open-folder Path payloads; never used as
        Projection identity.
        """
        try:
            target = path.resolve()
        except OSError:
            target = path
        target_s = str(target)
        raw_s = str(path)
        for card in self._cards:
            try:
                if card.managed_path.resolve() == target:
                    return card
            except OSError:
                pass
            if str(card.managed_path) in {target_s, raw_s}:
                return card
        return None

    def _on_panel_metadata_saved(self, mod_path: object) -> None:
        """Patch projection for the affected Mod — do not rescan the library."""
        mid = str(
            getattr(self.detail_panel, "current_mod_id", lambda: "")() or ""
        ).strip()
        if not mid:
            mid = str(self._selected_mod_id or "").strip()
        if not mid:
            # Legacy Path payload: bind only via already-visible Projection cards.
            raw = str(mod_path or "").strip()
            if raw.isdigit():
                mid = raw
            else:
                card = self._card_for_path(Path(raw)) if raw else None
                mid = card._mod_id() if card is not None else ""
        if not mid:
            return
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)
        scroll = self._capture_scroll()
        self._restore_scroll_after_layout(scroll, focus_mod_id=mid)

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
        # Immediate UI feedback (do not wait for queued deploy_started).
        self.detail_panel.set_deploy_busy(True, action=action)
        worker.deploy_started.connect(
            lambda a=action: self._on_deploy_started(a),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.deploy_finished.connect(self._on_deploy_finished)
        worker.deploy_failed.connect(self._on_deploy_failed)
        worker.finished.connect(self._on_deploy_thread_finished)
        self._deploy_worker = worker
        self._deploy_ui_timed_out = False
        worker.start()
        self._deploy_watchdog.start()

    def _on_deploy_watchdog_timeout(self) -> None:
        """UI-side stall recovery — does not kill the worker thread."""
        worker = self._deploy_worker
        if worker is None or not worker.isRunning():
            return
        mid = self._deploy_mod_id or ""
        logger.warning(
            "[DEPLOY_STALL] mod_id=%s worker still running after UI watchdog",
            mid,
        )
        self.detail_panel.apply_deploy_failure(
            "部署超时：后台任务可能仍在运行。请查看日志中的 "
            "[DEPLOY_STAGE] / [DEPLOY_SLOW] 定位卡点。",
            status="TIMEOUT",
        )
        self.detail_panel.set_deploy_busy(False, action="deploy")
        self._deploy_ui_timed_out = True

    def _stop_deploy_watchdog(self) -> None:
        if self._deploy_watchdog.isActive():
            self._deploy_watchdog.stop()

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
        self._persist_wh3_activation_state()
        self.detail_panel.clear()
        self.refresh()

    def _apply_tag_deploy_hint(self, mod_id: str) -> None:
        hints: list[str] = []
        try:
            flags = get_db().get_mods_tag_flags([mod_id]).get(str(mod_id))
        except Exception:  # noqa: BLE001
            flags = None
        if flags is not None and flags.invalid:
            reason = (flags.invalid_reason or "").strip()
            hints.append(
                "该 Mod 已标记为失效"
                + (f"（{reason}）" if reason else "")
            )
        try:
            st = get_db().get_mod_status(mod_id)
            if st is not None and st.conflict_status == "conflict":
                hints.append("该 Mod 已标记存在冲突")
        except Exception:  # noqa: BLE001
            pass
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
        self._stop_deploy_watchdog()
        data = result if isinstance(result, dict) else {"success": False, "error": str(result)}
        mid = self._deploy_mod_id or str(data.get("mod_id") or "")
        if getattr(self, "_deploy_ui_timed_out", False):
            logger.warning(
                "[DEPLOY_STALL] late worker result after UI timeout mod_id=%s "
                "success=%s — converging projection from DB",
                mid,
                data.get("success"),
            )
            # Do not leave temporary deploy UI / projection stuck: reload
            # mods.deploy_status (+ deployment record overlays via filter).
            if mid:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(str(mid))
                if self._status_filter == FILTER_DEPLOYMENT_RECORD:
                    self._cached_record_mod_ids = None
                    self._sync_record_overlays()
            return
        self.detail_panel.apply_deploy_result(data)
        # Status stays in DetailPanel — no modal dialogs.
        if data.get("success"):
            self._persist_wh3_activation_state()
        focus = mid if data.get("success") else ""
        self._refresh_mod_ui(mid, focus_mod_id=focus)

    def _on_deploy_failed(self, error: object) -> None:
        self._stop_deploy_watchdog()
        if getattr(self, "_deploy_ui_timed_out", False):
            mid = self._deploy_mod_id or ""
            if mid:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(str(mid))
            return
        # Failure terminal: paint once from the result payload, then single refresh.
        if isinstance(error, dict):
            data = error
            mid = self._deploy_mod_id or str(data.get("mod_id") or "")
            self.detail_panel.apply_deploy_result(data)
            self._refresh_mod_ui(mid)
            return
        self.detail_panel.apply_deploy_failure(str(error or "部署失败"))
        self._refresh_mod_ui(self._deploy_mod_id or "")

    def _on_deploy_thread_finished(self) -> None:
        self._stop_deploy_watchdog()
        self._deploy_worker = None
        self._deploy_mod_id = None
        self._deploy_ui_timed_out = False

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
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(str(mod_id))
        if self._status_filter == FILTER_DEPLOYMENT_RECORD:
            # Re-resolve record membership after deploy mutation.
            self._cached_record_mod_ids = None
            self._sync_record_overlays()
        else:
            for _index, card in self._card_entries:
                if card._mod_id() == str(mod_id):
                    card.clear_record_overlay()
                    break
        self._restore_scroll_after_layout(scroll, focus_mod_id=anchor_id)

    # ------------------------------------------------------------------
    # Game list / cards
    # ------------------------------------------------------------------

    def _rebuild_game_list(
        self,
        manager: ModFileManager,
        prefer: str | None = None,
        snapshot=None,
    ) -> None:
        """
        Build the game sidebar from SQLite games + mods aggregates (primary).

        Does **not** scan the filesystem. Does **not** require Steam workshop
        sync / ``workshop_path``.
        """
        del manager  # Filesystem manager must not drive Library sidebar.
        snap = snapshot if snapshot is not None else self._library_snapshot
        game_meta: dict[str, object] = {}
        if snap is not None:
            fp = (
                int(snap.total_count),
                tuple(
                    (
                        str(g.folder),
                        str(g.display),
                        int(g.app_id),
                        int(g.count),
                        str(getattr(g, "game_status", "") or ""),
                        str(
                            getattr(
                                getattr(g, "status_summary", None),
                                "overall_status",
                                "",
                            )
                            or ""
                        ),
                    )
                    for g in snap.games
                ),
                str(prefer or ""),
            )
            if (
                fp == getattr(self, "_game_list_fp", None)
                and self.game_list.count() > 0
            ):
                self.game_list.blockSignals(True)
                self._select_preferred_game_row(prefer)
                self.game_list.blockSignals(False)
                return
            self._game_list_fp = fp

        self.game_list.blockSignals(True)
        self.game_list.clear()

        if snap is not None:
            total = int(snap.total_count)
            self._add_game_list_item(ALL_GAMES_LABEL, total, key="", game_id=0)
            entries: dict[str, tuple[str, int, int, str]] = {}
            for g in snap.games:
                entries[g.folder] = (
                    g.display,
                    int(g.app_id),
                    int(g.count),
                    str(getattr(g, "game_status", "") or "healthy"),
                )
                game_meta[g.folder] = g
        else:
            # No snapshot yet — build sidebar from DB games + mods aggregates only.
            # ARCHITECTURE RULE: never list_games()/iterdir filesystem here;
            # never fall back to list_visible_mods.
            from services.game_sidebar import build_game_sidebar_view_models

            try:
                vms = build_game_sidebar_view_models()
            except Exception:  # noqa: BLE001
                logger.debug("DB game sidebar failed", exc_info=True)
                vms = []
            total = sum(int(v.count) for v in vms)
            self._add_game_list_item(ALL_GAMES_LABEL, total, key="", game_id=0)
            entries = {
                v.folder: (
                    v.display,
                    int(v.app_id),
                    int(v.count),
                    str(v.game_status or "healthy"),
                )
                for v in vms
            }

        from services.game_status import format_status_tooltip

        for key in sorted(entries.keys(), key=str.lower):
            packed = entries[key]
            if len(packed) == 4:
                display, app_id, count, game_status = packed
            else:
                display, app_id, count = packed
                game_status = "healthy"
            summary = None
            meta = game_meta.get(key)
            if meta is not None:
                summary = getattr(meta, "status_summary", None)
            elif snap is not None:
                for game_entry in snap.games:
                    if game_entry.folder == key:
                        summary = getattr(game_entry, "status_summary", None)
                        break
            overall = ""
            tip = ""
            if summary is not None:
                overall = str(getattr(summary, "overall_status", "") or "")
                tip = format_status_tooltip(summary)
            self._add_game_list_item(
                display,
                count,
                key=key,
                game_id=app_id,
                game_status=game_status,
                overall_status=overall,
                status_tip=tip,
            )

        prefer_key = prefer or ""
        if prefer_key == ALL_GAMES_LABEL:
            prefer_key = ""

        self._select_preferred_game_row(prefer)
        self._pending_game_filter = None
        self.game_list.blockSignals(False)

    def _select_preferred_game_row(self, prefer: str | None) -> None:
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
        gid = int(current.data(GAME_ID_ROLE) or 0) if current else 0
        self._set_current_game_context(key or None, game_id=gid or None)

    def _count_mods_for_category(
        self,
        game_key: str,
        category: str,
        manager: ModFileManager,
    ) -> int:
        label = str(category or "").strip()
        if not game_key or not label:
            return 0
        snap = self._library_snapshot
        if snap is not None:
            count = 0
            for card in snap.cards:
                if card.game_folder != game_key:
                    continue
                tags = str(card.category_tags or "").split()
                if tags and tags[0] == label:
                    count += 1
            return count
        count = 0
        try:
            db = get_db()
            for row in db.list_mod_list_items(game_folder=game_key):
                mid = str(row.get("internal_id") or "")
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
        game_status: str = "",
        overall_status: str = "",
        status_tip: str = "",
    ) -> None:
        """Steam-sidebar row via item widget only (empty item text avoids ghost paint)."""
        # Empty DisplayRole — QListWidgetItem text + setItemWidget stacked = 重影.
        item = QListWidgetItem()
        item.setData(GAME_ROLE, key)
        item.setData(GAME_ID_ROLE, int(game_id or 0))
        item.setData(GAME_CATEGORY_ROLE, str(category or ""))
        tip = str(status_tip or "").strip()
        if not tip:
            tip = f"{name}  ·  {count}" if not category else f"{name}  ·  {count}"
            if str(game_status or "").strip() == "missing_folder":
                tip = f"{tip}\n⚠ Mod目录不存在\n但备份数据仍存在"
        item.setToolTip(tip)
        if category:
            kind = _GameFilterRow.KIND_CATEGORY
        elif not key:
            kind = _GameFilterRow.KIND_ALL
        else:
            kind = _GameFilterRow.KIND_GAME
        row = _GameFilterRow(
            name,
            count,
            kind=kind,
            show_count=True,
            indent=indent or bool(category),
            expandable=expandable and not category,
            expanded=expanded,
            game_status="" if category else game_status,
            overall_status=overall_status,
            status_tip=tip,
        )
        item.setSizeHint(row.sizeHint())
        vw = int(self.game_list.viewport().width() or 0)
        if vw > 0:
            hint = item.sizeHint()
            hint.setWidth(vw)
            item.setSizeHint(hint)
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
        del item

    def _on_game_list_context_menu(self, pos) -> None:
        del pos

    def _on_game_item_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        from services.library_perf_metrics import get_library_perf_metrics
        from services.ui_perf_log import PerfScope

        metrics = get_library_perf_metrics()
        metrics.mark_game_switch_begin()
        perf = PerfScope("GAME SWITCH")
        key = ""
        gid = 0
        if current is not None:
            key = current.data(GAME_ROLE) or ""
            gid = int(current.data(GAME_ID_ROLE) or 0)
        perf.phase("context")
        self._set_current_game_context(key or None, game_id=gid or None)
        self._sidebar_category = None
        manager = ModFileManager(self._target_root)
        self._clear_selection()
        self.detail_panel.clear()
        perf.phase("mod list construction")
        # Drop the previous game's offset before the new filtered set is bound.
        self._set_scroll_value(0)
        self._render_mod_cards(manager, force_reload=self._snapshot_dirty)
        self._snapshot_dirty = False
        perf.phase("layout")
        # Game switch: never keep the previous game's scroll offset / range.
        self._sync_library_host_size()
        self._sync_viewport_cards(scroll_y=0)
        self.filter_changed.emit(key or ALL_GAMES_LABEL)
        perf.end()
        metrics.mark_game_switch_end()

    @traced("ModLibraryView._render_mod_cards")
    def _render_mod_cards(
        self, manager: ModFileManager, *, force_reload: bool = True
    ) -> None:
        """
        Build Layer-1 row index for the active game; bind only a viewport window.

        ARCHITECTURE RULE: game switch must not create one QWidget per mod.
        """
        from services.mod_library_cache import (
            card_data_to_metadata,
            get_library_cache,
        )
        from services.ui_perf_log import PerfScope

        self._reload_type_catalog(reconcile=False)
        perf = PerfScope("RENDER_MOD_CARDS")
        t_catalog = time.perf_counter()
        # catalog already reloaded above; note wall for diagnostics
        catalog_ms = (time.perf_counter() - t_catalog) * 1000.0
        snapshot = None
        if not force_reload and self._library_snapshot is not None:
            try:
                same = Path(self._library_snapshot.library_root).resolve() == Path(
                    manager.target_root
                ).resolve()
            except OSError:
                same = str(self._library_snapshot.library_root) == str(
                    manager.target_root
                )
            if same:
                snapshot = self._library_snapshot
        if snapshot is None:
            # Prefer worker-built snapshot; dirty path still DB-first (no FS resolve).
            snapshot = get_library_cache().load_snapshot(
                manager.target_root, force=force_reload
            )
            self._library_snapshot = snapshot
        perf.phase("snapshot")

        t_index = time.perf_counter()
        self._detach_active_cards()
        self._last_filter_sig = None
        game = self._current_game_filter
        rows = list(snapshot.cards)
        if game:
            rows = [c for c in rows if c.game_folder == game]
        perf.phase("filter_game")

        self._game_row_entries = []
        keep_keys: set[str] = set()
        for data in rows:
            folder = Path(data.managed_path)
            mid = str(data.id or "")
            key = self._card_cache_key(folder, mod_id=mid)
            if not key:
                continue
            keep_keys.add(key)
            index = self._filter_index_from_card_data(data)
            self._game_row_entries.append((index, data))

        self._prune_stale_card_cache(keep_keys, game)
        self._card_create_count = 0
        self._card_reuse_count = 0
        self._cards = []
        self._card_entries = []
        index_ms = (time.perf_counter() - t_index) * 1000.0
        try:
            from services.library_perf_metrics import get_library_perf_metrics

            get_library_perf_metrics().note("render_catalog_ms", round(catalog_ms, 2))
            get_library_perf_metrics().note("render_index_build_ms", round(index_ms, 2))
            get_library_perf_metrics().note("render_row_count", len(self._game_row_entries))
        except Exception:  # noqa: BLE001
            pass

        if not self._game_row_entries:
            self._filtered_row_entries = []
            self._refresh_category_combo()
            self._apply_view_filter()
            self._sync_wh3_activation_bar()
            perf.end()
            return

        game_cats = self._merged_category_options(
            collect_category_labels([index for index, _c in self._game_row_entries])
        )
        # Category options applied when cards are bound in the viewport.
        self._viewport_category_options = game_cats
        self._refresh_category_combo()
        perf.phase("index_ready")
        self._apply_view_filter()
        self._sync_wh3_activation_bar()
        perf.end()

    def _clear_flow_except_overlay(self) -> None:
        """Detach card widgets from the card FlowLayout (pads stay in the VBox)."""
        while self.library_layout.count():
            item = self.library_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is None:
                continue
            widget.hide()

    def _ensure_viewport_spacers(self) -> tuple[QWidget, QWidget]:
        assert self._viewport_top_spacer is not None
        assert self._viewport_bottom_spacer is not None
        return self._viewport_top_spacer, self._viewport_bottom_spacer

    def _log_viewport_debug(
        self,
        *,
        container_width: int,
        columns: int,
        visible_items_count: int,
        created_cards_count: int,
        layout_card_count: int,
    ) -> None:
        import os

        enabled = os.environ.get("SMM_VIEWPORT_DEBUG", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        msg = (
            "[VIEWPORT_DEBUG] "
            f"container_width={container_width} "
            f"card_width={CARD_WIDTH} "
            f"columns={columns} "
            f"visible_items_count={visible_items_count} "
            f"created_cards_count={created_cards_count} "
            f"layout_card_count={layout_card_count}"
        )
        if enabled:
            logger.info("%s", msg)
        else:
            logger.debug("%s", msg)

    def _sync_viewport_cards(self, *, scroll_y: int | None = None) -> None:
        """Bind a reusable card pool to the filtered row window (viewport + buffer).

        ARCHITECTURE RULE: vertical pads live in the outer VBox; FlowLayout
        contains only ModCardWidget peers so every row packs the same column count.

        This method is the only bind gate: it recomputes geometry for the
        current ``_filtered_row_entries`` length, clamps ``scroll_y``, and
        passes only a legal offset to the window math.
        """
        if getattr(self, "_viewport_syncing", False):
            return
        t0 = time.perf_counter()
        self._viewport_syncing = True
        try:
            raw = self._capture_scroll() if scroll_y is None else int(scroll_y)
            legal = self._legalize_viewport_scroll(
                raw, self._viewport_item_count()
            )
            if self._is_collection_list_mode():
                self._bind_collection_viewport_cards(scroll_y=legal)
            else:
                self._bind_viewport_cards(scroll_y=legal)
        finally:
            self._viewport_syncing = False
            try:
                from services.library_perf_metrics import get_library_perf_metrics

                get_library_perf_metrics().note(
                    "viewport_bind_ms",
                    round((time.perf_counter() - t0) * 1000.0, 2),
                )
            except Exception:  # noqa: BLE001
                pass

    def _viewport_item_count(self) -> int:
        if self._is_collection_list_mode():
            return len(getattr(self, "_collection_list_entries", ()) or ())
        return len(self._filtered_row_entries)

    def _collection_list_fingerprint(self) -> tuple:
        if not self._is_collection_list_mode():
            return ()
        out: list[tuple] = []
        for item in getattr(self, "_collection_list_entries", []) or []:
            if item is None:
                out.append(("create",))
                continue
            out.append(
                (
                    int(item.collection_id),
                    str(item.name or ""),
                    int(item.mod_count or 0),
                    int(item.sort_order or 0),
                )
            )
        return tuple(out)

    def _collection_content_fingerprint(self) -> tuple:
        if not self._is_collection_content_mode():
            return ()
        cid = int(getattr(self, "_current_collection_id", 0) or 0)
        from services import collection as coll

        members = tuple(coll.list_collection_member_ids(cid)) if cid > 0 else ()
        return (cid, members)

    def _prepare_collection_content_entries(
        self, *, query: str, category_key: str
    ) -> list[str]:
        """Membership → existing Library projection → ``filter_sort_entries``.

        Collection Content reuses ordinary Mod sort/search. It must not call
        ``collection_sort_entries`` or WH3 load-order helpers.
        """
        from services import collection as coll

        cid = int(getattr(self, "_current_collection_id", 0) or 0)
        member_ids = coll.list_collection_member_ids(cid) if cid > 0 else []
        wanted = {str(mid).strip() for mid in member_ids if str(mid).strip()}
        source = [
            (index, payload)
            for index, payload in self._game_row_entries
            if str(getattr(index, "mod_id", "") or "").strip() in wanted
        ]
        # Status chips are grey in Collection Content — do not apply _status_filter.
        self._filtered_row_entries = filter_sort_entries(
            source,
            query=query,
            filter_key=FILTER_ALL,
            platform_key=FILTER_PLATFORM_ALL,
            category_key=category_key,
            sort_mode=self._sort_mode,
            record_mod_ids=None,
        )
        return list(member_ids)

    def _collection_to_card_data(self, record) -> CollectionCardData:
        return CollectionCardData(
            collection_id=int(record.collection_id),
            name=str(record.name or ""),
            cover=str(record.cover_path or ""),
            mod_count=int(record.mod_count or 0),
            sort_order=int(record.sort_order or 0),
            app_id=int(record.app_id or 0),
        )

    def _prepare_collection_list_entries(self) -> None:
        from services import collection as coll

        gid = int(self.current_game_id or 0)
        records = coll.list_collections(gid) if gid > 0 else []
        self._collection_list_entries = [None]
        self._collection_list_entries.extend(
            self._collection_to_card_data(r) for r in records
        )

    def _bind_collection_viewport_cards(self, *, scroll_y: int) -> None:
        rows = list(getattr(self, "_collection_list_entries", []) or [])
        viewport_w, viewport_h = self._viewport_metrics()
        legal = clamp_scroll_y(
            int(scroll_y),
            len(rows),
            viewport_w,
            viewport_h,
        )
        window = compute_viewport_window(
            item_count=len(rows),
            scroll_y=legal,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        )
        total_h = max(window.total_height, estimate_total_height(len(rows), viewport_w))
        self.library_host.setMinimumHeight(max(0, total_h))

        self._clear_flow_except_overlay()
        top_pad, bottom_pad = self._ensure_viewport_spacers()
        top_h = max(0, window.top_pad)
        bot_h = max(0, window.bottom_pad)
        top_pad.setFixedHeight(top_h)
        bottom_pad.setFixedHeight(bot_h)
        top_pad.setVisible(top_h > 0)
        bottom_pad.setVisible(bot_h > 0)

        cards: list[QWidget] = []
        parent = self._cards_host if self._cards_host is not None else self.library_host
        slice_rows = rows[window.first_index : window.last_index]
        for item in slice_rows:
            if item is None:
                key = COLLECTION_CREATE_CACHE_KEY
                card = self._collection_card_cache.get(key)
                if not isinstance(card, CollectionCreateCard):
                    if card is not None:
                        card.hide()
                        card.deleteLater()
                    card = CollectionCreateCard(parent=parent)
                    card.create_requested.connect(self._on_create_collection)
                    self._collection_card_cache[key] = card
            else:
                key = collection_cache_key(item.collection_id)
                card = self._collection_card_cache.get(key)
                if isinstance(card, CollectionCardWidget):
                    card.rebind(item)
                else:
                    if card is not None:
                        card.hide()
                        card.deleteLater()
                    card = CollectionCardWidget(item, parent=parent)
                    card.rename_requested.connect(self._on_rename_collection)
                    card.cover_requested.connect(self._on_collection_cover_requested)
                    card.delete_requested.connect(self._on_delete_collection)
                    card.sort_drop_requested.connect(self._on_collection_sort_drop)
                    card.open_requested.connect(self._open_collection_content)
                    self._collection_card_cache[key] = card
            self.library_layout.addWidget(card)
            card.show()
            cards.append(card)

        live_keys = {
            COLLECTION_CREATE_CACHE_KEY
            if item is None
            else collection_cache_key(item.collection_id)
            for item in slice_rows
        }
        for key, stale in list(self._collection_card_cache.items()):
            if key not in live_keys:
                stale.hide()
        self._collection_cards = cards
        self._cards = []
        self._card_entries = []
        self.library_layout.invalidate()
        if self._cards_host is not None:
            self._cards_host.updateGeometry()

    def _clear_collection_card_cache(self) -> None:
        for card in list(getattr(self, "_collection_card_cache", {}).values()):
            card.hide()
            card.deleteLater()
        self._collection_card_cache = {}
        self._collection_cards = []
        self._collection_list_entries = []

    def _on_create_collection(self) -> None:
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return
        name, ok = QInputDialog.getText(self, "创建合集", "合集名称：")
        if not ok:
            return
        from services import collection as coll

        try:
            coll.create_collection(gid, name)
        except ValueError as exc:
            text = str(exc)
            if "non-empty" in text:
                QMessageBox.warning(self, "创建合集", "名称不能为空。")
            elif "already exists" in text:
                QMessageBox.information(
                    self, "创建合集", f"「{str(name or '').strip()}」已存在。"
                )
            else:
                QMessageBox.warning(self, "创建合集失败", text)
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "创建合集失败", str(exc))
            return
        self._last_filter_sig = None
        self._apply_view_filter()

    def _on_rename_collection(self, collection_id: int) -> None:
        from services import collection as coll

        rec = coll.get_collection(collection_id)
        current = rec.name if rec is not None else ""
        name, ok = QInputDialog.getText(
            self, "编辑合集名称", "合集名称：", text=current
        )
        if not ok:
            return
        try:
            coll.rename_collection(collection_id, name)
        except ValueError as exc:
            text = str(exc)
            if "non-empty" in text:
                QMessageBox.warning(self, "重命名合集", "名称不能为空。")
            elif "already exists" in text:
                QMessageBox.information(
                    self, "重命名合集", f"「{str(name or '').strip()}」已存在。"
                )
            else:
                QMessageBox.warning(self, "重命名合集失败", text)
            return
        except LookupError as exc:
            QMessageBox.warning(self, "重命名合集失败", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重命名合集失败", str(exc))
            return
        self._last_filter_sig = None
        self._apply_view_filter()

    def _on_collection_cover_requested(self, collection_id: int) -> None:
        from services import collection as coll

        rec = coll.get_collection(collection_id)
        if rec is None:
            return
        members = coll.list_collection_member_ids(collection_id)
        menu = QMenu(self)
        act_local = menu.addAction("本地图片")
        act_member = menu.addAction("从合集 Mod 选择")
        act_member.setEnabled(bool(members))
        card = self._collection_card_cache.get(collection_cache_key(collection_id))
        if isinstance(card, CollectionCardWidget):
            origin = card.btn_cover.mapToGlobal(card.btn_cover.rect().bottomLeft())
        else:
            origin = QCursor.pos()
        chosen = menu.exec(origin)
        if chosen is act_local:
            self._set_collection_cover_from_file(collection_id)
        elif chosen is act_member:
            self._set_collection_cover_from_member(collection_id)

    def _refresh_collection_list_after_cover(self) -> None:
        self._last_filter_sig = None
        self._apply_view_filter()

    def _set_collection_cover_from_file(self, collection_id: int) -> None:
        from services import collection as coll
        from services.importers.import_settings import (
            resolve_import_start_directory,
            set_last_import_directory,
        )

        start = resolve_import_start_directory()
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择合集封面",
            start,
            "Images (*.png *.jpg *.jpeg *.jfif *.webp);;All files (*.*)",
        )
        if not chosen:
            return
        set_last_import_directory(chosen)
        try:
            coll.set_collection_cover(collection_id, chosen)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "设置合集封面", str(exc))
            return
        self._refresh_collection_list_after_cover()

    def _set_collection_cover_from_member(self, collection_id: int) -> None:
        from services import collection as coll
        from ui.collection_cover_dialog import open_collection_cover_dialog

        rec = coll.get_collection(collection_id)
        name = rec.name if rec is not None else ""
        source = open_collection_cover_dialog(
            collection_id, name, parent=self
        )
        if source is None:
            return
        try:
            coll.set_collection_cover(collection_id, source)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "设置合集封面", str(exc))
            return
        self._refresh_collection_list_after_cover()

    def _on_delete_collection(self, collection_id: int) -> None:
        from services import collection as coll

        rec = coll.get_collection(collection_id)
        label = rec.name if rec is not None else str(collection_id)
        confirm = QMessageBox.question(
            self,
            "删除合集",
            f"删除合集「{label}」？\n合集内的 Mod 不会被删除。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            coll.delete_collection(collection_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除合集失败", str(exc))
            return
        key = collection_cache_key(collection_id)
        stale = self._collection_card_cache.pop(key, None)
        if stale is not None:
            stale.hide()
            stale.deleteLater()
        self._last_filter_sig = None
        self._apply_view_filter()

    def _on_set_collections_requested(self) -> None:
        """Apply Collection membership for ``_selected_mod_ids`` in one transaction."""
        ids = [str(m).strip() for m in self._selected_mod_ids if str(m).strip()]
        if not ids:
            return
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return
        from services import collection as coll
        from ui.collection_membership_dialog import CollectionMembershipDialog

        rows = coll.membership_check_states(gid, ids)
        if not rows:
            QMessageBox.information(
                self, "设置合集", "当前游戏还没有合集。请先进入合集模式创建。"
            )
            return
        dialog = CollectionMembershipDialog(rows, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        add_ids, remove_ids = dialog.edits()
        dialog.deleteLater()
        if not add_ids and not remove_ids:
            return
        try:
            coll.apply_collection_memberships(gid, ids, add_ids, remove_ids)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "设置合集失败", str(exc))
            return
        self._last_filter_sig = None
        self._apply_view_filter()

    def _on_collection_sort_drop(self, source_id: str, target_id: str) -> None:
        from services import collection as coll

        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return
        ids = [
            item.collection_id
            for item in self._collection_list_entries
            if item is not None
        ]
        next_order = coll.move_collection_in_order(ids, source_id, target_id)
        if next_order == ids:
            return
        coll.reorder_collections(gid, next_order)
        self._last_filter_sig = None
        self._apply_view_filter()

    @traced("ModLibraryView._bind_viewport_cards")
    def _bind_viewport_cards(self, *, scroll_y: int) -> None:
        from services.mod_library_cache import card_data_to_metadata
        from services.ui_block_trace import log_ui_block

        t_bind = time.perf_counter()
        rows = list(self._filtered_row_entries)
        viewport_w, viewport_h = self._viewport_metrics()
        legal = clamp_scroll_y(
            int(scroll_y),
            len(rows),
            viewport_w,
            viewport_h,
        )
        window = compute_viewport_window(
            item_count=len(rows),
            scroll_y=legal,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        )
        total_h = max(window.total_height, estimate_total_height(len(rows), viewport_w))
        self.library_host.setMinimumHeight(max(0, total_h))

        self._clear_flow_except_overlay()
        top_pad, bottom_pad = self._ensure_viewport_spacers()
        top_h = max(0, window.top_pad)
        bot_h = max(0, window.bottom_pad)
        top_pad.setFixedHeight(top_h)
        bottom_pad.setFixedHeight(bot_h)
        if top_h > 0:
            top_pad.show()
        else:
            top_pad.hide()
        if bot_h > 0:
            bottom_pad.show()
        else:
            bottom_pad.hide()

        cards: list[ModCardWidget] = []
        entries: list[tuple[ModFilterIndex, ModCardWidget]] = []
        created = 0
        reused = 0
        cats = list(getattr(self, "_viewport_category_options", []) or [])
        slice_rows = rows[window.first_index : window.last_index]
        for index, data in slice_rows:
            folder = Path(getattr(data, "managed_path", "") or "")
            mid = str(getattr(data, "id", "") or getattr(index, "mod_id", "") or "")
            key = self._card_cache_key(folder, mod_id=mid)
            if not key:
                continue
            meta = card_data_to_metadata(data)
            card = self._card_cache.get(key)
            if card is None:
                parent = self._cards_host if self._cards_host is not None else self.library_host
                card = ModCardWidget(
                    folder,
                    metadata=meta,
                    parent=parent,
                    card_data=data,
                )
                self._connect_card_signals(card)
                self._card_cache[key] = card
                created += 1
            else:
                prev = getattr(card, "_card_data", None)
                same_proj = (
                    prev is data
                    or (
                        prev is not None
                        and str(getattr(prev, "id", "") or "") == mid
                        and str(getattr(prev, "cover", "") or "")
                        == str(getattr(data, "cover", "") or "")
                        and bool(getattr(prev, "folder_absent", False))
                        == bool(getattr(data, "folder_absent", False))
                        and str(getattr(prev, "title", "") or "")
                        == str(getattr(data, "title", "") or "")
                        and str(getattr(prev, "deploy_status", "") or "")
                        == str(getattr(data, "deploy_status", "") or "")
                        and Path(str(getattr(prev, "managed_path", "") or "")) == folder
                    )
                )
                if same_proj:
                    reused += 1
                else:
                    card.rebind(folder, meta, card_data=data)
                    reused += 1
            if cats:
                card.set_category_options(cats)
            self._bind_wh3_sort_card(card, data)
            self._reveal_card(card)
            cards.append(card)
            entries.append((index, card))

        shown = {id(card) for card in cards}
        del shown  # selection uses card objects; ids used only for clarity above
        prev_live = list(getattr(self, "_viewport_live_cards", []) or [])
        live_set = set(cards)
        # Only hide cards that left this window — never walk the full cache.
        for old in prev_live:
            if old is not None and old not in live_set:
                old.hide()
        self._viewport_live_cards = list(cards)

        self._cards = cards
        self._card_entries = entries
        self._card_create_count = created
        self._card_reuse_count = reused
        self._viewport_last_width = viewport_w
        live_keys = {
            self._card_cache_key(
                Path(getattr(data, "managed_path", "") or ""),
                mod_id=str(
                    getattr(data, "id", "") or getattr(index, "mod_id", "") or ""
                ),
            )
            for index, data in slice_rows
        }
        live_keys.discard("")
        self._trim_card_cache_budget(live_keys=live_keys)
        bind_ms = (time.perf_counter() - t_bind) * 1000.0
        try:
            from services.library_perf_metrics import get_library_perf_metrics

            get_library_perf_metrics().record_viewport(
                cards_created=created,
                cards_reused=reused,
                visible_cards=len(cards),
            )
            log_ui_block(
                "modcard_viewport_bind",
                bind_ms,
                created=created,
                reused=reused,
                visible=len(cards),
                cache=len(self._card_cache),
                window=f"{window.first_index}:{window.last_index}",
            )
            from services.perf_stage import log_perf_stage

            log_perf_stage(
                "viewport_bind",
                bind_ms,
                created=created,
                reused=reused,
                visible=len(cards),
                cache=len(self._card_cache),
            )
            log_perf_stage(
                "widget_create",
                bind_ms if created else 0.0,
                created=created,
                reused=reused,
            )
        except Exception:  # noqa: BLE001
            pass
        self.library_layout.invalidate()
        if self._cards_host is not None:
            self._cards_host.updateGeometry()
        self._log_viewport_debug(
            container_width=viewport_w,
            columns=window.columns,
            visible_items_count=len(slice_rows),
            created_cards_count=created,
            layout_card_count=self.library_layout.count(),
        )
        # rebind/new card clears memory-only record overlays — restore after bind.
        self._sync_record_overlays()
        # Selection authority is mod_id; restore viewport styles after rebind.
        self._rematerialize_selection_from_ids()

    def _trim_card_cache_budget(self, *, live_keys: set[str]) -> None:
        """Drop off-viewport cached cards when the pool exceeds the budget."""
        budget = max(LIBRARY_CARD_CACHE_BUDGET, len(live_keys) * 2)
        if len(self._card_cache) <= budget:
            return
        for key in list(self._card_cache.keys()):
            if key in live_keys:
                continue
            self._drop_cache_key(key)
            if len(self._card_cache) <= budget:
                return

    def _filter_index_from_card_data(self, data) -> ModFilterIndex:
        folder_name = Path(data.managed_path).name
        source_type = str(getattr(data, "source_type", "") or "")
        content_status = str(getattr(data, "content_status", "") or "")
        identity_status = str(getattr(data, "identity_status", "") or "ok")
        size_status = str(getattr(data, "size_status", "") or "unknown") or "unknown"
        return ModFilterIndex(
            mod_id=str(data.id or ""),
            display_name=data.title,
            steam_name=data.steam_name,
            notes=data.notes,
            game_name=data.game_name,
            favorite=data.favorite,
            deployed=data.deployed,
            has_offline=data.has_offline,
            mtime=float(data.updated_time or 0.0),
            sort_name=data.title or data.steam_name or folder_name,
            invalid=data.invalid,
            conflict=data.conflict,
            tag_values=data.tag_values,
            platform=data.platform,
            source_url=data.source_url,
            external_id=data.external_id,
            workspace_id=str(getattr(data, "workspace_id", "") or ""),
            is_invalid=data.invalid,
            conflict_status=data.conflict_status,
            enabled=data.enabled,
            category_tags=data.category_tags,
            type_id=getattr(data, "type_id", None),
            content_status=content_status,
            identity_status=identity_status,
            source_type=source_type,
            local_size_bytes=data.size if size_status == "ok" else None,
            local_size_status=size_status,
        )

    @staticmethod
    def _card_cache_key(folder: Path, mod_id: str = "") -> str:
        """Viewport pool key = internal_id only (never path / folder / workspace)."""
        del folder  # path must never become the reuse key
        mid = str(mod_id or "").strip()
        if mid:
            return f"entity:{mid}"
        return ""

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
        card.set_collections_requested.connect(self._on_set_collections_requested)
        card.sort_drop_requested.connect(self._on_wh3_sort_drop)

    def _detach_active_cards(self) -> None:
        """Hide / detach from layout without destroying widgets (card cache reuse)."""
        self._last_clicked_index = 0
        self._clear_selection()
        while self.library_layout.count():
            item = self.library_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
        for card in self._cards:
            card.hide()
        for card in list(getattr(self, "_collection_card_cache", {}).values()):
            card.hide()
        if self._viewport_top_spacer is not None:
            self._viewport_top_spacer.setFixedHeight(0)
            self._viewport_top_spacer.hide()
        if self._viewport_bottom_spacer is not None:
            self._viewport_bottom_spacer.setFixedHeight(0)
            self._viewport_bottom_spacer.hide()
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
        Prune deleted entities without wiping other games' reuse cache.

        Keys are internal_id. When a game is selected, keep other games' cards
        for cross-game reuse; drop same-game leftovers not in *keep_keys*.
        """
        game_name = str(game or "").strip()
        for key in list(self._card_cache.keys()):
            if key in keep_keys:
                continue
            if not key:
                self._drop_cache_key(key)
                continue
            if not game_name:
                self._drop_cache_key(key)
                continue
            card = self._card_cache.get(key)
            data = getattr(card, "_card_data", None) if card is not None else None
            card_game = str(getattr(data, "game_folder", "") or "") if data else ""
            if not card_game or card_game == game_name:
                self._drop_cache_key(key)

    def _prune_card_cache(self, keep_keys: set[str]) -> None:
        for key in list(self._card_cache.keys()):
            if key in keep_keys:
                continue
            self._drop_cache_key(key)
    def _clear_cards(self) -> None:
        """Clear active lists and destroy cached cards (full wipe)."""
        self._detach_active_cards()
        self._clear_collection_card_cache()
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
        self.empty_action_btn.setVisible(bool(str(action or "").strip()))
        self.empty_overlay.setGeometry(self.library_host.rect())
        self.empty_overlay.show()
        self.empty_overlay.raise_()

    def _on_empty_action(self) -> None:
        kind = self._empty_kind
        if kind == EMPTY_SEARCH:
            self.search_box.clear()
            self._set_library_status_filter(FILTER_ALL)
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

    def _on_card_edit_requested(self, mod_id: object) -> None:
        mid = str(mod_id or "").strip()
        card = self._card_for_mod_id(mid) if mid else None
        if card is None:
            return
        self._select_card(card, show_panel=True)
        self.detail_panel.enter_edit()

    def _on_card_open_folder(self, mod_id: object) -> None:
        mid = str(mod_id or "").strip()
        card = self._card_for_mod_id(mid) if mid else None
        if card is not None:
            self._select_card(card, show_panel=True)
        self.detail_panel._open_folder()

    def _on_card_open_steam(self, mod_id: object) -> None:
        mid = str(mod_id or "").strip()
        card = self._card_for_mod_id(mid) if mid else None
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
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)
        scroll = self._capture_scroll()
        self._restore_scroll_after_layout(scroll, focus_mod_id=mid)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_filter_row_height()
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
        self._schedule_visible_covers()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._cancel_all_pending_covers()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.empty_overlay.isVisible():
            self.empty_overlay.setGeometry(self.library_host.rect())
        if self.loading_overlay.isVisible():
            self.loading_overlay.setGeometry(self.scroll.viewport().rect())
        # Width change ⇒ column count may change — resync viewport window + pool.
        vp = self.scroll.viewport()
        width = int(vp.width()) if vp is not None else 0
        if width > 0 and width != int(getattr(self, "_viewport_last_width", 0) or 0):
            if self._filtered_row_entries:
                self._sync_viewport_cards()
        self._schedule_viewport_scroll_clamp()
        self._schedule_visible_covers()
