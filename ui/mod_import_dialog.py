"""Mod Import dialog — dynamic sources → library (folder or archive)."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.sanitize import sanitize_folder_name
from core.mod_platform import (
    MODIO_ANNO_1800_URL,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    default_source_url_for_platform,
    get_available_sources,
    is_anno_1800_game,
    is_stardew_valley_game,
    platform_requires_source_url,
)
from services.importers.archive import is_archive_path
from services.importers.import_settings import (
    resolve_import_start_directory,
    set_last_import_directory,
)
from services.importers.importer_base import ImportResult
from services.importers.modio import parse_modio_id
from services.importers.nexus import parse_nexus_id
from ui.import_thread import ImportWorker


def parse_archive_path_list(text: str) -> list[str]:
    """Split a multi-archive line edit (``;`` / newline separated)."""
    return [p.strip() for p in str(text or "").replace("\n", ";").split(";") if p.strip()]


class ModImportDialog(QDialog):
    """
    Import one Mod from a game-aware source list into DB + managed library.

    Supports local folder or zip/7z/rar. Heavy work runs on :class:`ImportWorker`.
    Source radios come from :func:`get_available_sources` (mod.io only for Anno 1800).
    """

    imported = Signal(object)  # ImportResult

    def __init__(
        self,
        library_root: str | Path,
        parent: QWidget | None = None,
        *,
        game_context: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.library_root = Path(library_root)
        self._result: ImportResult | None = None
        self._worker: ImportWorker | None = None
        self.game_context = dict(game_context or {})
        self.game_id = int(self.game_context.get("game_id") or 0)
        self.game_name = str(self.game_context.get("game_name") or "").strip()
        self._platform_radios: dict[str, QRadioButton] = {}
        self._platform_stack_index: dict[str, int] = {}

        self.setWindowTitle("导入 Mod")
        self.setMinimumWidth(540)
        self.setObjectName("modImportDialog")

        self._cover_path: str = ""
        self._offline_html_path: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(14)
        # Shrink-wrap to visible widgets — no leftover blank after setVisible(False).
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self._main_layout = root

        context_row = QHBoxLayout()
        context_caption = QLabel("目标游戏：")
        context_caption.setObjectName("subtitleLabel")
        self.game_context_label = QLabel(self.game_name or "—")
        self.game_context_label.setObjectName("detailPanelTitle")
        self.game_context_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        context_row.addWidget(context_caption)
        context_row.addWidget(self.game_context_label, stretch=1)
        root.addLayout(context_row)

        hint = QLabel(
            "从来源平台登记 Mod（选项随当前游戏变化）。\n"
            "Nexus / GitHub / mod.io / 其它 将导入到上方目标游戏（不自动推断游戏）。\n"
            "可选择本机文件夹，或直接导入 zip / 7z / rar 压缩包。\n"
            "「其它」无需源链接或离线页面；Nexus / GitHub 支持一次多选多个压缩包。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        plat_row = QHBoxLayout()
        self._plat_group = QButtonGroup(self)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        page_builders = {
            PLATFORM_STEAM: self._build_steam_page,
            PLATFORM_NEXUS: self._build_nexus_page,
            PLATFORM_GITHUB: self._build_github_page,
            PLATFORM_MODIO: self._build_modio_page,
            PLATFORM_OTHER: self._build_other_page,
        }
        sources = get_available_sources(self.game_name, self.game_id)
        for plat_id, label in sources:
            radio = QRadioButton(label)
            self._plat_group.addButton(radio)
            plat_row.addWidget(radio)
            self._platform_radios[plat_id] = radio
            builder = page_builders.get(plat_id)
            if builder is None:
                continue
            idx = self.stack.addWidget(builder())
            self._platform_stack_index[plat_id] = idx
            radio.toggled.connect(self._on_platform_toggled)
        if self._platform_radios:
            next(iter(self._platform_radios.values())).setChecked(True)
        self._apply_game_platform_rules()
        # Back-compat aliases used by older tests / callers.
        self.radio_steam = self._platform_radios.get(PLATFORM_STEAM)
        self.radio_nexus = self._platform_radios.get(PLATFORM_NEXUS)
        self.radio_github = self._platform_radios.get(PLATFORM_GITHUB)
        self.radio_modio = self._platform_radios.get(PLATFORM_MODIO)
        self.radio_other = self._platform_radios.get(PLATFORM_OTHER)
        plat_row.addStretch(1)
        root.addLayout(plat_row)
        root.addWidget(self.stack)

        root.addWidget(self._build_cover_row())
        root.addWidget(self._build_offline_html_row())

        self.status_label = QLabel("")
        self.status_label.setObjectName("subtitleLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("导入")
        buttons.accepted.connect(self._on_import)
        buttons.rejected.connect(self._on_cancel)
        root.addWidget(buttons)

        self._on_platform_toggled()
        self._relayout_dialog()

    @property
    def last_result(self) -> ImportResult | None:
        return self._result

    def selected_platform(self) -> str:
        for plat_id, radio in self._platform_radios.items():
            if not radio.isHidden() and radio.isChecked():
                return plat_id
        for plat_id, radio in self._platform_radios.items():
            if not radio.isHidden():
                return plat_id
        return PLATFORM_OTHER

    def set_game_context(self, game_context: dict | None) -> None:
        """Update target game and re-apply platform visibility rules."""
        self.game_context = dict(game_context or {})
        self.game_id = int(self.game_context.get("game_id") or 0)
        self.game_name = str(self.game_context.get("game_name") or "").strip()
        self.game_context_label.setText(self.game_name or "—")
        self._apply_game_platform_rules()

    def _apply_game_platform_rules(self) -> None:
        """Anno 1800 / Stardew Valley have no Steam Workshop — hide radio."""
        steam_radio = self._platform_radios.get(PLATFORM_STEAM)
        if steam_radio is None:
            return
        hide_steam = is_anno_1800_game(
            self.game_name, self.game_id
        ) or is_stardew_valley_game(self.game_name, self.game_id)
        steam_radio.setVisible(not hide_steam)
        if hide_steam and (steam_radio.isChecked() or steam_radio.isHidden()):
            # Prefer first non-Steam visible option (mod.io / Nexus / …).
            preferred = (
                PLATFORM_MODIO,
                PLATFORM_NEXUS,
                PLATFORM_GITHUB,
                PLATFORM_OTHER,
            )
            moved = False
            for plat_id in preferred:
                radio = self._platform_radios.get(plat_id)
                if radio is None or radio.isHidden():
                    continue
                radio.setChecked(True)
                moved = True
                break
            if not moved:
                for plat_id, radio in self._platform_radios.items():
                    if plat_id != PLATFORM_STEAM and not radio.isHidden():
                        radio.setChecked(True)
                        break
        self._on_platform_toggled()

    def _on_platform_toggled(self, *_args) -> None:
        plat = self.selected_platform()
        index = self._platform_stack_index.get(plat, 0)
        self.stack.setCurrentIndex(index)
        # Offline HTML is Nexus-only (Steam/GitHub keep their own offline flows).
        # Guard: radios fire during __init__ before offline_html_row exists.
        if hasattr(self, "offline_html_row"):
            self.offline_html_row.setVisible(plat == PLATFORM_NEXUS)
            if plat != PLATFORM_NEXUS:
                self._set_offline_html_path("")
        self._refresh_batch_mode_ui()
        self._relayout_dialog()

    def _relayout_dialog(self) -> None:
        """Collapse blank space after hide / page switch (SetFixedSize + stack height)."""
        current = self.stack.currentWidget()
        if current is not None:
            layout = current.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            current.adjustSize()
            current.updateGeometry()
            hint = current.sizeHint()
            # QStackedWidget otherwise keeps the tallest page's height.
            self.stack.setFixedHeight(max(1, hint.height()))
        self.stack.updateGeometry()
        if hasattr(self, "_main_layout"):
            self._main_layout.invalidate()
            self._main_layout.activate()
        self.adjustSize()

    def _build_steam_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        self.steam_id_edit = QLineEdit()
        self.steam_id_edit.setPlaceholderText("例如 3761838546 或 Workshop 链接")
        form.addRow("Workshop ID", self.steam_id_edit)
        self.steam_title_edit = QLineEdit()
        self.steam_title_edit.setPlaceholderText("可选：显示名称")
        form.addRow("显示名称", self.steam_title_edit)
        self.steam_folder_edit = QLineEdit()
        self.steam_folder_edit.setPlaceholderText("可选：本地 Mod 目录（用于扫描文件）")
        browse = QPushButton("浏览…")
        browse.setObjectName("browseButton")
        browse.clicked.connect(lambda: self._browse_folder(self.steam_folder_edit))
        row = QHBoxLayout()
        row.addWidget(self.steam_folder_edit, stretch=1)
        row.addWidget(browse)
        form.addRow("本地目录", row)
        return page

    def _build_source_type_row(
        self,
        *,
        folder_radio: str,
        archive_radio: str,
    ) -> tuple[QWidget, QRadioButton, QRadioButton]:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        r_folder = QRadioButton(folder_radio)
        r_archive = QRadioButton(archive_radio)
        r_folder.setChecked(True)
        grp = QButtonGroup(wrap)
        grp.addButton(r_folder)
        grp.addButton(r_archive)
        row.addWidget(r_folder)
        row.addWidget(r_archive)
        row.addStretch(1)
        return wrap, r_folder, r_archive

    def _build_path_picker_row(
        self,
        edit: QLineEdit,
        *,
        browse_slot,
    ) -> QWidget:
        """LineEdit + browse button wrapped so the whole form row can hide."""
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, stretch=1)
        browse = QPushButton("浏览…")
        browse.setObjectName("browseButton")
        browse.clicked.connect(browse_slot)
        row.addWidget(browse)
        return wrap

    @staticmethod
    def _set_form_row_visible(
        form: QFormLayout,
        field: QWidget,
        visible: bool,
    ) -> None:
        field.setVisible(visible)
        label = form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _on_import_method_changed(
        self,
        *,
        form: QFormLayout,
        archive_radio: QRadioButton,
        folder_row: QWidget,
        archive_row: QWidget,
        refresh_batch: bool = False,
    ) -> None:
        """Show only the path row that matches 导入方式 (folder vs archive)."""
        use_archive = archive_radio.isChecked()
        self._set_form_row_visible(form, folder_row, not use_archive)
        self._set_form_row_visible(form, archive_row, use_archive)
        if refresh_batch:
            self._refresh_batch_mode_ui()
        self._relayout_dialog()

    def _build_nexus_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        self.nexus_url_edit = QLineEdit()
        self.nexus_url_edit.setPlaceholderText(
            "https://www.nexusmods.com/palworld/mods/123 或纯数字 ID"
        )
        form.addRow("Nexus URL / ID", self.nexus_url_edit)
        self.nexus_title_edit = QLineEdit()
        self.nexus_title_edit.setPlaceholderText("可选：显示名称")
        form.addRow("显示名称", self.nexus_title_edit)

        type_wrap, self.nexus_src_folder, self.nexus_src_archive = (
            self._build_source_type_row(
                folder_radio="本地文件夹",
                archive_radio="压缩包",
            )
        )
        form.addRow("导入方式", type_wrap)

        self.nexus_folder_edit = QLineEdit()
        self.nexus_folder_edit.setPlaceholderText("本机 Mod 文件夹")
        self.nexus_folder_row = self._build_path_picker_row(
            self.nexus_folder_edit,
            browse_slot=lambda: self._browse_folder(self.nexus_folder_edit),
        )
        form.addRow("本地目录", self.nexus_folder_row)

        self.nexus_archive_edit = QLineEdit()
        self.nexus_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        self.nexus_archive_row = self._build_path_picker_row(
            self.nexus_archive_edit,
            browse_slot=lambda: self._browse_archive(self.nexus_archive_edit),
        )
        form.addRow("压缩包", self.nexus_archive_row)

        self._nexus_form = form
        self.nexus_src_folder.toggled.connect(self._sync_nexus_source_ui)
        self.nexus_src_archive.toggled.connect(self._sync_nexus_source_ui)
        self._sync_nexus_source_ui()
        return page

    def _build_github_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        self.github_url_edit = QLineEdit()
        self.github_url_edit.setPlaceholderText("https://github.com/user/project")
        form.addRow("GitHub URL", self.github_url_edit)
        self.github_title_edit = QLineEdit()
        self.github_title_edit.setPlaceholderText("可选：显示名称")
        form.addRow("显示名称", self.github_title_edit)

        type_wrap, self.github_src_folder, self.github_src_archive = (
            self._build_source_type_row(
                folder_radio="本地文件夹",
                archive_radio="压缩包",
            )
        )
        form.addRow("导入方式", type_wrap)

        self.github_folder_edit = QLineEdit()
        self.github_folder_edit.setPlaceholderText("本地 clone 目录")
        self.github_folder_row = self._build_path_picker_row(
            self.github_folder_edit,
            browse_slot=lambda: self._browse_folder(self.github_folder_edit),
        )
        form.addRow("本地目录", self.github_folder_row)

        self.github_archive_edit = QLineEdit()
        self.github_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        self.github_archive_row = self._build_path_picker_row(
            self.github_archive_edit,
            browse_slot=lambda: self._browse_archive(self.github_archive_edit),
        )
        form.addRow("压缩包", self.github_archive_row)

        self._github_form = form
        self.github_src_folder.toggled.connect(self._sync_github_source_ui)
        self.github_src_archive.toggled.connect(self._sync_github_source_ui)
        self._sync_github_source_ui()
        return page

    def _build_modio_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        self.modio_url_edit = QLineEdit()
        self.modio_url_edit.setPlaceholderText(
            f"可选；留空则使用 {MODIO_ANNO_1800_URL}"
        )
        form.addRow("mod.io URL / ID", self.modio_url_edit)
        self.modio_title_edit = QLineEdit()
        self.modio_title_edit.setPlaceholderText("可选：显示名称")
        form.addRow("显示名称", self.modio_title_edit)

        type_wrap, self.modio_src_folder, self.modio_src_archive = (
            self._build_source_type_row(
                folder_radio="本地文件夹",
                archive_radio="压缩包",
            )
        )
        form.addRow("导入方式", type_wrap)

        self.modio_folder_edit = QLineEdit()
        self.modio_folder_edit.setPlaceholderText("本机 Mod 文件夹")
        self.modio_folder_row = self._build_path_picker_row(
            self.modio_folder_edit,
            browse_slot=lambda: self._browse_folder(self.modio_folder_edit),
        )
        form.addRow("本地目录", self.modio_folder_row)

        self.modio_archive_edit = QLineEdit()
        self.modio_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        self.modio_archive_row = self._build_path_picker_row(
            self.modio_archive_edit,
            browse_slot=lambda: self._browse_archive(self.modio_archive_edit),
        )
        form.addRow("压缩包", self.modio_archive_row)

        self._modio_form = form
        self.modio_src_folder.toggled.connect(self._sync_modio_source_ui)
        self.modio_src_archive.toggled.connect(self._sync_modio_source_ui)
        self._sync_modio_source_ui()
        return page

    def _build_other_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        self.other_url_edit = QLineEdit()
        self.other_url_edit.setPlaceholderText("可选，可留空")
        form.addRow("源链接（可选）", self.other_url_edit)
        self.other_title_edit = QLineEdit()
        self.other_title_edit.setPlaceholderText("可选：显示名称")
        form.addRow("显示名称", self.other_title_edit)

        type_wrap, self.other_src_folder, self.other_src_archive = (
            self._build_source_type_row(
                folder_radio="本地文件夹",
                archive_radio="压缩包",
            )
        )
        form.addRow("导入方式", type_wrap)

        self.other_folder_edit = QLineEdit()
        self.other_folder_edit.setPlaceholderText("本地 Mod 目录")
        self.other_folder_row = self._build_path_picker_row(
            self.other_folder_edit,
            browse_slot=lambda: self._browse_folder(self.other_folder_edit),
        )
        form.addRow("本地目录", self.other_folder_row)

        self.other_archive_edit = QLineEdit()
        self.other_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        self.other_archive_row = self._build_path_picker_row(
            self.other_archive_edit,
            browse_slot=lambda: self._browse_archive(self.other_archive_edit),
        )
        form.addRow("压缩包", self.other_archive_row)

        self._other_form = form
        self.other_src_folder.toggled.connect(self._sync_other_source_ui)
        self.other_src_archive.toggled.connect(self._sync_other_source_ui)
        self._sync_other_source_ui()
        return page

    def _sync_nexus_source_ui(self, *_args) -> None:
        self._on_import_method_changed(
            form=self._nexus_form,
            archive_radio=self.nexus_src_archive,
            folder_row=self.nexus_folder_row,
            archive_row=self.nexus_archive_row,
            refresh_batch=True,
        )

    def _sync_github_source_ui(self, *_args) -> None:
        self._on_import_method_changed(
            form=self._github_form,
            archive_radio=self.github_src_archive,
            folder_row=self.github_folder_row,
            archive_row=self.github_archive_row,
            refresh_batch=True,
        )

    def _sync_modio_source_ui(self, *_args) -> None:
        self._on_import_method_changed(
            form=self._modio_form,
            archive_radio=self.modio_src_archive,
            folder_row=self.modio_folder_row,
            archive_row=self.modio_archive_row,
        )

    def _sync_other_source_ui(self, *_args) -> None:
        self._on_import_method_changed(
            form=self._other_form,
            archive_radio=self.other_src_archive,
            folder_row=self.other_folder_row,
            archive_row=self.other_archive_row,
        )

    def _build_cover_row(self) -> QWidget:
        wrap = QWidget()
        form = QFormLayout(wrap)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.cover_status_label = QLabel("未选择")
        self.cover_status_label.setObjectName("subtitleLabel")
        pick = QPushButton("选择图片")
        pick.setObjectName("browseButton")
        pick.clicked.connect(self._browse_cover)
        clear = QPushButton("清除")
        clear.setObjectName("browseButton")
        clear.clicked.connect(self._clear_cover)
        row = QHBoxLayout()
        row.addWidget(self.cover_status_label, stretch=1)
        row.addWidget(pick)
        row.addWidget(clear)
        form.addRow("展示图片（可选）", row)
        hint = QLabel("支持 png / jpg / jpeg / jfif / webp。跳过则使用默认占位图。")
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return wrap

    def _build_offline_html_row(self) -> QWidget:
        wrap = QWidget()
        self.offline_html_row = wrap
        form = QFormLayout(wrap)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.offline_html_status = QLabel("未选择")
        self.offline_html_status.setObjectName("subtitleLabel")
        pick = QPushButton("选择HTML页面")
        pick.setObjectName("browseButton")
        pick.clicked.connect(self._browse_offline_html)
        clear = QPushButton("清除")
        clear.setObjectName("browseButton")
        clear.clicked.connect(self._clear_offline_html)
        row = QHBoxLayout()
        row.addWidget(self.offline_html_status, stretch=1)
        row.addWidget(pick)
        row.addWidget(clear)
        form.addRow("离线页面（可选）", row)
        self.offline_clean_check = QCheckBox("优化离线页面布局（推荐）")
        self.offline_clean_check.setChecked(True)
        self.offline_clean_check.setToolTip(
            "对浏览器保存的 MHTML 清洗广告/登录壳与大面积空白，保留 Mod 核心阅读区。"
            "关闭则保留原始转换结果。"
        )
        form.addRow("", self.offline_clean_check)
        hint = QLabel(
            "请使用浏览器保存 Nexus 页面后导入。\n"
            "支持 .html / .htm / .mhtml / .mht。跳过则导入后 offline_status=none。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)
        wrap.setVisible(False)
        return wrap

    def _set_offline_html_path(self, path: str) -> None:
        self._offline_html_path = str(path or "").strip()
        if self._offline_html_path:
            self.offline_html_status.setText(Path(self._offline_html_path).name)
        else:
            self.offline_html_status.setText("未选择")

    def _clear_offline_html(self) -> None:
        self._set_offline_html_path("")

    def _browse_offline_html(self) -> None:
        start = resolve_import_start_directory(self._offline_html_path)
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Nexus 离线页面",
            start,
            "Offline Web Page (*.html *.htm *.mhtml *.mht);;所有文件 (*.*)",
        )
        if chosen:
            set_last_import_directory(chosen)
            self._set_offline_html_path(chosen)

    def _set_cover_path(self, path: str) -> None:
        self._cover_path = str(path or "").strip()
        if self._cover_path:
            self.cover_status_label.setText(Path(self._cover_path).name)
        else:
            self.cover_status_label.setText("未选择")

    def _clear_cover(self) -> None:
        self._set_cover_path("")

    def _browse_cover(self) -> None:
        start = resolve_import_start_directory(self._cover_path)
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择展示图片",
            start,
            "Images (*.png *.jpg *.jpeg *.jfif *.webp);;All files (*.*)",
        )
        if chosen:
            set_last_import_directory(chosen)
            self._set_cover_path(chosen)

    def _maybe_offer_sibling_cover(self, archive_path: str) -> None:
        """Prompt when a same-stem image sits next to the chosen archive."""
        from services.importers.image_picker import suggest_sibling_covers

        suggestions = suggest_sibling_covers(archive_path)
        if not suggestions:
            return
        first = suggestions[0]
        if self._cover_path and Path(self._cover_path).resolve() == first.resolve():
            return
        reply = QMessageBox.question(
            self,
            "检测到可能的展示图片",
            f"检测到可能的展示图片:\n{first.name}\n\n是否作为 Mod 封面？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # Yes = 使用, No = 忽略 — never auto-bind without this prompt.
        if reply == QMessageBox.StandardButton.Yes:
            self._set_cover_path(str(first))

    def _browse_folder(self, target: QLineEdit) -> None:
        start = resolve_import_start_directory(target.text())
        chosen = QFileDialog.getExistingDirectory(self, "选择 Mod 目录", start)
        if chosen:
            set_last_import_directory(chosen)
            target.setText(chosen)
            self._refresh_batch_mode_ui()

    def _empty_stub_folder(self, title: str = "", ident: str = "") -> str:
        """Empty payload folder used when the user picked no local path."""
        raw = str(title or ident or "").strip() or f"Empty_Mod_{uuid.uuid4().hex[:8]}"
        label = sanitize_folder_name(raw, fallback="Empty_Mod")
        dest = Path(tempfile.mkdtemp(prefix="smm_empty_import_")) / label
        dest.mkdir(parents=True, exist_ok=True)
        return str(dest)

    def _resolve_optional_local_source(
        self,
        *,
        use_archive: bool,
        archive_paths: list[str],
        folder_text: str,
        title: str = "",
        ident: str = "",
    ) -> tuple[str, bool, list[str]] | None:
        """Allow empty path: create a stub folder instead of blocking import."""
        if use_archive and archive_paths:
            if not all(is_archive_path(p) for p in archive_paths):
                QMessageBox.warning(
                    self, "导入失败", "请选择 .zip / .7z / .rar 文件。"
                )
                return None
            return "; ".join(archive_paths), True, archive_paths
        source = str(folder_text or "").strip()
        if not source:
            source = self._empty_stub_folder(title=title, ident=ident)
        return source, False, []

    def _refresh_batch_mode_ui(self) -> None:
        """
        This dialog is Single Import only.

        Source-link fields stay enabled; Multi Import uses the library
        「批量导入目录」entry and never this dialog.
        """
        if hasattr(self, "nexus_url_edit"):
            self.nexus_url_edit.setEnabled(True)
            self.nexus_url_edit.setPlaceholderText(
                "https://www.nexusmods.com/palworld/mods/123 或纯数字 ID"
            )
        if hasattr(self, "github_url_edit"):
            self.github_url_edit.setEnabled(True)
            self.github_url_edit.setPlaceholderText(
                "https://github.com/user/project"
            )
        if hasattr(self, "nexus_title_edit"):
            self.nexus_title_edit.setEnabled(True)
        if hasattr(self, "github_title_edit"):
            self.github_title_edit.setEnabled(True)

    def _browse_archive(self, target: QLineEdit) -> None:
        """Nexus / GitHub: multi-select archives as separate FileEntry sources."""
        existing = parse_archive_path_list(target.text())
        start = resolve_import_start_directory(*(existing[:1] or [None]))
        chosen, _ = QFileDialog.getOpenFileNames(
            self,
            "选择压缩包（可多选）",
            start,
            "Archives (*.zip *.7z *.rar);;All files (*.*)",
        )
        if not chosen:
            return
        set_last_import_directory(chosen[0])
        target.setText("; ".join(chosen))
        self._maybe_offer_sibling_cover(chosen[0])

    def _set_busy(self, busy: bool) -> None:
        self._ok_btn.setEnabled(not busy)
        self.stack.setEnabled(not busy)
        for radio in self._platform_radios.values():
            radio.setEnabled(not busy)

    def _on_cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
        self.reject()

    def _on_import(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        plat = self.selected_platform()
        params = self._collect_params(plat)
        if params is None:
            return

        self.status_label.setText("正在启动导入…")
        self._set_busy(True)
        worker = ImportWorker(
            platform=plat,
            library_root=self.library_root,
            params=params,
            parent=self,
        )
        worker.progress_changed.connect(self._on_progress)
        worker.import_finished.connect(self._on_import_ok)
        worker.import_failed.connect(self._on_import_err)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _context_params(self) -> dict:
        ctx = dict(self.game_context)
        if self._offline_html_path and self.selected_platform() == PLATFORM_NEXUS:
            ctx["offline_html_path"] = self._offline_html_path
        return {
            "context": ctx,
            "game_id": int(self.game_context.get("game_id") or 0),
            "game_name": str(self.game_context.get("game_name") or "").strip(),
            "app_id": int(self.game_context.get("game_id") or 0),
        }

    def _collect_params(self, plat: str) -> dict | None:
        ctx = self._context_params()
        if plat == PLATFORM_STEAM:
            wid = self.steam_id_edit.text().strip()
            if not wid:
                QMessageBox.warning(self, "导入失败", "请填写 Workshop ID。")
                return None
            return {
                "workshop_id": wid,
                "title": self.steam_title_edit.text().strip(),
                "folder": self.steam_folder_edit.text().strip(),
                "use_archive": False,
                "cover_source": self._cover_path,
                "is_batch_mode": False,
                **ctx,
            }
        if plat == PLATFORM_NEXUS:
            raw = self.nexus_url_edit.text().strip()
            nexus_id = parse_nexus_id(raw, "")
            nexus_url = raw if not raw.isdigit() else ""
            if raw.isdigit():
                nexus_id = raw
            use_archive = self.nexus_src_archive.isChecked()
            archive_paths = (
                parse_archive_path_list(self.nexus_archive_edit.text())
                if use_archive
                else []
            )
            title = self.nexus_title_edit.text().strip()
            resolved = self._resolve_optional_local_source(
                use_archive=use_archive,
                archive_paths=archive_paths,
                folder_text=self.nexus_folder_edit.text(),
                title=title,
                ident=nexus_id,
            )
            if resolved is None:
                return None
            source, use_archive, archive_paths = resolved
            return {
                "nexus_url": nexus_url or raw,
                "nexus_id": nexus_id,
                "title": title,
                "folder": "" if use_archive else source,
                "source_path": source if use_archive else "",
                "archive_paths": archive_paths if use_archive else [],
                "use_archive": use_archive,
                "cover_source": self._cover_path,
                "offline_html_path": self._offline_html_path,
                "offline_clean": bool(self.offline_clean_check.isChecked()),
                "is_batch_mode": False,
                **ctx,
            }
        if plat == PLATFORM_MODIO:
            raw = self.modio_url_edit.text().strip()
            modio_id = parse_modio_id(raw, "")
            modio_url = raw
            if not modio_url:
                modio_url = default_source_url_for_platform(
                    PLATFORM_MODIO,
                    game_name=self.game_name,
                    game_id=self.game_id,
                )
            use_archive = self.modio_src_archive.isChecked()
            archive_paths = (
                parse_archive_path_list(self.modio_archive_edit.text())
                if use_archive
                else []
            )
            title = self.modio_title_edit.text().strip()
            resolved = self._resolve_optional_local_source(
                use_archive=use_archive,
                archive_paths=archive_paths,
                folder_text=self.modio_folder_edit.text(),
                title=title,
                ident=modio_id,
            )
            if resolved is None:
                return None
            source, use_archive, archive_paths = resolved
            return {
                "modio_url": modio_url,
                "modio_id": modio_id,
                "title": title,
                "folder": "" if use_archive else source,
                "source_path": source if use_archive else "",
                "archive_paths": archive_paths if use_archive else [],
                "use_archive": use_archive,
                "cover_source": self._cover_path,
                "is_batch_mode": False,
                **ctx,
            }
        if plat == PLATFORM_OTHER:
            use_archive = self.other_src_archive.isChecked()
            archive_paths = (
                parse_archive_path_list(self.other_archive_edit.text())
                if use_archive
                else []
            )
            title = self.other_title_edit.text().strip()
            resolved = self._resolve_optional_local_source(
                use_archive=use_archive,
                archive_paths=archive_paths,
                folder_text=self.other_folder_edit.text(),
                title=title,
                ident="",
            )
            if resolved is None:
                return None
            source, use_archive, archive_paths = resolved
            # URL / offline are fully optional for「其它」.
            return {
                "source_url": self.other_url_edit.text().strip(),
                "other_url": self.other_url_edit.text().strip(),
                "title": title,
                "folder": "" if use_archive else source,
                "source_path": source if use_archive else "",
                "archive_paths": archive_paths if use_archive else [],
                "use_archive": use_archive,
                "cover_source": self._cover_path,
                "offline_html_path": "",
                "is_batch_mode": False,
                **ctx,
            }
        # GitHub
        url = self.github_url_edit.text().strip()
        use_archive = self.github_src_archive.isChecked()
        archive_paths = (
            parse_archive_path_list(self.github_archive_edit.text())
            if use_archive
            else []
        )
        title = self.github_title_edit.text().strip()
        resolved = self._resolve_optional_local_source(
            use_archive=use_archive,
            archive_paths=archive_paths,
            folder_text=self.github_folder_edit.text(),
            title=title,
            ident="",
        )
        if resolved is None:
            return None
        source, use_archive, archive_paths = resolved
        # GitHub still requires URL for single-mod imports;「其它」does not.
        if not url and platform_requires_source_url(PLATFORM_GITHUB):
            QMessageBox.warning(self, "导入失败", "请填写 GitHub URL。")
            return None
        return {
            "github_url": url,
            "title": title,
            "folder": "" if use_archive else source,
            "source_path": source if use_archive else "",
            "archive_paths": archive_paths if use_archive else [],
            "use_archive": use_archive,
            "cover_source": self._cover_path,
            "is_batch_mode": False,
            **ctx,
        }

    def _on_progress(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_import_ok(self, result: object) -> None:
        assert isinstance(result, ImportResult)
        self._result = result
        self.imported.emit(result)
        if result.is_duplicate and int(result.imported_count or 0) <= 1:
            self.status_label.setText("该 Mod 已存在，跳过导入")
            return
        count = int(result.imported_count or 0)
        if count > 1:
            skipped = int(result.skipped_count or 0)
            extra = f"，跳过 {skipped} 个" if skipped else ""
            msg = f"已成功导入 {count} 个 Mod{extra}"
        elif count == 0 and int(result.skipped_count or 0) > 0:
            msg = f"该 Mod 已存在，跳过导入（跳过 {int(result.skipped_count)} 个）"
        else:
            name = str(result.title or result.mod_id or "").strip()
            msg = f"已成功导入 {name}" if name else "已成功导入"
        self.status_label.setText(msg)
        # Close dialog immediately on success — library already receives `imported`.
        self.accept()

    def _clear_import_status(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        text = self.status_label.text()
        if (
            text.startswith("已成功导入")
            or text.startswith("该 Mod 已存在")
            or text == "导入完成"
        ):
            self.status_label.setText("")

    def _on_import_err(self, error: str) -> None:
        self.status_label.setText(error)
        QMessageBox.warning(self, "导入失败", error or "未知错误")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._set_busy(False)
