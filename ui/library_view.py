"""Mod Library view — game filter + card grid + detail panel."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal, Slot
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
    QToolButton,
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
    FILTER_DEPLOYMENT_RECORD,
    FILTER_PLATFORM_ALL,
    PLATFORM_FILTER_LABELS,
    SORT_LABELS,
    SORT_MTIME,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
    coerce_filter_selection,
    collect_category_labels,
    collect_source_keys,
    compute_record_relative_status,
    filter_and_sort,
    folder_mtime,
    merge_category_labels,
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
        self._deploy_watchdog = QTimer(self)
        self._deploy_watchdog.setSingleShot(True)
        self._deploy_watchdog.setInterval(180_000)
        self._deploy_watchdog.timeout.connect(self._on_deploy_watchdog_timeout)
        self._status_filter = FILTER_ALL
        self._platform_filter = FILTER_PLATFORM_ALL
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
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_view_filter)
        self._cover_sched = QTimer(self)
        self._cover_sched.setSingleShot(True)
        self._cover_sched.setInterval(40)
        self._cover_sched.timeout.connect(self._load_viewport_covers)

        self._build_ui()
        self._reconcile_idle.connect(
            self._flush_pending_library_load,
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
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)
        title_col.addWidget(self._page_title)
        title_col.addWidget(self.count_label)
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

        # --- Status chips + Deployment Record on one row (no extra height) ---
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_buttons: dict[str, QPushButton] = {}

        self._status_bar = QWidget()
        self._status_bar.setObjectName("libraryFilterBar")
        self._status_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        status_row = QHBoxLayout(self._status_bar)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)

        self._status_chips = QWidget(self._status_bar)
        self._status_chips.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
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
        status_row.addWidget(self._status_chips, 1)

        self.btn_deployment_record = QToolButton(self._status_bar)
        self.btn_deployment_record.setObjectName("libraryDeploymentRecordButton")
        self.btn_deployment_record.setText("💾 部署记录 ▼")
        self.btn_deployment_record.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        # Entire control opens one Popup (filter + manage) — no split body/arrow/右键.
        self.btn_deployment_record.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.btn_deployment_record.setToolTip("部署记录筛选与管理")
        self._deployment_record_menu = QMenu(self.btn_deployment_record)
        self.btn_deployment_record.setMenu(self._deployment_record_menu)
        self._deployment_record_menu.aboutToShow.connect(
            self._rebuild_deployment_record_menu
        )
        status_row.addWidget(
            self.btn_deployment_record,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        center_layout.addWidget(self._status_bar)

        self._platform_group = QButtonGroup(self)
        self._platform_group.setExclusive(True)
        self._platform_buttons: dict[str, QPushButton] = {}
        self._source_row = QWidget()
        self._source_row.setObjectName("libraryFilterBar")
        self._source_row.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        source_layout = QHBoxLayout(self._source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(8)
        source_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._source_caption = QLabel("来源")
        self._source_caption.setObjectName("fieldCaption")
        self._source_caption.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        source_layout.addWidget(
            self._source_caption, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self._platform_bar = QWidget()
        self._platform_bar.setObjectName("libraryFilterBar")
        self._platform_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._platform_flow = FlowLayout(
            self._platform_bar, margin=0, h_spacing=6, v_spacing=6
        )
        source_layout.addWidget(
            self._platform_bar, 1, Qt.AlignmentFlag.AlignVCenter
        )
        center_layout.addWidget(self._source_row)
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
        self.btn_delete_game_type.setToolTip("从当前游戏的类型目录中删除选中项，不修改已有 Mod 标签")
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
        self.scroll.verticalScrollBar().valueChanged.connect(
            self._on_library_scroll_covers
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
        self.detail_panel.relocate_completed.connect(self._on_relocate_completed)
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

    def refresh(self, *, force: bool = True) -> None:
        """Reload library listing from disk and rebuild cards (UI-thread safe).

        Read + render only — no network, Steam archive, migration, or sync.
        Shows a loading overlay so the window is never silent during work.

        *force*:
          - ``True`` (Refresh button / import): rebuild snapshot from disk
          - ``False`` (nav back to Library): reuse warm ``ModLibraryCache`` when present
        """
        scroll = self._capture_scroll()
        keep_path = self._selected_path
        keep_mod_id = (
            self._selected_card._mod_id() if self._selected_card is not None else ""
        )
        self._pending_restore = {
            "scroll": scroll,
            "path": keep_path,
            "mod_id": keep_mod_id,
        }
        self._set_loading(True)
        root = Path(self._target_root)
        root.mkdir(parents=True, exist_ok=True)
        # Reconcile syncs backup (sha256/copytree) for every mod — never block
        # UI / show(). Startup and refresh only schedule the same work async.
        # Deduped inside start_reconcile_library_async.
        # LibraryLoadWorker must not overlap library-reconcile: pytest/sync
        # still starts reconcile first (existing tests); the async worker
        # path sets pending *before* starting reconcile to avoid a missed idle.
        if force:
            try:
                from services.startup_io_trace import log_io_event

                log_io_event("library_refresh", "start", force=1)
            except Exception:  # noqa: BLE001
                pass
            if _library_load_sync():
                try:
                    from services.library_reconcile import start_reconcile_library_async

                    start_reconcile_library_async(root)
                except Exception:  # noqa: BLE001
                    logger.debug("start_reconcile_library_async failed", exc_info=True)

        if not force:
            try:
                from services.mod_library_cache import get_library_cache

                cache = get_library_cache()
                snap = cache.peek_snapshot(root)
                if snap is not None:
                    self._library_load_pending = False
                    self._apply_library_snapshot(snap)
                    self._finish_library_load()
                    return
            except Exception:  # noqa: BLE001
                logger.debug("soft library cache miss", exc_info=True)

        if _library_load_sync():
            try:
                from services.mod_library_cache import get_library_cache

                snapshot = get_library_cache().load_snapshot(root, force=True)
                self._apply_library_snapshot(snapshot)
            finally:
                self._library_load_pending = False
                self._finish_library_load()
            return

        self._library_load_pending = True
        if force:
            try:
                from services.library_reconcile import start_reconcile_library_async

                start_reconcile_library_async(root)
            except Exception:  # noqa: BLE001
                logger.debug("start_reconcile_library_async failed", exc_info=True)
        try:
            from services.library_reconcile import library_load_must_wait
        except Exception:  # noqa: BLE001
            def library_load_must_wait() -> bool:
                return False
        if library_load_must_wait():
            try:
                from services.startup_io_trace import log_io_event

                log_io_event("library_load", "deferred", waiting="reconcile")
            except Exception:  # noqa: BLE001
                pass
            return
        self._flush_pending_library_load()

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
        try:
            from services.library_reconcile import library_load_must_wait
        except Exception:  # noqa: BLE001
            def library_load_must_wait() -> bool:
                return False
        if library_load_must_wait():
            return
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

    def _on_library_loaded(self, snapshot, generation: int) -> None:
        if int(generation) != self._load_gen:
            return
        try:
            self._apply_library_snapshot(snapshot)
        finally:
            self._finish_library_load()

    def _on_library_failed(self, message: str, generation: int) -> None:
        if int(generation) != self._load_gen:
            return
        logger.warning("library load failed: %s", message)
        self._set_loading(False)
        self._pending_restore = None

    def _apply_library_snapshot(self, snapshot) -> None:
        self._library_snapshot = snapshot
        self._snapshot_dirty = False
        manager = ModFileManager(self._target_root)
        previous = self._current_game_filter
        pending = self._pending_game_filter
        self._rebuild_game_list(manager, prefer=pending or previous, snapshot=snapshot)
        self._render_mod_cards(manager, force_reload=False)

    def _finish_library_load(self) -> None:
        pending = self._pending_restore or {}
        keep_path = pending.get("path")
        keep_mod_id = str(pending.get("mod_id") or "")
        scroll = int(pending.get("scroll") or 0)
        focus_id = keep_mod_id
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
        self._set_loading(False)
        self._restore_scroll_after_layout(scroll, focus_mod_id=focus_id)
        self._pending_restore = None

    def _mark_card_stale(self, card) -> None:
        """Drop batched card data so the next paint reads live SQLite."""
        card._card_data = None
        # Card metadata changed — next full snapshot rebuild is required.
        self._snapshot_dirty = True

    def _capture_scroll(self) -> int:
        return int(self.scroll.verticalScrollBar().value())

    def _set_scroll_value(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(int(value), bar.maximum())))

    def _on_library_flow_height(self, height: int) -> None:
        """Shrink/grow library_host with FlowLayout content (scrollbar range)."""
        self.library_host.setMinimumHeight(max(0, int(height)))
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
            from services.mod_metadata_resolver import list_visible_mods

            for item in list_visible_mods(
                manager.target_root, name
            ):
                if int(item.app_id or 0) > 0:
                    return int(item.app_id)
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
        """Refresh only the affected card after a single-mod offline download."""
        path = Path(str(mod_path))
        card = self._card_for_path(path)
        if card is not None:
            self._mark_card_stale(card)
            card.refresh_display()
            self._refresh_mod_ui(card._mod_id())

    def _on_relocate_completed(self, mod_id: str) -> None:
        """Full refresh after a successful missing-folder relocate."""
        mid = str(mod_id or "").strip()
        self.refresh()
        if mid:
            QTimer.singleShot(0, lambda: self._focus_mod_after_relocate(mid))

    def _focus_mod_after_relocate(self, mod_id: str) -> None:
        mid = str(mod_id or "").strip()
        for card in self._cards:
            if card._mod_id() == mid:
                self.on_mod_selected(card)
                return

    def _set_loading(self, loading: bool) -> None:
        self._loading = bool(loading)
        self.refresh_btn.setEnabled(not loading)
        self.search_box.setEnabled(not loading)
        self.sort_combo.setEnabled(not loading)
        for btn in self._filter_buttons.values():
            btn.setEnabled(not loading)
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

    def _collect_active_platform_sources(self) -> list[str]:
        """Distinct sources for currently loaded (game-scoped) cards. Snapshot only."""
        return collect_source_keys([index for index, _card in self._card_entries])

    def _rebuild_platform_filter_bar(
        self, available: list[str] | None = None
    ) -> None:
        """Rebuild source chips from the current game's actual sources."""
        from ui.platform_labels import platform_badge_label

        labels = {key: label for key, label in PLATFORM_FILTER_LABELS}
        if available is None:
            available = self._collect_active_platform_sources()
        current = coerce_filter_selection(
            self._platform_filter, available, all_key=FILTER_PLATFORM_ALL
        )

        show_bar = (
            len(
                [
                    key
                    for key in available
                    if key not in (FILTER_PLATFORM_ALL, FILTER_ALL, "")
                ]
            )
            >= 2
        )
        if not show_bar:
            current = FILTER_PLATFORM_ALL

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

        def _add_chip(key: str, label: str) -> None:
            btn = self._make_filter_chip(label, parent=self._platform_bar)
            btn.setCheckable(True)
            self._platform_group.addButton(btn)
            self._platform_buttons[key] = btn
            self._filter_buttons[key] = btn
            self._platform_flow.addWidget(btn)
            btn.toggled.connect(
                lambda checked, k=key: self._on_platform_filter_toggled(k, checked)
            )

        _add_chip(FILTER_PLATFORM_ALL, labels.get(FILTER_PLATFORM_ALL, "全部"))
        for key in available:
            if key in (FILTER_PLATFORM_ALL, FILTER_ALL):
                continue
            label = labels.get(key) or platform_badge_label(key)
            _add_chip(key, label)

        self._platform_filter = current
        pick = self._platform_buttons[current]
        pick.blockSignals(True)
        pick.setChecked(True)
        pick.blockSignals(False)

        self._platform_bar.adjustSize()
        self._source_row.setVisible(show_bar)

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
        if key == FILTER_DEPLOYMENT_RECORD:
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
        """Deployed mod ids in the current Library game view (card index)."""
        from ui.library_query import normalize_record_mod_id

        out: list[str] = []
        seen: set[str] = set()
        for index, _card in self._card_entries:
            if not index.deployed:
                continue
            mid = normalize_record_mod_id(index.mod_id)
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    def _snapshot_mod_ids_for_deployment_record(self) -> list[str] | None:
        """
        Prefer live Library card index; ``None`` → service uses DB library query.

        Empty ``_card_entries`` means the view has not bound this game yet
        (tests / early save) — do not snapshot an empty set by mistake.
        """
        if not self._card_entries:
            return None
        return self._current_library_deployed_mod_ids()

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
        # Selecting a chip clears any deployment-record filter (mutex).
        self._set_library_status_filter(key)

    def _on_platform_filter_toggled(self, key: str, checked: bool) -> None:
        if not checked:
            return
        self._platform_filter = key
        self._apply_view_filter()

    def _on_category_changed(self, _index: int = 0) -> None:
        data = self.category_combo.currentData()
        self._category_filter = str(data or FILTER_CATEGORY_ALL)
        self._sync_type_manage_buttons()
        self._apply_view_filter()

    def _current_game_type_catalog(self) -> list[str]:
        gid = int(self.current_game_id or 0)
        if gid <= 0:
            return []
        try:
            return list(get_db().list_game_categories(gid))
        except Exception:  # noqa: BLE001
            logger.debug("list_game_categories failed", exc_info=True)
            return []

    def _merged_category_options(self, used: list[str] | None = None) -> list[str]:
        if used is None:
            used = collect_category_labels(
                [index for index, _card in self._card_entries]
            )
        return merge_category_labels(self._current_game_type_catalog(), used)

    def _sync_type_manage_buttons(self) -> None:
        if not hasattr(self, "btn_add_game_type"):
            return
        has_game = bool(int(self.current_game_id or 0) > 0)
        self.btn_add_game_type.setEnabled(has_game)
        selected = str(self.category_combo.currentData() or "")
        catalog = {n.casefold() for n in self._current_game_type_catalog()}
        can_delete = (
            has_game
            and selected not in ("", FILTER_CATEGORY_ALL, FILTER_ALL)
            and selected.casefold() in catalog
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
        try:
            created = get_db().add_game_category(gid, label)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "新增类型失败", str(exc))
            return
        if not created:
            QMessageBox.information(self, "新增类型", f"「{label}」已存在。")
        self._refresh_category_combo()
        self._apply_category_options_to_cards()

    def _on_delete_game_type(self) -> None:
        gid = int(self.current_game_id or 0)
        label = str(self.category_combo.currentData() or "").strip()
        if gid <= 0 or label in ("", FILTER_CATEGORY_ALL, FILTER_ALL):
            return
        confirm = QMessageBox.question(
            self,
            "删除类型",
            f"从当前游戏的类型目录中删除「{label}」？\n已标记该类型的 Mod 不会被改动。",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            get_db().delete_game_category(gid, label)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除类型失败", str(exc))
            return
        if self._category_filter.casefold() == label.casefold():
            self._category_filter = FILTER_CATEGORY_ALL
        self._refresh_category_combo()
        self._apply_category_options_to_cards()
        self._apply_view_filter()

    def _apply_category_options_to_cards(self) -> None:
        options = self._merged_category_options()
        for card in self._cards:
            card.set_category_options(options)

    def _refresh_category_combo(self, available: list[str] | None = None) -> None:
        if available is None:
            available = self._merged_category_options()
        else:
            available = self._merged_category_options(available)
        current = coerce_filter_selection(
            self._category_filter, available, all_key=FILTER_CATEGORY_ALL
        )
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("全部分类", FILTER_CATEGORY_ALL)
        for tag in available:
            self.category_combo.addItem(tag, tag)
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
        self._sync_type_manage_buttons()

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
            db_display_name=db_display if folder.is_dir() else "",
            db_steam_name=steam if folder.is_dir() else "",
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
        workspace_id = ""
        is_invalid = False
        conflict_status = "none"
        enabled = True
        category_tags = ""
        if db_fields is not None:
            platform = getattr(db_fields, "platform", "steam") or "steam"
            source_url = getattr(db_fields, "source_url", "") or ""
            external_id = getattr(db_fields, "external_id", "") or ""
            workspace_id = getattr(db_fields, "workspace_id", "") or ""
            is_invalid = bool(getattr(db_fields, "is_invalid", False))
            conflict_status = getattr(db_fields, "conflict_status", "none") or "none"
            enabled = bool(getattr(db_fields, "enabled", True))
            category_tags = getattr(db_fields, "category_tags", "") or ""
        # Prefer SQLite lifecycle flags; fall back to legacy tag flags
        invalid = is_invalid or (
            bool(getattr(tag_flags, "invalid", False)) if tag_flags else False
        )
        conflict = (conflict_status == "conflict") or (
            bool(getattr(tag_flags, "conflict", False)) if tag_flags else False
        )
        from services.platform_identity import resolve_display_platform

        meta_plat = ""
        if meta is not None:
            meta_plat = str(getattr(meta, "source_type", "") or "").strip()
        db_plat = ""
        if db_fields is not None:
            db_plat = str(getattr(db_fields, "platform", "") or "").strip()
        else:
            db_plat = str(platform or "").strip()
        platform = resolve_display_platform(
            db_platform=db_plat,
            metadata_platform=meta_plat,
        )
        if meta is not None and str(getattr(meta, "url", "") or "").strip():
            source_url = str(meta.url).strip()
        has_offline = False
        from services.mod_metadata_resolver import resolve_offline_page

        found = resolve_offline_page(mid or None, folder)
        if found is not None:
            has_offline = True
        else:
            off_ref = (
                str(getattr(meta, "offline_page_path", "") or "").strip() if meta else ""
            )
            if off_ref:
                try:
                    has_offline = Path(off_ref).is_file()
                except OSError:
                    has_offline = False
        content_status = ""
        source_type = ""
        try:
            from services.library_status import row_content_status, row_source_type

            if mid:
                brow = get_db().get_mod_backup_row(mid)
                if brow is not None:
                    content_status = row_content_status(brow)
                    sticky = row_source_type(brow)
                    if sticky and sticky != "unknown":
                        source_type = sticky
        except Exception:  # noqa: BLE001
            pass
        return ModFilterIndex(
            mod_id=mid,
            display_name=display,
            steam_name=steam,
            notes=notes,
            game_name=game_search or game_name,
            favorite=favorite,
            deployed=deployed,
            has_offline=has_offline,
            mtime=folder_mtime(folder),
            sort_name=display or steam or folder.name,
            invalid=invalid,
            conflict=conflict,
            tag_values=tag_values,
            platform=platform,
            source_url=source_url,
            external_id=external_id,
            workspace_id=workspace_id,
            is_invalid=is_invalid or invalid,
            conflict_status=conflict_status,
            enabled=enabled,
            category_tags=category_tags,
            content_status=content_status,
            source_type=source_type,
        )

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
                    for index, _card in self._card_entries
                    if index.deployed
                )
            )
        filter_sig = (
            query,
            self._status_filter,
            self._platform_filter,
            category_key,
            self._sort_mode,
            self._current_game_filter or "",
            len(self._card_entries),
            self._deployment_record_id,
            None if recorded_ids is None else tuple(sorted(recorded_ids)),
            deployed_fp,
        )
        if filter_sig == getattr(self, "_last_filter_sig", None):
            self._sync_record_overlays()
            return
        self._last_filter_sig = filter_sig
        self._sync_record_overlays()
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

            visible_cards = filter_and_sort(
                self._card_entries,
                query=query,
                filter_key=self._status_filter,
                platform_key=self._platform_filter,
                category_key=category_key,
                sort_mode=self._sort_mode,
                record_mod_ids=recorded_ids,
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
            status_line = str(getattr(self, "_pending_game_status_line", "") or "").strip()
            if self.current_game_name and status_line:
                self.count_label.setText(f"{len(visible_cards)} Mods  ·  {status_line}")
            self._refresh_game_header()
            self.library_layout.invalidate()
            self._sync_library_host_size()
            assert id(self.detail_panel) == detail_id
        finally:
            self.library_layout.setEnabled(True)
            self.library_host.setUpdatesEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            self._set_scroll_value(scroll)
        self._schedule_visible_covers()

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

    def _on_library_scroll_covers(self, *_args) -> None:
        self._schedule_visible_covers()

    def _schedule_visible_covers(self) -> None:
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
        visible = self.iter_viewport_cover_cards()
        visible_set = set(visible)
        submitted = 0
        for card in self._cards:
            if not isinstance(card, ModCardWidget) or card.isHidden():
                continue
            if card in visible_set:
                if card.ensure_cover():
                    submitted += 1
            else:
                card.cancel_pending_cover(keep_pixmap=True)
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
    # Selection
    # ------------------------------------------------------------------

    def on_mod_selected(self, mod_path: object) -> None:
        from PySide6.QtWidgets import QToolTip

        try:
            from ui.popup_trace import log_popup

            log_popup("slot:on_mod_selected", detail=str(mod_path))
        except Exception:  # noqa: BLE001
            pass
        # Kill floating tip / toast that Qt pops under the cursor on click.
        QToolTip.hideText()
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
            self.detail_panel.show_mod(
                card.managed_path,
                mod_id=card._mod_id() or None,
                game_id=int(self.current_game_id or 0),
                game_name=str(self.current_game_name or "").strip(),
            )
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
            card._card_data = None
            self._snapshot_dirty = True
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
                self._mark_card_stale(card)
                card.refresh_display()
        # refresh_display clears relative overlay; restore if Record Filter active.
        self._sync_record_overlays()
        # Keep multi-select panel state with updated platform label.
        if len(self._selected_cards) > 1:
            self._apply_multi_or_single_panel()
        elif self._selected_card is not None:
            self.detail_panel.show_mod(
                self._selected_card.managed_path,
                mod_id=self._selected_card._mod_id() or None,
                game_id=int(self.current_game_id or 0),
                game_name=str(self.current_game_name or "").strip(),
            )

    def _card_for_path(self, path: Path) -> ModCardWidget | None:
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
        from services.mod_metadata_resolver import resolve_mod_metadata

        mid = card._mod_id() if card is not None else ""
        resolved = resolve_mod_metadata(mid or None, path)
        meta = resolved.to_mod_metadata() if resolved is not None else None
        if card is None and meta is not None and meta.published_file_id:
            card = self._card_for_mod_id(str(meta.published_file_id))
        if card is not None:
            # Drop batched snapshot so content_status / missing badge re-read disk+DB.
            self._mark_card_stale(card)
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
                self.detail_panel.show_mod(
                    path,
                    mod_id=card._mod_id() or None,
                    game_id=int(self.current_game_id or 0),
                    game_name=str(self.current_game_name or "").strip(),
                )

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
        self._stop_deploy_watchdog()
        if getattr(self, "_deploy_ui_timed_out", False):
            logger.warning(
                "[DEPLOY_STALL] ignoring late worker result after UI timeout mod_id=%s",
                self._deploy_mod_id or "",
            )
            return
        data = result if isinstance(result, dict) else {"success": False, "error": str(result)}
        mid = self._deploy_mod_id or str(data.get("mod_id") or "")
        self.detail_panel.apply_deploy_result(data)
        # Status stays in DetailPanel — no modal dialogs.
        focus = mid if data.get("success") else ""
        self._refresh_mod_ui(mid, focus_mod_id=focus)

    def _on_deploy_failed(self, error: str) -> None:
        self._stop_deploy_watchdog()
        if getattr(self, "_deploy_ui_timed_out", False):
            return
        self.detail_panel.apply_deploy_failure(error)
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
        for i, (index, card) in enumerate(self._card_entries):
            if card._mod_id() != str(mod_id):
                continue
            self._mark_card_stale(card)
            if self._status_filter != FILTER_DEPLOYMENT_RECORD:
                card.clear_record_overlay()
            card.refresh_display()
            manager = ModFileManager(self._target_root)
            from services.mod_metadata_resolver import resolve_mod_metadata

            resolved = resolve_mod_metadata(mod_id, card.managed_path)
            meta = resolved.to_mod_metadata() if resolved is not None else card.metadata
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
                    self.detail_panel.show_mod(
                        card.managed_path,
                        mod_id=card._mod_id() or None,
                        game_id=int(self.current_game_id or 0),
                        game_name=str(self.current_game_name or "").strip(),
                    )
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
        snapshot=None,
    ) -> None:
        """
        Build the game sidebar from SQLite-configured games (primary),
        merged with any on-disk library folders not yet in the DB.

        Does **not** require Steam workshop sync / ``workshop_path``.
        """
        from core.sanitize import sanitize_folder_name

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
            from services.mod_metadata_resolver import list_visible_mods

            visible = list_visible_mods(manager.target_root, None)
            total = len(visible)
            self._add_game_list_item(ALL_GAMES_LABEL, total, key="", game_id=0)

            def _count_for(game_key: str) -> int:
                n = 0
                for item in visible:
                    path = Path(item.managed_path or "")
                    if path.parent.name == game_key or item.game_name == game_key:
                        n += 1
                return n

            # key (library folder) -> (display_name, app_id, mod_count, game_status)
            entries = {}
            from services.library_status import compute_game_status

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
                entries[folder] = (
                    display,
                    app_id,
                    _count_for(folder),
                    compute_game_status(manager.target_root, folder),
                )

            # Keep filesystem-only folders (legacy / empty dirs) that are not in DB.
            for name in manager.list_games():
                if name in entries:
                    continue
                entries[name] = (
                    name,
                    0,
                    _count_for(name),
                    compute_game_status(manager.target_root, name),
                )

            for item in visible:
                key = Path(item.managed_path or "").parent.name or item.game_name
                if key and key not in entries:
                    entries[key] = (
                        key,
                        int(item.app_id or 0),
                        _count_for(key),
                        compute_game_status(manager.target_root, key),
                    )

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
            from services.mod_metadata_resolver import list_visible_mods

            for item in list_visible_mods(
                manager.target_root, game_key
            ):
                mid = str(item.published_file_id or "")
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
        from services.ui_perf_log import PerfScope

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
        self._render_mod_cards(manager, force_reload=self._snapshot_dirty)
        self._snapshot_dirty = False
        perf.phase("layout")
        # Game switch: never keep the previous game's scroll offset / range.
        self._sync_library_host_size()
        self._set_scroll_value(0)
        self.filter_changed.emit(key or ALL_GAMES_LABEL)
        perf.end()

    def _render_mod_cards(
        self, manager: ModFileManager, *, force_reload: bool = True
    ) -> None:
        from services.mod_library_cache import (
            card_data_to_metadata,
            get_library_cache,
        )

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
            snapshot = get_library_cache().load_snapshot(
                manager.target_root, force=force_reload
            )
            self._library_snapshot = snapshot

        self._detach_active_cards()
        self._last_filter_sig = None
        game = self._current_game_filter
        rows = list(snapshot.cards)
        if game:
            rows = [c for c in rows if c.game_folder == game]

        if not rows:
            self._cards = []
            self._card_entries = []
            self._prune_stale_card_cache(set(), game)
            self._rebuild_platform_filter_bar([])
            self._refresh_category_combo()
            self._apply_category_options_to_cards()
            self._apply_view_filter()
            return

        created = 0
        reused = 0
        keep_keys: set[str] = set()
        cards: list[ModCardWidget] = []
        entries: list[tuple[ModFilterIndex, ModCardWidget]] = []

        for data in rows:
            folder = Path(data.managed_path)
            meta = card_data_to_metadata(data)
            mid = str(data.id or "")
            key = self._card_cache_key(folder, mod_id=mid)
            keep_keys.add(key)
            card = self._card_cache.get(key)
            if card is None:
                card = ModCardWidget(
                    folder,
                    metadata=meta,
                    parent=self.library_host,
                    card_data=data,
                )
                card.hide()
                self._connect_card_signals(card)
                self._card_cache[key] = card
                created += 1
            else:
                card.rebind(folder, meta, card_data=data)
                card.hide()
                reused += 1
            index = self._filter_index_from_card_data(data)
            cards.append(card)
            entries.append((index, card))

        game_cats = self._merged_category_options(
            collect_category_labels([index for index, _c in entries])
        )
        for card in cards:
            card.set_category_options(game_cats)

        self._cards = cards
        self._card_entries = entries
        self._card_create_count = created
        self._card_reuse_count = reused

        # Drop missing paths always; within the active game, drop renamed leftovers.
        # Other games' cached cards stay for reuse on switch.
        self._prune_stale_card_cache(keep_keys, game)

        self._rebuild_platform_filter_bar()
        self._refresh_category_combo()
        self._apply_view_filter()

    def _filter_index_from_card_data(self, data) -> ModFilterIndex:
        folder_name = Path(data.managed_path).name
        source_type = str(getattr(data, "source_type", "") or "")
        content_status = str(getattr(data, "content_status", "") or "")
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
            content_status=content_status,
            source_type=source_type,
        )

    @staticmethod
    def _card_cache_key(folder: Path, mod_id: str = "") -> str:
        mid = str(mod_id or "").strip()
        try:
            resolved = Path(folder).resolve()
            if resolved.is_dir():
                return str(resolved)
        except OSError:
            pass
        if mid.isdigit():
            return f"missing:{mid}"
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
            if key.startswith("missing:"):
                if key not in keep_keys:
                    self._drop_cache_key(key)
                continue
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
            self._set_library_status_filter(FILTER_ALL)
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
            self._mark_card_stale(card)
            card.refresh_display()
            manager = ModFileManager(self._target_root)
            from services.mod_metadata_resolver import resolve_mod_metadata

            resolved = resolve_mod_metadata(mid, card.managed_path)
            meta = resolved.to_mod_metadata() if resolved is not None else card.metadata
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
                self.detail_panel.show_mod(
                    card.managed_path,
                    mod_id=card._mod_id() or None,
                    game_id=int(self.current_game_id or 0),
                    game_name=str(self.current_game_name or "").strip(),
                )
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
        self._schedule_visible_covers()
