"""Mod Import dialog — Steam / Nexus / GitHub → library (folder or archive)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from services.importers.archive import is_archive_path
from services.importers.importer_base import ImportResult
from services.importers.nexus import parse_nexus_id
from ui.import_thread import ImportWorker


class ModImportDialog(QDialog):
    """
    Import one Mod from Steam Workshop / Nexus / GitHub into DB + managed library.

    Supports local folder or zip/7z/rar. Heavy work runs on :class:`ImportWorker`.
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

        self.setWindowTitle("导入 Mod")
        self.setMinimumWidth(540)
        self.resize(580, 480)
        self.setObjectName("modImportDialog")

        root = QVBoxLayout(self)
        root.setSpacing(14)

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
            "从 Steam Workshop、Nexus Mods 或 GitHub 登记 Mod。\n"
            "Nexus / GitHub 将导入到上方目标游戏（不自动推断游戏）。\n"
            "可选择本机文件夹，或直接导入 zip / 7z / rar 压缩包。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        plat_row = QHBoxLayout()
        self.radio_steam = QRadioButton("Steam Workshop")
        self.radio_nexus = QRadioButton("Nexus Mods")
        self.radio_github = QRadioButton("GitHub")
        self.radio_steam.setChecked(True)
        self._plat_group = QButtonGroup(self)
        for btn in (self.radio_steam, self.radio_nexus, self.radio_github):
            self._plat_group.addButton(btn)
            plat_row.addWidget(btn)
        plat_row.addStretch(1)
        root.addLayout(plat_row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_steam_page())
        self.stack.addWidget(self._build_nexus_page())
        self.stack.addWidget(self._build_github_page())
        root.addWidget(self.stack, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("subtitleLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.radio_steam.toggled.connect(self._on_platform_toggled)
        self.radio_nexus.toggled.connect(self._on_platform_toggled)
        self.radio_github.toggled.connect(self._on_platform_toggled)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("导入")
        buttons.accepted.connect(self._on_import)
        buttons.rejected.connect(self._on_cancel)
        root.addWidget(buttons)

        self._on_platform_toggled()

    @property
    def last_result(self) -> ImportResult | None:
        return self._result

    def selected_platform(self) -> str:
        if self.radio_nexus.isChecked():
            return PLATFORM_NEXUS
        if self.radio_github.isChecked():
            return PLATFORM_GITHUB
        return PLATFORM_STEAM

    def _on_platform_toggled(self, *_args) -> None:
        plat = self.selected_platform()
        index = {
            PLATFORM_STEAM: 0,
            PLATFORM_NEXUS: 1,
            PLATFORM_GITHUB: 2,
        }.get(plat, 0)
        self.stack.setCurrentIndex(index)

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
        browse_f = QPushButton("浏览…")
        browse_f.setObjectName("browseButton")
        browse_f.clicked.connect(lambda: self._browse_folder(self.nexus_folder_edit))
        row_f = QHBoxLayout()
        row_f.addWidget(self.nexus_folder_edit, stretch=1)
        row_f.addWidget(browse_f)
        form.addRow("本地目录", row_f)

        self.nexus_archive_edit = QLineEdit()
        self.nexus_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        browse_a = QPushButton("浏览…")
        browse_a.setObjectName("browseButton")
        browse_a.clicked.connect(lambda: self._browse_archive(self.nexus_archive_edit))
        row_a = QHBoxLayout()
        row_a.addWidget(self.nexus_archive_edit, stretch=1)
        row_a.addWidget(browse_a)
        form.addRow("压缩包", row_a)

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
        browse_f = QPushButton("浏览…")
        browse_f.setObjectName("browseButton")
        browse_f.clicked.connect(lambda: self._browse_folder(self.github_folder_edit))
        row_f = QHBoxLayout()
        row_f.addWidget(self.github_folder_edit, stretch=1)
        row_f.addWidget(browse_f)
        form.addRow("本地目录", row_f)

        self.github_archive_edit = QLineEdit()
        self.github_archive_edit.setPlaceholderText("选择 .zip / .7z / .rar")
        browse_a = QPushButton("浏览…")
        browse_a.setObjectName("browseButton")
        browse_a.clicked.connect(lambda: self._browse_archive(self.github_archive_edit))
        row_a = QHBoxLayout()
        row_a.addWidget(self.github_archive_edit, stretch=1)
        row_a.addWidget(browse_a)
        form.addRow("压缩包", row_a)

        self.github_src_folder.toggled.connect(self._sync_github_source_ui)
        self.github_src_archive.toggled.connect(self._sync_github_source_ui)
        self._sync_github_source_ui()
        return page

    def _sync_nexus_source_ui(self, *_args) -> None:
        archive = self.nexus_src_archive.isChecked()
        self.nexus_folder_edit.setEnabled(not archive)
        self.nexus_archive_edit.setEnabled(archive)

    def _sync_github_source_ui(self, *_args) -> None:
        archive = self.github_src_archive.isChecked()
        self.github_folder_edit.setEnabled(not archive)
        self.github_archive_edit.setEnabled(archive)

    def _browse_folder(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择 Mod 目录", start)
        if chosen:
            target.setText(chosen)

    def _browse_archive(self, target: QLineEdit) -> None:
        start = target.text().strip() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择压缩包",
            start,
            "Archives (*.zip *.7z *.rar);;All files (*.*)",
        )
        if chosen:
            target.setText(chosen)

    def _set_busy(self, busy: bool) -> None:
        self._ok_btn.setEnabled(not busy)
        self.stack.setEnabled(not busy)
        self.radio_steam.setEnabled(not busy)
        self.radio_nexus.setEnabled(not busy)
        self.radio_github.setEnabled(not busy)

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
        return {
            "context": dict(self.game_context),
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
                **ctx,
            }
        if plat == PLATFORM_NEXUS:
            raw = self.nexus_url_edit.text().strip()
            nexus_id = parse_nexus_id(raw, "")
            nexus_url = raw if not raw.isdigit() else ""
            if raw.isdigit():
                nexus_id = raw
            use_archive = self.nexus_src_archive.isChecked()
            source = (
                self.nexus_archive_edit.text().strip()
                if use_archive
                else self.nexus_folder_edit.text().strip()
            )
            if not source:
                QMessageBox.warning(
                    self,
                    "导入失败",
                    "请选择本地文件夹或压缩包。",
                )
                return None
            if use_archive and not is_archive_path(source):
                QMessageBox.warning(self, "导入失败", "请选择 .zip / .7z / .rar 文件。")
                return None
            return {
                "nexus_url": nexus_url or raw,
                "nexus_id": nexus_id,
                "title": self.nexus_title_edit.text().strip(),
                "folder": "" if use_archive else source,
                "source_path": source if use_archive else "",
                "use_archive": use_archive,
                **ctx,
            }
        # GitHub
        url = self.github_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "导入失败", "请填写 GitHub URL。")
            return None
        use_archive = self.github_src_archive.isChecked()
        source = (
            self.github_archive_edit.text().strip()
            if use_archive
            else self.github_folder_edit.text().strip()
        )
        if not source:
            QMessageBox.warning(
                self,
                "导入失败",
                "请选择本地文件夹或压缩包。",
            )
            return None
        if use_archive and not is_archive_path(source):
            QMessageBox.warning(self, "导入失败", "请选择 .zip / .7z / .rar 文件。")
            return None
        return {
            "github_url": url,
            "title": self.github_title_edit.text().strip(),
            "folder": "" if use_archive else source,
            "source_path": source if use_archive else "",
            "use_archive": use_archive,
            **ctx,
        }

    def _on_progress(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_import_ok(self, result: object) -> None:
        assert isinstance(result, ImportResult)
        self._result = result
        self.imported.emit(result)
        self.status_label.setText("导入完成")
        QMessageBox.information(
            self,
            "导入成功",
            f"已导入 {result.title or result.mod_id}\n"
            f"平台：{result.platform}\n"
            f"文件：{result.files_count} 个",
        )
        self.accept()

    def _on_import_err(self, error: str) -> None:
        self.status_label.setText(error)
        QMessageBox.warning(self, "导入失败", error or "未知错误")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._set_busy(False)
