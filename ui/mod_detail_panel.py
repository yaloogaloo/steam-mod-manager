"""In-library Mod detail panel — view / edit modes (non-modal QWidget)."""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
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
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    RELATIONSHIP_ADDON,
    RELATIONSHIP_CONFLICT,
    RELATIONSHIP_DEPENDENCY,
    RELATIONSHIP_PATCH,
    TAG_TYPE_CONFLICT,
    TAG_TYPE_INVALID,
    ModDisplayInfo,
    get_db,
)
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_NONE,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    format_offline_provider,
    normalize_offline_status,
)
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
    ModStatus,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, ModFileManager
from services.mod_files import ModFileManager as ModFilesJsonManager
from ui.platform_labels import (
    format_external_id,
    format_mod_info_clipboard,
    format_platform_name,
    get_platform_metadata_labels,
)

COVER_W = 112
COVER_H = 84
OFFLINE_INDEX = "index.html"
OFFLINE_SNAPSHOT_DIR = "offline"

MODE_EMPTY = 0
MODE_VIEW = 1
MODE_EDIT = 2


def humanize_deploy_error(error: str) -> str:
    """Map deployer error strings to concise UI copy."""
    text = (error or "").strip()
    if not text:
        return "部署失败。"
    if text == "Target mod directory does not exist" or text.startswith(
        "Target mod directory does not exist"
    ):
        return "目标部署目录不存在"
    if text.startswith("Permission denied"):
        return "没有写入权限（Permission denied）"
    if "请先配置游戏部署目录" in text or (
        "部署目录" in text and "配置" in text
    ):
        return "请先配置游戏部署目录"
    if "源 Mod 目录不存在" in text or "源文件不存在" in text:
        return "Mod源文件不存在"
    return text


class ModDetailPanel(QWidget):
    """
    Reusable right-hand workspace panel for Mod Library.

    Created once; call ``show_mod`` / ``clear`` / ``enter_edit`` to update.
    Never opens QDialog; never triggers Steam archive downloads.
    Deploy work is requested via ``deploy_requested`` (library starts QThread).
    """

    metadata_saved = Signal(object)  # Path managed_path after successful save
    tags_saved = Signal(object)  # Path after user tags / conflicts saved
    deploy_requested = Signal(str)  # mod_id — library_view starts DeployWorker
    redeploy_requested = Signal(str)
    undeploy_requested = Signal(str)
    remove_requested = Signal(str)  # mod_id — library confirms then removes
    offline_page_updated = Signal(object)  # Path managed_path after offline download

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modDetailPanel")
        self.setMinimumWidth(360)
        self.setMaximumWidth(440)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        self._managed_path: Path | None = None
        self._library_root: Path | None = None
        self._metadata: ModMetadata | None = None
        self._display_info: ModDisplayInfo | None = None
        self._mode = MODE_EMPTY
        self._deploy_busy = False
        self._audit_hint = ""
        self._conflict_hint = ""
        self._tag_deploy_hint = ""
        self._peer_mods: list[tuple[str, str]] = []
        self._peer_candidates: list[dict] = []
        self._offline_worker = None
        self._current_platform = PLATFORM_STEAM
        self._source_url_value = ""

        self._build_ui()
        self.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_mod(self, managed_path: str | Path) -> None:
        """Load local metadata into view mode (no Steam I/O)."""
        self._managed_path = Path(managed_path)
        try:
            self._library_root = self._managed_path.parents[1]
        except IndexError:
            self._library_root = self._managed_path.parent

        files = ModFileManager(self._library_root)
        meta = files.load_metadata(self._managed_path)
        if meta is None:
            meta = ModMetadata(
                published_file_id=(
                    self._managed_path.name
                    if self._managed_path.name.isdigit()
                    else ""
                ),
                title=self._managed_path.name,
                managed_path=str(self._managed_path),
            )
        meta.managed_path = str(self._managed_path)
        files.enrich_title_from_db(meta)
        self._metadata = meta

        self._display_info = None
        mid = meta.published_file_id
        if str(mid).isdigit():
            try:
                self._display_info = get_db().get_mod_display_info(mid)
            except Exception:  # noqa: BLE001
                self._display_info = None

        self._stack.setCurrentWidget(self._view_page)
        self._mode = MODE_VIEW
        self._fill_view()
        self.setEnabled(True)

    def clear(self) -> None:
        """Empty / unselected state."""
        self._managed_path = None
        self._library_root = None
        self._metadata = None
        self._display_info = None
        self._mode = MODE_EMPTY
        self._deploy_busy = False
        self._stack.setCurrentWidget(self._empty_page)
        self.setEnabled(True)
        self.btn_deploy.setEnabled(False)
        self.btn_deploy.setText("部署")
        self.btn_redeploy.setEnabled(False)
        self.btn_undeploy.setEnabled(False)
        self.view_deploy.clear()
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self.view_deploy_type.clear()
        self.view_deploy_error.clear()
        self.view_deploy_audit.clear()
        self.view_deploy_conflict.clear()
        self._audit_hint = ""
        self._conflict_hint = ""
        self._tag_deploy_hint = ""
        self._peer_mods = []
        self._peer_candidates = []
        if hasattr(self, "view_tag_deploy_hint"):
            self.view_tag_deploy_hint.clear()
        if hasattr(self, "tag_conflict_list"):
            self.tag_conflict_list.clear()
        if hasattr(self, "tag_invalid_check"):
            self.tag_invalid_check.setChecked(False)
            self.tag_conflict_check.setChecked(False)
            self.tag_invalid_reason.clear()
        if hasattr(self, "status_reason_edit"):
            self._reset_status_widgets()
        if hasattr(self, "_rel_lists"):
            for lst in self._rel_lists.values():
                lst.clear()

    def enter_edit(self) -> None:
        if self._managed_path is None or self._metadata is None:
            return
        self._fill_edit_form()
        self._stack.setCurrentWidget(self._edit_page)
        self._mode = MODE_EDIT

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._empty_page = self._build_empty_page()
        self._view_page = self._build_view_page()
        self._edit_page = self._build_edit_page()
        self._stack.addWidget(self._empty_page)
        self._stack.addWidget(self._view_page)
        self._stack.addWidget(self._edit_page)

        self.setStyleSheet(_PANEL_STYLE)

    def _build_empty_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("detailPanelInner")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 24, 20, 24)
        label = QLabel("选择一个 Mod\n在右侧查看详情")
        label.setObjectName("detailEmptyHint")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _build_view_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._view_scroll = QScrollArea()
        self._view_scroll.setWidgetResizable(True)
        self._view_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._view_scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QFrame()
        body.setObjectName("detailPanelInner")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        # --- Header ---
        header = QFrame()
        header.setObjectName("detailSection")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 10, 10, 10)
        header_layout.setSpacing(12)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_W, COVER_H)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.cover_label, alignment=Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        header_caption = QLabel("概览")
        header_caption.setObjectName("detailPanelSection")
        title_col.addWidget(header_caption)
        self.view_title = QLabel()
        self.view_title.setObjectName("detailPanelTitle")
        self.view_title.setWordWrap(True)
        self.view_title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.view_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        title_col.addWidget(self.view_title)
        title_col.addStretch(1)
        header_layout.addLayout(title_col, stretch=1)
        layout.addWidget(header)

        # --- Section: Basic Info (under Header) ---
        basic = self._make_section("基本信息")
        basic_body = basic.layout()
        assert isinstance(basic_body, QVBoxLayout)

        self._name_value = ""
        self._id_value = ""
        self._source_url_value = ""

        self.view_name_caption = QLabel()
        self.view_steam = QLabel()  # primary name value (legacy attr name)
        self.view_steam.setObjectName("detailPanelMeta")
        self.view_steam.setWordWrap(True)
        self.view_steam.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.btn_copy_name = QPushButton("复制")
        basic_body.addWidget(
            self._make_copy_field_row(
                self.view_name_caption, self.view_steam, self.btn_copy_name
            )
        )

        self.view_id_caption = QLabel()
        self.view_id = QLabel()
        self.view_id.setObjectName("detailPanelMeta")
        self.view_id.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.btn_copy_id = QPushButton("复制")
        basic_body.addWidget(
            self._make_copy_field_row(
                self.view_id_caption, self.view_id, self.btn_copy_id
            )
        )

        self.view_platform = QLabel()
        self.view_platform.setObjectName("detailPanelMeta")
        basic_body.addWidget(self.view_platform)

        self.view_source_caption = QLabel("来源：")
        self.view_source_caption.setObjectName("detailPanelMeta")
        self.view_source_url = QLabel()
        self.view_source_url.setObjectName("detailPanelMeta")
        self.view_source_url.setWordWrap(True)
        self.view_source_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.view_source_url.setOpenExternalLinks(True)
        self.btn_copy_source_url = QPushButton("复制链接")
        basic_body.addWidget(
            self._make_copy_field_row(
                self.view_source_caption,
                self.view_source_url,
                self.btn_copy_source_url,
                stacked=True,
            )
        )

        # Kept for backward-compatible attribute access in tests; hidden.
        self.view_external_id = QLabel()
        self.view_external_id.setObjectName("detailPanelMeta")
        self.view_external_id.hide()
        basic_body.addWidget(self.view_external_id)

        basic_body.addWidget(self._field_caption("Mod Files"))
        self.mod_files_host = QWidget()
        self.mod_files_layout = QVBoxLayout(self.mod_files_host)
        self.mod_files_layout.setContentsMargins(0, 0, 0, 0)
        self.mod_files_layout.setSpacing(4)
        self.view_mod_files = QLabel("（单文件 / 未登记多文件包）")
        self.view_mod_files.setObjectName("detailPanelBody")
        self.view_mod_files.setWordWrap(True)
        self.mod_files_layout.addWidget(self.view_mod_files)
        basic_body.addWidget(self.mod_files_host)

        self.view_game = QLabel()
        self.view_game.setObjectName("detailPanelMeta")
        self.view_game.setWordWrap(True)
        basic_body.addWidget(self.view_game)
        layout.addWidget(basic)

        # --- Status (offline only) ---
        status = self._make_section("状态")
        status_body = status.layout()
        assert isinstance(status_body, QVBoxLayout)

        self.view_offline = QLabel()
        self.view_offline.setObjectName("detailPanelMeta")
        self.view_offline.setWordWrap(True)
        status_body.addWidget(self.view_offline)
        layout.addWidget(status)

        # --- Deploy status ---
        deploy_sec = self._make_section("部署状态")
        deploy_body = deploy_sec.layout()
        assert isinstance(deploy_body, QVBoxLayout)

        self.view_deploy = QLabel()
        self.view_deploy.setObjectName("detailPanelMeta")
        self.view_deploy.setWordWrap(True)
        self.view_deploy.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        deploy_body.addWidget(self.view_deploy)

        self.view_deploy_time = QLabel()
        self.view_deploy_time.setObjectName("detailPanelMeta")
        self.view_deploy_time.setWordWrap(True)
        deploy_body.addWidget(self.view_deploy_time)

        self.view_deploy_path = QLabel()
        self.view_deploy_path.setObjectName("detailPanelMeta")
        self.view_deploy_path.setWordWrap(True)
        self.view_deploy_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        deploy_body.addWidget(self.view_deploy_path)

        self.view_deploy_type = QLabel()
        self.view_deploy_type.setObjectName("detailPanelMeta")
        self.view_deploy_type.setWordWrap(True)
        deploy_body.addWidget(self.view_deploy_type)

        self.view_deploy_error = QLabel()
        self.view_deploy_error.setObjectName("detailPanelMeta")
        self.view_deploy_error.setWordWrap(True)
        self.view_deploy_error.setStyleSheet("color: #e07070;")
        deploy_body.addWidget(self.view_deploy_error)

        self.view_deploy_audit = QLabel()
        self.view_deploy_audit.setObjectName("detailPanelMeta")
        self.view_deploy_audit.setWordWrap(True)
        self.view_deploy_audit.setStyleSheet("color: #c9a227;")
        deploy_body.addWidget(self.view_deploy_audit)

        self.view_deploy_conflict = QLabel()
        self.view_deploy_conflict.setObjectName("detailPanelMeta")
        self.view_deploy_conflict.setWordWrap(True)
        self.view_deploy_conflict.setStyleSheet("color: #c9a227;")
        deploy_body.addWidget(self.view_deploy_conflict)
        layout.addWidget(deploy_sec)

        # --- Section: User Metadata ---
        user_meta = self._make_section("用户元数据")
        user_body = user_meta.layout()
        assert isinstance(user_body, QVBoxLayout)

        self.view_favorite = QLabel()
        self.view_favorite.setObjectName("detailPanelMeta")
        user_body.addWidget(self.view_favorite)

        user_body.addWidget(self._field_caption("自定义介绍"))
        self.view_custom_desc = QLabel()
        self.view_custom_desc.setObjectName("detailPanelBody")
        self.view_custom_desc.setWordWrap(True)
        self.view_custom_desc.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        user_body.addWidget(self.view_custom_desc)

        user_body.addWidget(self._field_caption("备注"))
        self.view_notes = QLabel()
        self.view_notes.setObjectName("detailPanelBody")
        self.view_notes.setWordWrap(True)
        self.view_notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        user_body.addWidget(self.view_notes)
        layout.addWidget(user_meta)

        # --- Section: Lifecycle status (inline — no dialog) ---
        status_sec = self._make_section("状态")
        status_body = status_sec.layout()
        assert isinstance(status_body, QVBoxLayout)

        self.status_run_label = QLabel("运行状态：—")
        self.status_run_label.setObjectName("detailPanelBody")
        status_body.addWidget(self.status_run_label)

        self.status_enabled_label = QLabel("Enabled：—")
        self.status_enabled_label.setObjectName("detailPanelBody")
        status_body.addWidget(self.status_enabled_label)

        self.status_invalid_label = QLabel("失效：—")
        self.status_invalid_label.setObjectName("detailPanelBody")
        status_body.addWidget(self.status_invalid_label)

        self.status_conflict_label = QLabel("冲突：—")
        self.status_conflict_label.setObjectName("detailPanelBody")
        status_body.addWidget(self.status_conflict_label)

        self.status_version_label = QLabel("Version：—")
        self.status_version_label.setObjectName("detailPanelBody")
        self.status_version_label.setWordWrap(True)
        status_body.addWidget(self.status_version_label)

        self.status_installed_label = QLabel("Installed：—")
        self.status_installed_label.setObjectName("detailPanelBody")
        status_body.addWidget(self.status_installed_label)

        self.status_update_label = QLabel("Status：—")
        self.status_update_label.setObjectName("detailPanelMeta")
        status_body.addWidget(self.status_update_label)

        status_body.addWidget(self._field_caption("原因"))
        self.status_reason_edit = QLineEdit()
        self.status_reason_edit.setPlaceholderText("失效原因或冲突备注")
        status_body.addWidget(self.status_reason_edit)

        self.status_check_time_label = QLabel("最后检测：—")
        self.status_check_time_label.setObjectName("detailPanelMeta")
        status_body.addWidget(self.status_check_time_label)

        status_btns = QHBoxLayout()
        status_btns.setSpacing(6)
        self.btn_enable_mod = QPushButton("Enable")
        self.btn_disable_mod = QPushButton("Disable Mod")
        self.btn_mark_invalid = QPushButton("标记失效")
        self.btn_mark_valid = QPushButton("标记正常")
        self.btn_mark_conflict = QPushButton("标记冲突")
        self.btn_clear_conflict = QPushButton("清除冲突")
        for btn in (
            self.btn_enable_mod,
            self.btn_disable_mod,
            self.btn_mark_invalid,
            self.btn_mark_valid,
            self.btn_mark_conflict,
            self.btn_clear_conflict,
        ):
            btn.setObjectName("panelActionButton")
            status_btns.addWidget(btn)
        status_btns.addStretch(1)
        status_body.addLayout(status_btns)

        self.btn_view_conflicts = QPushButton("冲突详情")
        self.btn_view_conflicts.setObjectName("panelActionButton")
        self.btn_view_conflicts.setCheckable(True)
        status_body.addWidget(self.btn_view_conflicts)

        self.status_conflict_detail = QLabel()
        self.status_conflict_detail.setObjectName("detailPanelMeta")
        self.status_conflict_detail.setWordWrap(True)
        self.status_conflict_detail.setStyleSheet("color: #e06c75;")
        self.status_conflict_detail.hide()
        status_body.addWidget(self.status_conflict_detail)

        self.btn_enable_mod.clicked.connect(self._on_enable_mod)
        self.btn_disable_mod.clicked.connect(self._on_disable_mod)
        self.btn_mark_invalid.clicked.connect(self._on_mark_invalid)
        self.btn_mark_valid.clicked.connect(self._on_mark_valid)
        self.btn_mark_conflict.clicked.connect(self._on_mark_conflict)
        self.btn_clear_conflict.clicked.connect(self._on_clear_conflict)
        self.btn_view_conflicts.toggled.connect(self._on_toggle_conflict_detail)
        layout.addWidget(status_sec)

        # --- Category Tags ---
        cat_sec = self._make_section("Tags")
        cat_body = cat_sec.layout()
        assert isinstance(cat_body, QVBoxLayout)
        self.category_tags_label = QLabel("（无标签）")
        self.category_tags_label.setObjectName("detailPanelBody")
        self.category_tags_label.setWordWrap(True)
        cat_body.addWidget(self.category_tags_label)
        cat_row = QHBoxLayout()
        self.category_tag_edit = QLineEdit()
        self.category_tag_edit.setPlaceholderText("Gameplay / Fix / Graphics …")
        self.btn_add_category_tag = QPushButton("+ Add Tag")
        self.btn_add_category_tag.setObjectName("panelActionButton")
        self.btn_remove_category_tag = QPushButton("Remove Tag")
        self.btn_remove_category_tag.setObjectName("panelActionButton")
        cat_row.addWidget(self.category_tag_edit, stretch=1)
        cat_row.addWidget(self.btn_add_category_tag)
        cat_row.addWidget(self.btn_remove_category_tag)
        cat_body.addLayout(cat_row)
        self.btn_add_category_tag.clicked.connect(self._on_add_category_tag)
        self.btn_remove_category_tag.clicked.connect(self._on_remove_category_tag)
        layout.addWidget(cat_sec)

        # --- Relationships (user-declared only) ---
        rel_sec = self._make_section("Relationships")
        rel_body = rel_sec.layout()
        assert isinstance(rel_body, QVBoxLayout)

        self._rel_lists: dict[str, QListWidget] = {}
        self._rel_add_buttons: dict[str, QPushButton] = {}
        for key, caption, add_label, rtype in (
            ("dependencies", "Dependencies", "+ Add Mod", RELATIONSHIP_DEPENDENCY),
            ("conflicts", "Conflicts", "+ Add Conflict", RELATIONSHIP_CONFLICT),
            ("addons", "Addons", "+ Add Extension", RELATIONSHIP_ADDON),
            ("patches", "Patches", "+ Add Patch", RELATIONSHIP_PATCH),
        ):
            rel_body.addWidget(self._field_caption(caption))
            lst = QListWidget()
            lst.setMinimumHeight(48)
            lst.setMaximumHeight(90)
            lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self._rel_lists[key] = lst
            rel_body.addWidget(lst)
            row = QHBoxLayout()
            add_btn = QPushButton(add_label)
            add_btn.setObjectName("panelActionButton")
            rem_btn = QPushButton("Remove")
            rem_btn.setObjectName("panelActionButton")
            add_btn.clicked.connect(
                lambda _=False, t=rtype: self._on_add_relationship(t)
            )
            rem_btn.clicked.connect(
                lambda _=False, k=key: self._on_remove_relationship(k)
            )
            row.addWidget(add_btn)
            row.addWidget(rem_btn)
            row.addStretch(1)
            rel_body.addLayout(row)
            self._rel_add_buttons[key] = add_btn

        layout.addWidget(rel_sec)

        # --- Section: User Tags (relation targets; legacy + peer conflicts) ---
        tags_sec = self._make_section("用户标记")
        tags_body = tags_sec.layout()
        assert isinstance(tags_body, QVBoxLayout)

        self.tag_invalid_check = QCheckBox("已失效（标签）")
        tags_body.addWidget(self.tag_invalid_check)

        tags_body.addWidget(self._field_caption("失效原因（标签）"))
        self.tag_invalid_reason = QLineEdit()
        self.tag_invalid_reason.setPlaceholderText("可选：说明失效原因")
        tags_body.addWidget(self.tag_invalid_reason)

        self.tag_conflict_check = QCheckBox("存在冲突（关联）")
        tags_body.addWidget(self.tag_conflict_check)

        tags_body.addWidget(self._field_caption("冲突 Mod（勾选其它 Mod）"))
        self.tag_conflict_list = QListWidget()
        self.tag_conflict_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.tag_conflict_list.setMinimumHeight(100)
        self.tag_conflict_list.setMaximumHeight(160)
        tags_body.addWidget(self.tag_conflict_list)

        self.btn_save_tags = QPushButton("保存标记")
        self.btn_save_tags.setObjectName("panelPrimaryButton")
        tags_body.addWidget(self.btn_save_tags)

        self.view_tag_deploy_hint = QLabel()
        self.view_tag_deploy_hint.setObjectName("detailPanelMeta")
        self.view_tag_deploy_hint.setWordWrap(True)
        self.view_tag_deploy_hint.setStyleSheet("color: #c9a227;")
        tags_body.addWidget(self.view_tag_deploy_hint)

        self.tag_invalid_check.toggled.connect(self._on_tag_invalid_toggled)
        self.tag_conflict_check.toggled.connect(self._on_tag_conflict_toggled)
        self.btn_save_tags.clicked.connect(self._save_user_tags)
        layout.addWidget(tags_sec)

        layout.addStretch(1)
        self._view_scroll.setWidget(body)
        outer.addWidget(self._view_scroll, stretch=1)

        # --- Actions (fixed footer) ---
        self._view_footer = QFrame()
        self._view_footer.setObjectName("detailFooter")
        actions = QVBoxLayout(self._view_footer)
        actions.setContentsMargins(12, 10, 12, 12)
        actions.setSpacing(8)
        actions_caption = QLabel("操作")
        actions_caption.setObjectName("detailPanelSection")
        actions.addWidget(actions_caption)

        # Open / offline actions: two rows so labels never compress sideways.
        link_row1 = QHBoxLayout()
        link_row1.setSpacing(8)
        link_row2 = QHBoxLayout()
        link_row2.setSpacing(8)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        row4 = QHBoxLayout()
        row4.setSpacing(8)
        self.btn_folder = QPushButton("打开目录")
        self.btn_steam = QPushButton("原链接")
        self.btn_download_offline = QPushButton("保存离线页面")
        self.btn_offline = QPushButton("打开离线页面")
        self.btn_deploy = QPushButton("Deploy")
        self.btn_redeploy = QPushButton("重新部署")
        self.btn_undeploy = QPushButton("取消部署")
        self.btn_copy_link = QPushButton("复制链接")
        self.btn_copy_info = QPushButton("复制全部信息")
        self.btn_edit = QPushButton("编辑")
        self.btn_remove_mod = QPushButton("Remove Mod")
        for btn in (self.btn_folder, self.btn_steam):
            btn.setObjectName("panelActionButton")
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            btn.setMinimumWidth(max(96, btn.sizeHint().width()))
            link_row1.addWidget(btn)
        link_row1.addStretch(1)
        for btn in (self.btn_download_offline, self.btn_offline):
            btn.setObjectName("panelActionButton")
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            btn.setMinimumWidth(max(120, btn.sizeHint().width()))
            link_row2.addWidget(btn)
        link_row2.addStretch(1)
        for btn in (self.btn_deploy, self.btn_redeploy, self.btn_undeploy):
            btn.setObjectName("panelActionButton")
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            row2.addWidget(btn)
        row2.addStretch(1)
        for btn in (self.btn_copy_link, self.btn_copy_info):
            btn.setObjectName("panelActionButton")
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            row4.addWidget(btn)
        row4.addStretch(1)
        self.btn_edit.setObjectName("panelPrimaryButton")
        self.btn_edit.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.btn_remove_mod.setObjectName("panelActionButton")
        self.btn_remove_mod.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.btn_remove_mod.setStyleSheet("color: #e06c75;")
        row3.addWidget(self.btn_edit)
        row3.addWidget(self.btn_remove_mod)
        row3.addStretch(1)
        actions.addLayout(link_row1)
        actions.addLayout(link_row2)
        actions.addLayout(row2)
        actions.addLayout(row4)
        actions.addLayout(row3)
        outer.addWidget(self._view_footer)

        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_offline.clicked.connect(self._open_offline)
        self.btn_download_offline.clicked.connect(self._download_offline_page)
        self.btn_steam.clicked.connect(self._open_steam)
        self.btn_copy_name.clicked.connect(self._copy_name)
        self.btn_copy_id.clicked.connect(self._copy_id)
        self.btn_copy_source_url.clicked.connect(self._copy_source_url)
        self.btn_copy_link.clicked.connect(self._copy_source_url)
        self.btn_copy_info.clicked.connect(self._copy_mod_info)
        self.btn_deploy.clicked.connect(self._request_deploy)
        self.btn_redeploy.clicked.connect(self._request_redeploy)
        self.btn_undeploy.clicked.connect(self._request_undeploy)
        self.btn_edit.clicked.connect(self.enter_edit)
        self.btn_remove_mod.clicked.connect(self._request_remove)
        return page

    def _make_copy_field_row(
        self,
        caption: QLabel,
        value: QLabel,
        button: QPushButton,
        *,
        stacked: bool = False,
    ) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        caption.setObjectName("detailPanelMeta")
        col.addWidget(caption)
        col.addWidget(value)
        row.addLayout(col, stretch=1)
        button.setObjectName("panelActionButton")
        button.setFixedWidth(64 if button.text() == "复制" else 72)
        row.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
        return host

    def _build_edit_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QFrame()
        body.setObjectName("detailPanelInner")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("编辑 Mod 信息")
        heading.setObjectName("detailPanelTitle")
        layout.addWidget(heading)

        self.edit_hint = QLabel()
        self.edit_hint.setObjectName("detailPanelMeta")
        self.edit_hint.setWordWrap(True)
        layout.addWidget(self.edit_hint)

        layout.addWidget(self._section_label("显示名称"))
        self.edit_display_name = QLineEdit()
        self.edit_display_name.setPlaceholderText("留空则使用 Steam 原名")
        layout.addWidget(self.edit_display_name)

        layout.addWidget(self._section_label("自定义介绍"))
        self.edit_custom_desc = QTextEdit()
        self.edit_custom_desc.setAcceptRichText(False)
        self.edit_custom_desc.setPlaceholderText("可选")
        self.edit_custom_desc.setMinimumHeight(80)
        layout.addWidget(self.edit_custom_desc)

        layout.addWidget(self._section_label("备注"))
        self.edit_notes = QTextEdit()
        self.edit_notes.setAcceptRichText(False)
        self.edit_notes.setPlaceholderText("个人备注…")
        self.edit_notes.setMinimumHeight(80)
        layout.addWidget(self.edit_notes)

        self.edit_favorite = QCheckBox("收藏")
        layout.addWidget(self.edit_favorite)

        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        footer = QFrame()
        footer.setObjectName("detailFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(12, 10, 12, 12)
        buttons.setSpacing(8)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("panelPrimaryButton")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("panelActionButton")
        buttons.addStretch(1)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_save)
        outer.addWidget(footer)

        self.btn_save.clicked.connect(self._save_edit)
        self.btn_cancel.clicked.connect(self._cancel_edit)
        return page

    def _make_section(self, title: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("detailSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(self._section_label(title))
        return frame

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailPanelSection")
        return label

    @staticmethod
    def _field_caption(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailPanelField")
        return label

    # ------------------------------------------------------------------
    # Fill / actions
    # ------------------------------------------------------------------

    def _fill_view(self) -> None:
        meta = self._metadata
        info = self._display_info
        assert meta is not None

        shown = info.display_name if info else meta.display_name
        steam = info.steam_name if info else (meta.title or "").strip()

        self.view_title.setText(shown)
        self.view_title.setToolTip(shown)

        platform = PLATFORM_STEAM
        source_url = ""
        external_id = ""
        files_bundle = None
        if info is not None:
            platform = info.platform or PLATFORM_STEAM
            source_url = (info.source_url or "").strip()
            external_id = (info.external_id or "").strip()
            files_bundle = info.mod_files
        if not source_url and platform == PLATFORM_STEAM and meta.published_file_id:
            source_url = meta.workshop_url

        labels = get_platform_metadata_labels(platform)
        self._current_platform = str(platform or PLATFORM_STEAM).strip().lower()

        name_value = (shown or steam or "").strip()
        self._name_value = name_value
        self.view_name_caption.setText(f"{labels.name}：")
        self.view_steam.setText(name_value or "—")
        self.view_steam.setToolTip(name_value or "")
        self.btn_copy_name.setEnabled(bool(name_value))

        id_value = format_external_id(
            platform,
            external_id,
            source_url=source_url,
            published_file_id=str(meta.published_file_id or ""),
        )
        self._id_value = id_value
        self.view_id_caption.setText(f"{labels.external_id}：")
        self.view_id.setText(id_value or "—")
        self.view_external_id.setText(f"外部 ID：{id_value or '—'}")
        self.btn_copy_id.setEnabled(bool(id_value))

        self.view_platform.setText(f"{labels.platform}：{format_platform_name(platform)}")
        self._source_url_value = source_url
        self.view_source_caption.setText(f"{labels.source}：")
        if source_url:
            safe = source_url.replace("&", "&amp;")
            self.view_source_url.setText(f'<a href="{safe}">{safe}</a>')
            self.btn_copy_source_url.setEnabled(True)
            self.btn_copy_link.setEnabled(True)
        else:
            self.view_source_url.setText("—")
            self.btn_copy_source_url.setEnabled(False)
            self.btn_copy_link.setEnabled(False)

        self._fill_mod_files_list(files_bundle)

        game = ""
        if self._library_root is not None:
            game = ModFileManager(self._library_root).game_name_for_path(
                self._managed_path  # type: ignore[arg-type]
            )
        game = meta.game_name or game or "—"
        app = f" · AppID {meta.app_id}" if meta.app_id else ""
        self.view_game.setText(f"游戏：{game}{app}")

        self._refresh_offline_status_label()
        self._update_offline_download_button()

        if not self._deploy_busy:
            self._fill_deploy_status_from_db()

        fav = bool(info.favorite) if info else False
        self.view_favorite.setText("收藏：是 ★" if fav else "收藏：否")

        custom = (info.custom_description if info else "").strip()
        self.view_custom_desc.setText(custom or "（无）")

        notes = (info.user_notes if info else "").strip() or (
            meta.custom_notes or ""
        ).strip()
        self.view_notes.setText(notes or "（无）")

        self._fill_lifecycle_status()
        self._fill_relationships()
        self._fill_user_tags()
        mid = self.current_mod_id()
        if mid:
            self._fill_category_tags(mid)
        else:
            self.category_tags_label.setText("（无标签）")
        self.view_tag_deploy_hint.setText(self._tag_deploy_hint)

        cover = None
        if self._library_root is not None and self._managed_path is not None:
            cover = ModFileManager(self._library_root).find_local_cover(
                self._managed_path
            )
        self._set_cover(cover)

    def _clear_mod_files_widgets(self) -> None:
        while self.mod_files_layout.count():
            item = self.mod_files_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _fill_mod_files_list(self, files_bundle) -> None:
        self._clear_mod_files_widgets()
        files = list(files_bundle.files) if files_bundle else []
        if not files:
            self.view_mod_files = QLabel("（单文件 / 未登记多文件包）")
            self.view_mod_files.setObjectName("detailPanelBody")
            self.view_mod_files.setWordWrap(True)
            self.mod_files_layout.addWidget(self.view_mod_files)
            return
        enabled_n = sum(1 for f in files if f.enabled)
        summary = QLabel(f"文件：{len(files)} 个 · 启用：{enabled_n}/{len(files)}")
        summary.setObjectName("detailPanelMeta")
        self.mod_files_layout.addWidget(summary)
        for entry in files:
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            label = f"{entry.name or entry.filename}"
            cb = QCheckBox(label)
            cb.blockSignals(True)
            cb.setChecked(bool(entry.enabled))
            cb.blockSignals(False)
            cb.setProperty("file_id", entry.id)
            tip = f"{entry.filename or entry.path} [{entry.type}]"
            cb.setToolTip(tip)
            sub = QLabel(entry.filename or entry.path or "")
            sub.setObjectName("detailPanelMeta")
            sub.setStyleSheet("color: #8b9bb0; font-size: 11px;")
            cb.toggled.connect(
                lambda checked, fid=entry.id: self._on_mod_file_toggled(fid, checked)
            )
            row_layout.addWidget(cb)
            row_layout.addWidget(sub)
            self.mod_files_layout.addWidget(row)

    def _on_mod_file_toggled(self, file_id: str, checked: bool) -> None:
        mid = self.current_mod_id()
        if not mid or not file_id:
            return
        try:
            ModFilesJsonManager(get_db()).set_file_enabled(mid, file_id, checked)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新文件失败", str(exc))
            self._fill_view()
            return
        try:
            self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            pass
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def set_peer_mods(self, peers) -> None:
        """
        Other library Mods for pickers.

        Accepts ``list[tuple[mod_id, name]]`` or ``list[dict]`` with
        ``mod_id`` / ``title`` / ``platform`` / ``game_name``.
        """
        self._peer_mods = []
        self._peer_candidates = []
        for entry in peers or []:
            if isinstance(entry, dict):
                mid = str(entry.get("mod_id") or "").strip()
                if not mid.isdigit():
                    continue
                title = str(entry.get("title") or mid)
                self._peer_mods.append((mid, title))
                self._peer_candidates.append(
                    {
                        "mod_id": mid,
                        "title": title,
                        "platform": str(entry.get("platform") or "steam"),
                        "game_name": str(entry.get("game_name") or ""),
                    }
                )
            else:
                mid = str(entry[0]).strip()
                if not mid.isdigit():
                    continue
                title = str(entry[1] if len(entry) > 1 else mid)
                self._peer_mods.append((mid, title))
                self._peer_candidates.append(
                    {
                        "mod_id": mid,
                        "title": title,
                        "platform": "steam",
                        "game_name": "",
                    }
                )
        if self._mode == MODE_VIEW and self._metadata is not None:
            selected = self._selected_conflict_ids()
            self._rebuild_conflict_list(preselect=selected)

    def _fill_relationships(self) -> None:
        if not hasattr(self, "_rel_lists"):
            return
        for lst in self._rel_lists.values():
            lst.clear()
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        try:
            grouped = get_db().get_mod_relationships(mid)
        except Exception:  # noqa: BLE001
            return
        for key, items in grouped.items():
            lst = self._rel_lists.get(key)
            if lst is None:
                continue
            for item in items:
                title = str(item.get("title") or item.get("mod_id") or "")
                tid = str(item.get("mod_id") or "")
                label = title if title else tid
                row = QListWidgetItem(label)
                row.setData(Qt.ItemDataRole.UserRole, int(item.get("id") or 0))
                row.setToolTip(f"ID {tid}")
                lst.addItem(row)

    def _on_add_relationship(self, relationship_type: str) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        from ui.mod_picker_dialog import ModPickerDialog

        candidates = [
            c
            for c in self._peer_candidates
            if str(c.get("mod_id")) != mid
        ]
        dlg = ModPickerDialog(
            candidates,
            title=f"Add {relationship_type}",
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        target = dlg.selected_mod_id()
        if not target:
            return
        try:
            get_db().add_mod_relationship(mid, target, relationship_type)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "添加关系失败", str(exc))
            return
        self._fill_relationships()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_remove_relationship(self, group_key: str) -> None:
        lst = self._rel_lists.get(group_key)
        if lst is None:
            return
        item = lst.currentItem()
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if not rid:
            return
        try:
            get_db().remove_mod_relationship(int(rid))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "删除关系失败", str(exc))
            return
        self._fill_relationships()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def set_tag_deploy_hint(self, text: str) -> None:
        """Show pre-deploy invalid/conflict warning (hint only)."""
        self._tag_deploy_hint = str(text or "").strip()
        if hasattr(self, "view_tag_deploy_hint"):
            self.view_tag_deploy_hint.setText(self._tag_deploy_hint)

    def _reset_status_widgets(self) -> None:
        self.status_run_label.setText("运行状态：—")
        if hasattr(self, "status_enabled_label"):
            self.status_enabled_label.setText("Enabled：—")
        self.status_invalid_label.setText("失效：—")
        self.status_conflict_label.setText("冲突：—")
        if hasattr(self, "status_version_label"):
            self.status_version_label.setText("Version：—")
            self.status_installed_label.setText("Installed：—")
            self.status_update_label.setText("Status：—")
        self.status_reason_edit.clear()
        self.status_check_time_label.setText("最后检测：—")
        self.btn_view_conflicts.blockSignals(True)
        self.btn_view_conflicts.setChecked(False)
        self.btn_view_conflicts.blockSignals(False)
        self.status_conflict_detail.clear()
        self.status_conflict_detail.hide()
        if hasattr(self, "category_tags_label"):
            self.category_tags_label.setText("（无标签）")
            self.category_tag_edit.clear()

    def _fill_lifecycle_status(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            self._reset_status_widgets()
            return
        try:
            st = get_db().get_mod_status(mid)
        except Exception:  # noqa: BLE001
            st = ModStatus()
        self._apply_status_to_widgets(st)
        self._fill_version_and_enabled(mid)
        self._fill_category_tags(mid)

    def _fill_version_and_enabled(self, mid: str) -> None:
        try:
            enabled = get_db().is_mod_enabled(mid)
        except Exception:  # noqa: BLE001
            enabled = True
        self.status_enabled_label.setText(
            f"Enabled：{'✓' if enabled else 'Disabled'}"
        )
        self.btn_enable_mod.setEnabled(not enabled)
        self.btn_disable_mod.setEnabled(enabled)
        try:
            ver = get_db().get_mod_version(mid)
        except Exception:  # noqa: BLE001
            from core.db_manager import ModVersionInfo

            ver = ModVersionInfo(mod_id=mid)
        self.status_version_label.setText(f"Version：{ver.mod_version or '—'}")
        self.status_installed_label.setText(
            f"Installed：{ver.installed_version or '—'}"
        )
        self.status_update_label.setText(f"Status：{ver.status_label}")

    def _fill_category_tags(self, mid: str) -> None:
        try:
            tags = get_db().get_category_tags(mid)
        except Exception:  # noqa: BLE001
            tags = []
        if tags:
            self.category_tags_label.setText("  ".join(f"[{t}]" for t in tags))
        else:
            self.category_tags_label.setText("（无标签）")

    def _on_enable_mod(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        get_db().enable_mod(mid)
        self._fill_lifecycle_status()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_disable_mod(self) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        get_db().disable_mod(mid)
        self._fill_lifecycle_status()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_add_category_tag(self) -> None:
        mid = self.current_mod_id()
        tag = self.category_tag_edit.text().strip()
        if not mid or not mid.isdigit() or not tag:
            return
        try:
            get_db().add_category_tag(mid, tag)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "添加标签失败", str(exc))
            return
        self.category_tag_edit.clear()
        self._fill_category_tags(mid)
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_remove_category_tag(self) -> None:
        mid = self.current_mod_id()
        tag = self.category_tag_edit.text().strip()
        if not mid or not mid.isdigit() or not tag:
            return
        get_db().remove_category_tag(mid, tag)
        self.category_tag_edit.clear()
        self._fill_category_tags(mid)
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _request_remove(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        reply = QMessageBox.question(
            self,
            "Remove Mod",
            "该操作会删除：\n\n"
            "- Mod 文件\n"
            "- 部署状态\n\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.remove_requested.emit(mid)

    def _apply_status_to_widgets(self, st: ModStatus) -> None:
        self.status_run_label.setText(f"运行状态：{st.run_label}")
        self.status_invalid_label.setText(f"失效：{'是' if st.invalid else '否'}")
        conflict_map = {
            CONFLICT_STATUS_NONE: "无",
            CONFLICT_STATUS_WARNING: "警告",
            CONFLICT_STATUS_CONFLICT: "冲突",
        }
        self.status_conflict_label.setText(
            f"冲突：{conflict_map.get(st.conflict_status, st.conflict_status)}"
        )
        # Prefer invalid_reason when invalid; else conflict_note
        reason = ""
        if st.invalid:
            reason = st.invalid_reason or ""
        elif st.conflict_status != CONFLICT_STATUS_NONE:
            reason = st.conflict_note or st.invalid_reason or ""
        else:
            reason = st.invalid_reason or st.conflict_note or ""
        self.status_reason_edit.setText(reason)
        check = st.last_check_time or "—"
        self.status_check_time_label.setText(f"最后检测：{check}")
        if not self.btn_view_conflicts.isChecked():
            self.status_conflict_detail.hide()

    def _status_reason_text(self) -> str:
        return self.status_reason_edit.text().strip()

    def _persist_status(self, **kwargs) -> None:
        mid = self.current_mod_id()
        if not mid or not mid.isdigit():
            return
        try:
            st = get_db().update_mod_status(mid, touch_check_time=True, **kwargs)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新状态失败", str(exc))
            return
        self._apply_status_to_widgets(st)
        try:
            self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            pass
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _on_mark_invalid(self) -> None:
        self._persist_status(
            invalid=True,
            invalid_reason=self._status_reason_text() or "已标记失效",
        )

    def _on_mark_valid(self) -> None:
        self._persist_status(invalid=False, invalid_reason="")

    def _on_mark_conflict(self) -> None:
        note = self._status_reason_text() or "已标记冲突"
        self._persist_status(
            conflict_status=CONFLICT_STATUS_CONFLICT,
            conflict_note=note,
        )

    def _on_clear_conflict(self) -> None:
        self._persist_status(
            conflict_status=CONFLICT_STATUS_NONE,
            conflict_note="",
        )

    def _on_toggle_conflict_detail(self, checked: bool) -> None:
        if not checked:
            self.status_conflict_detail.hide()
            return
        mid = self.current_mod_id()
        text = self._format_conflict_detail(mid)
        self.status_conflict_detail.setText(text or "当前无检测到的文件冲突。")
        self.status_conflict_detail.show()

    def _format_conflict_detail(self, mid: str | None) -> str:
        if not mid or self._library_root is None:
            # Fall back to stored note
            try:
                st = get_db().get_mod_status(mid) if mid else None
            except Exception:  # noqa: BLE001
                st = None
            if st and st.conflict_note:
                return f"冲突备注：{st.conflict_note}"
            return ""
        try:
            from services.conflict import ConflictDetector

            report = ConflictDetector(
                self._library_root, db=get_db()
            ).check_mod(mid, persist=False)
        except Exception as exc:  # noqa: BLE001
            return f"检测失败：{exc}"
        if not report.conflicts:
            try:
                st = get_db().get_mod_status(mid)
            except Exception:  # noqa: BLE001
                st = None
            if st and st.conflict_note:
                return f"冲突备注：{st.conflict_note}"
            return ""
        lines: list[str] = []
        for entry in report.conflicts:
            name = Path(entry.file).name or entry.file
            lines.append(f"冲突文件：{name}")
            # Resolve display names when possible
            sources: list[str] = []
            for other in entry.mods:
                label = other
                try:
                    info = get_db().get_mod_display_info(other)
                    if info and info.display_name:
                        label = f"{info.display_name} ({other})"
                except Exception:  # noqa: BLE001
                    pass
                sources.append(label)
            lines.append("来源：")
            for s in sources:
                lines.append(f"  · {s}")
            lines.append("")
        return "\n".join(lines).strip()

    def _on_tag_invalid_toggled(self, checked: bool) -> None:
        self.tag_invalid_reason.setEnabled(bool(checked))

    def _on_tag_conflict_toggled(self, checked: bool) -> None:
        self.tag_conflict_list.setEnabled(bool(checked))

    def _selected_conflict_ids(self) -> set[str]:
        out: set[str] = set()
        for i in range(self.tag_conflict_list.count()):
            item = self.tag_conflict_list.item(i)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                mid = str(item.data(Qt.ItemDataRole.UserRole) or "")
                if mid.isdigit():
                    out.add(mid)
        return out

    def _rebuild_conflict_list(self, *, preselect: set[str] | None = None) -> None:
        selected = preselect if preselect is not None else set()
        self.tag_conflict_list.clear()
        for mid, name in self._peer_mods:
            item = QListWidgetItem(f"{name}  ({mid})")
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setData(Qt.ItemDataRole.UserRole, mid)
            item.setCheckState(
                Qt.CheckState.Checked
                if mid in selected
                else Qt.CheckState.Unchecked
            )
            self.tag_conflict_list.addItem(item)

    def _fill_user_tags(self) -> None:
        mid = self.current_mod_id()
        invalid = False
        reason = ""
        conflict = False
        conflict_ids: set[str] = set()
        if mid:
            try:
                db = get_db()
                for tag in db.get_mod_tags(mid):
                    if tag.tag_type == TAG_TYPE_INVALID:
                        invalid = True
                        reason = tag.tag_value or ""
                    elif tag.tag_type == TAG_TYPE_CONFLICT:
                        conflict = True
                for rel in db.get_mod_relations(mid):
                    conflict_ids.add(rel.target_mod_id)
                    conflict = True
            except Exception:  # noqa: BLE001
                pass

        self.tag_invalid_check.blockSignals(True)
        self.tag_conflict_check.blockSignals(True)
        self.tag_invalid_check.setChecked(invalid)
        self.tag_invalid_reason.setText(reason)
        self.tag_invalid_reason.setEnabled(invalid)
        self.tag_conflict_check.setChecked(conflict)
        self.tag_conflict_list.setEnabled(conflict)
        self.tag_invalid_check.blockSignals(False)
        self.tag_conflict_check.blockSignals(False)
        self._rebuild_conflict_list(preselect=conflict_ids)

    def _save_user_tags(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            return
        try:
            db = get_db()
            if self.tag_invalid_check.isChecked():
                db.add_mod_tag(
                    mid,
                    TAG_TYPE_INVALID,
                    tag_value=self.tag_invalid_reason.text().strip(),
                )
            else:
                db.remove_mod_tag(mid, TAG_TYPE_INVALID)

            if self.tag_conflict_check.isChecked():
                targets = sorted(self._selected_conflict_ids())
                db.set_mod_conflict_targets(mid, targets)
                if not targets:
                    # Conflict marked without peers — still keep the tag
                    db.add_mod_tag(mid, TAG_TYPE_CONFLICT, tag_value="")
            else:
                db.set_mod_conflict_targets(mid, [])
                db.remove_mod_tag(mid, TAG_TYPE_CONFLICT)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存标记失败", str(exc))
            return

        self._fill_user_tags()
        if self._managed_path is not None:
            self.tags_saved.emit(self._managed_path)

    def _fill_edit_form(self) -> None:
        info = self._display_info
        meta = self._metadata
        steam = ""
        if info:
            steam = info.steam_name
        elif meta:
            steam = (meta.title or "").strip()
        self.edit_hint.setText(
            f"Mod ID：{meta.published_file_id if meta else '—'}"
            + (f"\nSteam 原名：{steam}" if steam else "")
        )
        self.edit_display_name.setText(info.user_display_name if info else "")
        self.edit_display_name.setPlaceholderText(steam or "留空则使用 Steam 原名")
        self.edit_custom_desc.setPlainText(info.custom_description if info else "")
        notes = ""
        if info:
            notes = info.user_notes
        elif meta:
            notes = meta.custom_notes or ""
        self.edit_notes.setPlainText(notes)
        self.edit_favorite.setChecked(bool(info.favorite) if info else False)

    def _offline_status_key(self) -> str:
        info = self._display_info
        if info is not None:
            status = normalize_offline_status(info.offline_status)
            if status != OFFLINE_STATUS_NONE:
                return status
        if self._has_offline_page():
            return OFFLINE_STATUS_ARCHIVED
        return OFFLINE_STATUS_NONE

    def _format_offline_updated_at(self) -> str:
        info = self._display_info
        raw = (info.offline_updated_at if info is not None else "") or ""
        text = str(raw).strip()
        if not text:
            return "—"
        # Prefer date portion for display (ISO / SQLite UTC).
        if "T" in text:
            return text.split("T", 1)[0]
        if " " in text:
            return text.split(" ", 1)[0]
        return text[:10] if len(text) >= 10 else text

    def _refresh_offline_status_label(self, *, busy: bool = False) -> None:
        """Show offline snapshot status, provider, and update time."""
        if busy:
            self.view_offline.setText("离线状态：刷新中…")
            self.view_offline.setStyleSheet("color: #d4a017;")
            return
        status = self._offline_status_key()
        provider = ""
        if self._display_info is not None:
            provider = format_offline_provider(self._display_info.offline_provider)
        updated = self._format_offline_updated_at()
        if status in (OFFLINE_STATUS_GENERATED, OFFLINE_STATUS_ARCHIVED):
            lines = [
                "离线状态：✓ 已保存",
                f"Provider：{provider}",
                f"更新时间：{updated}",
            ]
            self.view_offline.setText("\n".join(lines))
            self.view_offline.setStyleSheet("color: #3fb950;")
        elif status == OFFLINE_STATUS_FAILED:
            lines = [
                "离线状态：失败",
                f"Provider：{provider}",
                f"更新时间：{updated}",
            ]
            self.view_offline.setText("\n".join(lines))
            self.view_offline.setStyleSheet("color: #e06c75;")
        else:
            self.view_offline.setText("离线状态：未保存")
            self.view_offline.setStyleSheet("color: #d4a017;")

    def _iter_offline_index_candidates(self):
        """Yield candidate offline index paths (snapshot first, then Steam layout)."""
        if self._managed_path is None:
            return
        root = self._managed_path
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            yield root / info_name / OFFLINE_SNAPSHOT_DIR / OFFLINE_INDEX
            yield root / info_name / OFFLINE_INDEX

    def _has_offline_page(self) -> bool:
        """Existence check only — does not read HTML content or hit the network."""
        for index in self._iter_offline_index_candidates():
            try:
                if index.is_file() and index.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    def _index_path(self) -> Path | None:
        for index in self._iter_offline_index_candidates():
            try:
                if index.is_file() and index.stat().st_size > 0:
                    return index
            except OSError:
                continue
        return None

    def _set_cover(self, path: Path | None) -> None:
        if path and path.is_file():
            pix = QPixmap(str(path))
            if not pix.isNull():
                scaled = pix.scaled(
                    COVER_W,
                    COVER_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = max(0, (scaled.width() - COVER_W) // 2)
                y = max(0, (scaled.height() - COVER_H) // 2)
                self.cover_label.setPixmap(scaled.copy(x, y, COVER_W, COVER_H))
                return
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H))

    def _open_folder(self) -> None:
        if self._managed_path is None:
            return
        folder = self._managed_path.resolve()
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def _update_offline_download_button(self) -> None:
        busy = self._offline_worker is not None and self._offline_worker.isRunning()
        if busy:
            self.btn_download_offline.setText("正在保存...")
            self.btn_download_offline.setEnabled(False)
            self.btn_download_offline.setToolTip("")
            return
        plat = self._current_platform
        if plat == PLATFORM_STEAM:
            self.btn_download_offline.setText("下载 Steam 页面")
            self.btn_download_offline.setToolTip("抓取 Steam Workshop 网页并保存为离线页面")
        elif plat == PLATFORM_NEXUS:
            self.btn_download_offline.setText("保存 Nexus 页面")
            self.btn_download_offline.setToolTip("下载 Nexus Mods 网页快照（HTML + 资源）")
        elif plat == PLATFORM_GITHUB:
            self.btn_download_offline.setText("保存 GitHub 页面")
            self.btn_download_offline.setToolTip("下载 GitHub 仓库网页快照（HTML + 资源）")
        else:
            self.btn_download_offline.setText("保存离线页面")
            self.btn_download_offline.setToolTip("下载来源网站页面快照")
        self.btn_download_offline.setEnabled(True)

    def _download_offline_page(self) -> None:
        if self._managed_path is None or self._metadata is None:
            return
        if self._offline_worker is not None and self._offline_worker.isRunning():
            return
        from .offline_archive_thread import OfflineArchiveWorker

        worker = OfflineArchiveWorker(
            self._managed_path,
            platform=self._current_platform,
            published_file_id=self._metadata.published_file_id,
            metadata=self._metadata,
            library_root=self._library_root,
            parent=self,
        )
        worker.archive_started.connect(self._on_offline_archive_started)
        worker.archive_finished.connect(self._on_offline_archive_finished)
        worker.archive_failed.connect(self._on_offline_archive_failed)
        worker.finished.connect(self._on_offline_archive_thread_finished)
        self._offline_worker = worker
        self._update_offline_download_button()
        worker.start()

    def _on_offline_archive_started(self) -> None:
        self._refresh_offline_status_label(busy=True)
        self._update_offline_download_button()

    def _on_offline_archive_finished(self, path: str) -> None:
        if self._managed_path is not None:
            self.offline_page_updated.emit(self._managed_path)
            # Refresh local labels without full library rescan
            self.show_mod(self._managed_path)
        else:
            self._refresh_offline_status_label()
        self._update_offline_download_button()

    def _on_offline_archive_failed(self, error: str) -> None:
        self.view_offline.setText(f"离线状态：失败 — {error}")
        self.view_offline.setStyleSheet("color: #e06c75;")
        QMessageBox.warning(self, "离线页面", error or "保存失败")
        self._update_offline_download_button()

    def _on_offline_archive_thread_finished(self) -> None:
        self._offline_worker = None
        self._update_offline_download_button()

    def _open_offline(self) -> None:
        index = self._index_path()
        if index is None:
            QMessageBox.information(
                self,
                "离线页面",
                "离线页面尚未保存。\n可点击上方按钮下载来源网页快照。",
            )
            return
        if not webbrowser.open(index.resolve().as_uri()):
            QMessageBox.warning(self, "打开失败", f"无法打开：\n{index}")

    def _open_steam(self) -> None:
        url = self._current_source_url()
        if not url:
            QMessageBox.information(self, "来源链接", "当前 Mod 没有可用的来源链接。")
            return
        webbrowser.open(url)

    def _current_source_url(self) -> str:
        if getattr(self, "_source_url_value", ""):
            return str(self._source_url_value).strip()
        if self._display_info and (self._display_info.source_url or "").strip():
            return self._display_info.source_url.strip()
        if self._metadata is not None and self._metadata.published_file_id:
            if self._current_platform == PLATFORM_STEAM:
                return self._metadata.workshop_url
        return ""

    def _copy_to_clipboard(self, text: str) -> bool:
        from PySide6.QtGui import QClipboard

        clip = QApplication.clipboard()
        if clip is None:
            return False
        clip.setText(text or "", QClipboard.Mode.Clipboard)
        return True

    def _copy_name(self) -> None:
        value = (getattr(self, "_name_value", "") or "").strip()
        if not value:
            QMessageBox.information(self, "复制", "当前没有可复制的名称。")
            return
        if self._copy_to_clipboard(value):
            QMessageBox.information(self, "复制", "已复制名称。")

    def _copy_id(self) -> None:
        value = (getattr(self, "_id_value", "") or "").strip()
        if not value:
            QMessageBox.information(self, "复制", "当前没有可复制的 ID。")
            return
        if self._copy_to_clipboard(value):
            QMessageBox.information(self, "复制", "已复制 ID。")

    def _copy_source_url(self) -> None:
        url = self._current_source_url()
        if not url:
            QMessageBox.information(self, "复制链接", "当前 Mod 没有可用的来源链接。")
            return
        if self._copy_to_clipboard(url):
            QMessageBox.information(self, "复制链接", "已复制来源链接。")
        else:
            QMessageBox.warning(self, "复制链接", "无法访问系统剪贴板。")

    def _file_names_for_copy(self) -> list[str]:
        info = self._display_info
        if info is not None:
            names = [
                (f.filename or f.name).strip()
                for f in info.mod_files.files
                if (f.filename or f.name).strip()
            ]
            if names:
                return names
        return []

    def _copy_mod_info(self) -> None:
        meta = self._metadata
        info = self._display_info
        if meta is None:
            return
        shown = info.display_name if info else meta.display_name
        steam = info.steam_name if info else (meta.title or "").strip()
        platform = (
            info.platform
            if info is not None
            else getattr(self, "_current_platform", PLATFORM_STEAM)
        )
        name = (shown or steam or "").strip()
        external_id = format_external_id(
            platform,
            (info.external_id if info else "") or "",
            source_url=self._current_source_url(),
            published_file_id=str(meta.published_file_id or ""),
        )
        payload = format_mod_info_clipboard(
            name=name,
            platform=platform,
            source_url=self._current_source_url(),
            external_id=external_id,
            files=self._file_names_for_copy(),
        )
        if self._copy_to_clipboard(payload):
            QMessageBox.information(self, "复制全部信息", "已复制 Mod 信息。")
        else:
            QMessageBox.warning(self, "复制全部信息", "无法访问系统剪贴板。")

    def _cancel_edit(self) -> None:
        if self._managed_path is not None:
            self._stack.setCurrentWidget(self._view_page)
            self._mode = MODE_VIEW
            self._fill_view()
        else:
            self.clear()

    def _save_edit(self) -> None:
        meta = self._metadata
        path = self._managed_path
        if meta is None or path is None:
            return
        mid = meta.published_file_id
        if not str(mid).isdigit():
            QMessageBox.warning(self, "保存失败", "缺少有效的 Mod ID。")
            return
        try:
            self._display_info = get_db().update_mod_user_metadata(
                mid,
                {
                    "display_name": self.edit_display_name.text(),
                    "custom_description": self.edit_custom_desc.toPlainText(),
                    "user_notes": self.edit_notes.toPlainText(),
                    "favorite": self.edit_favorite.isChecked(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self._stack.setCurrentWidget(self._view_page)
        self._mode = MODE_VIEW
        self._fill_view()
        self.metadata_saved.emit(path)

    # ------------------------------------------------------------------
    # Deploy UI (worker lives in library_view)
    # ------------------------------------------------------------------

    def current_mod_id(self) -> str:
        if self._metadata and str(self._metadata.published_file_id).isdigit():
            return str(self._metadata.published_file_id)
        return ""

    def _request_deploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.deploy_requested.emit(mid)

    def _request_redeploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.redeploy_requested.emit(mid)

    def _request_undeploy(self) -> None:
        mid = self.current_mod_id()
        if not mid or self._deploy_busy:
            return
        self.undeploy_requested.emit(mid)

    def set_deploy_busy(self, busy: bool, *, action: str = "deploy") -> None:
        """Disable deploy buttons while a worker runs."""
        self._deploy_busy = bool(busy)
        if busy:
            label = {
                "undeploy": "正在取消部署…",
                "redeploy": "正在重新部署…",
            }.get(action, "正在部署…")
            self.view_deploy.setText(f"状态：{label}")
            for btn in (self.btn_deploy, self.btn_redeploy, self.btn_undeploy):
                btn.setEnabled(False)
        else:
            self._fill_deploy_status_from_db()

    def set_audit_hint(self, status: str, reason: str = "") -> None:
        """Show startup / consistency audit (missing / broken) without auto-fix."""
        if status in ("missing", "broken") and reason:
            label = "源缺失" if status == "missing" else "部署异常"
            self._audit_hint = f"一致性：{label} — {reason}"
        else:
            self._audit_hint = ""
        self.view_deploy_audit.setText(self._audit_hint)

    def apply_deploy_result(self, result: dict) -> None:
        """Update panel from a DeployWorker result dict (no library rescan)."""
        self._deploy_busy = False
        if not isinstance(result, dict):
            self.view_deploy.setText("状态：部署失败")
            self.view_deploy_error.setText("原因：未知错误")
            self._set_deploy_buttons(DEPLOY_STATUS_FAILED)
            return

        conflicts = result.get("conflicts")
        if isinstance(conflicts, dict):
            files = conflicts.get("files") or []
            entries = conflicts.get("conflicts") or []
            status = str(conflicts.get("status") or "")
            is_conflict = bool(conflicts.get("conflict")) or status in (
                CONFLICT_STATUS_WARNING,
                CONFLICT_STATUS_CONFLICT,
            )
            if is_conflict and (files or entries):
                n = len(files) if files else len(entries)
                sample = ""
                if files and isinstance(files[0], dict):
                    sample = str(files[0].get("existing_mod") or "")
                elif entries and isinstance(entries[0], dict):
                    mods = entries[0].get("mods") or []
                    mid_self = str(result.get("mod_id") or "")
                    sample = next(
                        (str(m) for m in mods if str(m) != mid_self),
                        str(mods[0]) if mods else "",
                    )
                self._conflict_hint = (
                    f"存在文件冲突（{n}）"
                    + (f"，例如已被 Mod {sample} 占用" if sample else "")
                    + "。本次仍继续部署，请人工确认。"
                )
            else:
                self._conflict_hint = ""
        else:
            self._conflict_hint = ""
        self.view_deploy_conflict.setText(self._conflict_hint)

        if result.get("success"):
            self._fill_deploy_status_from_db()
            self._fill_lifecycle_status()
            target = str(result.get("target") or "")
            if target:
                self.view_deploy_path.setText(f"目标路径：{target}")
            dtype = str(result.get("deploy_type") or "")
            if dtype:
                self.view_deploy_type.setText(f"部署类型：{dtype}")
            return

        error = humanize_deploy_error(str(result.get("error") or "未知错误"))
        self.view_deploy.setText("状态：部署失败")
        self.view_deploy_error.setText(f"原因：{error}")
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self._set_deploy_buttons(DEPLOY_STATUS_FAILED)

    def apply_deploy_failure(self, error: str) -> None:
        self._deploy_busy = False
        msg = humanize_deploy_error(error)
        self.view_deploy.setText("状态：部署失败")
        self.view_deploy_error.setText(f"原因：{msg}")
        self.view_deploy_path.clear()
        self.view_deploy_time.clear()
        self._set_deploy_buttons(DEPLOY_STATUS_FAILED)

    def _set_deploy_buttons(self, status: str) -> None:
        mid_ok = bool(self.current_mod_id()) and self._mode == MODE_VIEW
        busy = self._deploy_busy
        deployed = status == DEPLOY_STATUS_DEPLOYED
        self.btn_deploy.setEnabled(mid_ok and not busy and not deployed)
        self.btn_redeploy.setEnabled(mid_ok and not busy and deployed)
        self.btn_undeploy.setEnabled(mid_ok and not busy and deployed)

    def _resolve_deploy_type(self, mid: str) -> str:
        # Prefer manifest, then game config
        try:
            from services.deploy_rules import load_manifest

            if self._managed_path is not None:
                man = load_manifest(self._managed_path)
                if man and man.deploy_type:
                    return man.deploy_type
        except Exception:  # noqa: BLE001
            pass
        try:
            info = get_db().get_mod_deploy_info(mid)
            app_id = info.app_id if info else 0
            if not app_id and self._metadata:
                app_id = int(self._metadata.app_id or 0)
            if app_id:
                cfg = get_db().get_game_deploy_config(app_id)
                if cfg and cfg.deploy_type:
                    return cfg.deploy_type
        except Exception:  # noqa: BLE001
            pass
        return DEPLOY_TYPE_FOLDER_COPY

    def _fill_deploy_status_from_db(self) -> None:
        mid = self.current_mod_id()
        if not mid:
            self.view_deploy.setText("状态：—")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            self.view_deploy_type.clear()
            self.view_deploy_error.clear()
            self.view_deploy_audit.setText(self._audit_hint)
            self.view_deploy_conflict.setText(self._conflict_hint)
            self._set_deploy_buttons(DEPLOY_STATUS_NOT_DEPLOYED)
            return
        try:
            info = get_db().get_mod_deploy_info(mid)
        except Exception:  # noqa: BLE001
            info = None

        dtype = self._resolve_deploy_type(mid)
        self.view_deploy_type.setText(f"部署类型：{dtype}")

        status = (
            info.deploy_status
            if info is not None
            else DEPLOY_STATUS_NOT_DEPLOYED
        ) or DEPLOY_STATUS_NOT_DEPLOYED

        if status == DEPLOY_STATUS_DEPLOYED and info is not None:
            self.view_deploy.setText("状态：已部署")
            self.view_deploy_path.setText(
                f"目标路径：{info.deploy_path}" if info.deploy_path else "目标路径：—"
            )
            self.view_deploy_time.setText(
                f"部署时间：{info.deploy_time}" if info.deploy_time else "部署时间：—"
            )
            self.view_deploy_error.clear()
        elif status == DEPLOY_STATUS_FAILED:
            self.view_deploy.setText("状态：部署失败")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            err = (info.deploy_error if info else "") or ""
            self.view_deploy_error.setText(f"原因：{err}" if err else "原因：—")
        else:
            self.view_deploy.setText("状态：未部署")
            self.view_deploy_path.clear()
            self.view_deploy_time.clear()
            self.view_deploy_error.clear()

        self.view_deploy_audit.setText(self._audit_hint)
        self.view_deploy_conflict.setText(self._conflict_hint)
        self._set_deploy_buttons(status)

def _placeholder(width: int, height: int) -> QPixmap:
    pix = QPixmap(width, height)
    pix.fill(QColor("#1b2838"))
    painter = QPainter(pix)
    painter.setPen(QColor("#3d5a73"))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "No Preview")
    painter.end()
    return pix


_PANEL_STYLE = """
QWidget#modDetailPanel {
    background-color: #151c26;
    border-left: 1px solid #2c3a4d;
}
QFrame#detailPanelInner {
    background-color: #151c26;
}
QFrame#detailSection {
    background-color: #1a2330;
    border: 1px solid #243044;
    border-radius: 8px;
}
QFrame#detailFooter {
    background-color: #121820;
    border-top: 1px solid #2c3a4d;
}
QLabel#detailEmptyHint {
    color: #6b7c8f;
    font-size: 14px;
    line-height: 1.5;
}
QLabel#detailPanelTitle {
    color: #e8eef5;
    font-size: 16px;
    font-weight: 600;
}
QLabel#detailPanelSection {
    color: #66c0f4;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}
QLabel#detailPanelField {
    color: #8b9bb0;
    font-size: 11px;
    margin-top: 4px;
}
QLabel#detailPanelMeta {
    color: #8b9bb0;
    font-size: 12px;
}
QLabel#detailPanelBody {
    color: #c7d5e0;
    font-size: 13px;
}
QPushButton#panelActionButton {
    background-color: #1b2838;
    border: 1px solid #3d5a73;
    border-radius: 6px;
    padding: 8px 10px;
}
QPushButton#panelActionButton:hover {
    background-color: #243447;
}
QPushButton#panelPrimaryButton {
    background-color: #66c0f4;
    color: #0b1520;
    font-weight: 600;
    border: none;
    border-radius: 6px;
    padding: 8px 12px;
}
QPushButton#panelPrimaryButton:hover {
    background-color: #8ed0f8;
}
"""
