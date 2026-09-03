"""Mod detail dialog — metadata, notes, offline page / folder / Steam links."""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from core.models import ModMetadata
from services.archive import (
    DEFAULT_INDEX_NAME,
    OfflinePageArchiver,
    is_archive_cooldown_active,
    is_stub_offline_page,
)
from services.file_ops import ModFileManager

COVER_W = 200
COVER_H = 112


class OfflinePageWorker(QThread):
    """Background archive so Mod detail never blocks the UI thread on Steam I/O."""

    finished_ok = Signal(str)  # index.html path
    finished_error = Signal(str)

    def __init__(
        self,
        info_dir: str | Path,
        published_file_id: str | int,
        metadata: ModMetadata | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.info_dir = Path(info_dir)
        self.published_file_id = published_file_id
        self.metadata = metadata

    def run(self) -> None:
        try:
            with OfflinePageArchiver() as archiver:
                ensured = archiver.ensure_offline_page(
                    self.info_dir,
                    self.published_file_id,
                    metadata=self.metadata,
                    force_refresh=True,
                )
            path = ensured.path if hasattr(ensured, "path") else ensured
            outcome = getattr(ensured, "outcome", "success")
            if outcome in ("failed", "rate_limited"):
                err = getattr(ensured, "error", "") or "离线页面保存失败"
                self.finished_error.emit(str(err))
                return
            if outcome == "skipped":
                # Dialog path is always force_refresh; skip should not happen.
                self.finished_error.emit("离线页面未刷新（意外跳过）")
                return
            self.finished_ok.emit(str(path))
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.finished_error.emit(str(exc))


class ModDetailDialog(QDialog):
    """Rich detail view for a single managed Mod."""

    def __init__(
        self,
        managed_path: str | Path | None = None,
        parent: QWidget | None = None,
        *,
        mod_id: str | int | None = None,
    ) -> None:
        super().__init__(parent)
        from services.mod_metadata_resolver import resolve_mod_metadata

        self.managed_path = Path(managed_path) if managed_path is not None else Path()
        try:
            self.files = ModFileManager(self.managed_path.parents[1])
        except IndexError:
            self.files = ModFileManager(self.managed_path.parent)

        resolved = resolve_mod_metadata(mod_id, managed_path)
        self._resolved = resolved
        if resolved is not None and resolved.managed_path:
            self.managed_path = Path(resolved.managed_path)
        if resolved is not None:
            self.metadata = resolved.to_mod_metadata()
        else:
            self.metadata = ModMetadata(
                published_file_id=(
                    str(mod_id or "").strip()
                    or (self.managed_path.name if self.managed_path.name.isdigit() else "")
                ),
                title=self.managed_path.name,
                managed_path=str(self.managed_path),
            )
        self.metadata.managed_path = str(self.managed_path)
        self._folder_absent = bool(resolved is None or not resolved.folder_present)
        self._offline_worker: OfflinePageWorker | None = None
        self._display_info = None
        try:
            from core.db_manager import get_db

            mid = str(self.metadata.published_file_id or "").strip()
            if mid.isdigit():
                self._display_info = get_db().get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            self._display_info = None

        shown = (
            resolved.display_name
            if resolved is not None and resolved.display_name
            else self.metadata.display_name
        )
        self.setWindowTitle(f"Mod 详情 — {shown}")
        self.resize(820, 620)
        self.setMinimumSize(720, 520)
        self.setObjectName("modDetailDialog")
        self.setStyleSheet(_DIALOG_STYLE)

        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(16)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(COVER_W, COVER_H)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.cover_label)

        info_col = QVBoxLayout()
        info_col.setSpacing(6)
        self.title_label = QLabel()
        self.title_label.setObjectName("detailTitle")
        self.title_label.setWordWrap(True)
        info_col.addWidget(self.title_label)

        self.steam_name_label = QLabel()
        self.steam_name_label.setObjectName("detailMeta")
        self.steam_name_label.setWordWrap(True)
        info_col.addWidget(self.steam_name_label)

        self.id_label = QLabel()
        self.id_label.setObjectName("detailMeta")
        info_col.addWidget(self.id_label)

        self.game_label = QLabel()
        self.game_label.setObjectName("detailMeta")
        info_col.addWidget(self.game_label)

        self.offline_status = QLabel()
        self.offline_status.setObjectName("detailMeta")
        self.offline_status.setWordWrap(True)
        info_col.addWidget(self.offline_status)

        info_col.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_edit = QPushButton("编辑信息")
        self.btn_offline = QPushButton("打开离线页面")
        self.btn_folder = QPushButton("打开本地目录")
        self.btn_steam = QPushButton("Steam 原页")
        for btn in (self.btn_edit, self.btn_offline, self.btn_folder, self.btn_steam):
            btn.setObjectName("detailAction")
            actions.addWidget(btn)
        actions.addStretch(1)
        info_col.addLayout(actions)

        header.addLayout(info_col, stretch=1)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.desc_browser = QTextBrowser()
        self.desc_browser.setOpenExternalLinks(True)
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText(
            "在此记录个人备注，例如：必装前置、已知 BUG、配置说明…"
        )

        notes_page = QWidget()
        notes_layout = QVBoxLayout(notes_page)
        notes_layout.setContentsMargins(8, 8, 8, 8)
        notes_layout.addWidget(self.notes_edit)
        self.btn_save_notes = QPushButton("保存备注")
        self.btn_save_notes.setObjectName("syncButton")
        notes_layout.addWidget(self.btn_save_notes, alignment=Qt.AlignmentFlag.AlignRight)

        self.tabs.addTab(self.desc_browser, "官方简介")
        self.tabs.addTab(notes_page, "我的备注")
        root.addWidget(self.tabs, stretch=1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

        self.btn_edit.clicked.connect(self._open_edit)
        self.btn_offline.clicked.connect(self._open_offline)
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_steam.clicked.connect(self._open_steam)
        self.btn_save_notes.clicked.connect(self._save_notes)

    def _populate(self, *, refresh_offline: bool = True) -> None:
        meta = self.metadata
        info = self._display_info
        resolved = getattr(self, "_resolved", None)
        if resolved is not None:
            shown = resolved.display_name or meta.display_name
            steam_name = resolved.title or (meta.title or "").strip()
            desc_text = resolved.description
        else:
            shown = meta.display_name
            steam_name = (meta.title or "").strip()
            desc_text = str(meta.description or "")
        self.title_label.setText(shown)
        if steam_name and steam_name != shown:
            self.steam_name_label.setText(f"Steam 原名: {steam_name}")
            self.steam_name_label.show()
        else:
            self.steam_name_label.hide()
        ws = ""
        if info is not None:
            ws = str(info.workspace_id or "").strip()
        if not ws and resolved is not None:
            ws = str(getattr(resolved, "workspace_id", "") or "").strip()
        self.id_label.setText(f"Workspace ID: {ws or '—'}")
        game = (
            (resolved.game_name if resolved is not None else "")
            or meta.game_name
            or self.files.game_name_for_path(self.managed_path)
        )
        app = f" · AppID {meta.app_id}" if meta.app_id else ""
        self.game_label.setText(f"游戏: {game}{app}")

        from services.mod_metadata_resolver import resolve_cover_path

        cover = resolve_cover_path(meta.published_file_id or None, self.managed_path)
        self._set_cover(cover)

        custom_desc = (info.custom_description if info else "").strip()
        desc_parts: list[str] = []
        if custom_desc:
            desc_parts.append(f"<b>自定义介绍</b><br>{_html_escape(custom_desc)}")
        desc_parts.append(
            f"<b>Steam 简介</b><br>{_html_escape((desc_text or '').strip() or '（暂无官方描述）')}"
        )
        self.desc_browser.setHtml(
            f'<div style="color:#c7d5e0;font-family:Segoe UI;font-size:13px;'
            f'line-height:1.55;padding:8px;">{"<hr>".join(desc_parts)}</div>'
        )
        notes = ""
        if info:
            notes = info.user_notes
        elif meta.custom_notes:
            notes = meta.custom_notes
        self.notes_edit.setPlainText(notes)

        folder_ok = not getattr(self, "_folder_absent", False) and self.managed_path.is_dir()
        self.btn_folder.setEnabled(folder_ok)
        self.btn_edit.setEnabled(folder_ok)
        self.btn_offline.setEnabled(self._index_path() is not None)
        source = ""
        if resolved is not None:
            source = resolved.source_url
        if not source:
            source = str(meta.url or "").strip() or meta.workshop_url
        self.btn_steam.setEnabled(bool(source))

        if refresh_offline:
            self._show_cached_offline_state()
            if folder_ok:
                self._start_offline_refresh_if_needed()

    def _open_edit(self) -> None:
        from .mod_edit_dialog import ModEditDialog

        mid = self.metadata.published_file_id
        if not str(mid).isdigit():
            QMessageBox.warning(self, "无法编辑", "缺少有效的 Mod ID。")
            return
        steam = (
            self._display_info.steam_name
            if self._display_info
            else (self.metadata.title or "")
        )
        dialog = ModEditDialog(mid, steam_name=steam, parent=self)
        if dialog.exec() and dialog.saved_info is not None:
            self._display_info = dialog.saved_info
            from services.mod_metadata_resolver import resolve_mod_metadata

            resolved = resolve_mod_metadata(
                self.metadata.published_file_id, self.managed_path
            )
            self._resolved = resolved
            if resolved is not None:
                self.metadata = resolved.to_mod_metadata()
                self.metadata.managed_path = str(self.managed_path)
                shown = resolved.display_name
            else:
                shown = dialog.saved_info.display_name
            self.setWindowTitle(f"Mod 详情 — {shown}")
            self._populate(refresh_offline=False)

    def _index_path(self) -> Path | None:
        from services.mod_metadata_resolver import resolve_offline_page

        return resolve_offline_page(
            self.metadata.published_file_id or None, self.managed_path
        )

    def _show_cached_offline_state(self) -> None:
        index = self._index_path()
        if index is not None:
            self.metadata.offline_page_path = str(index)
            if is_stub_offline_page(index):
                if is_archive_cooldown_active(index.parent):
                    self.offline_status.setText(
                        "离线页面暂不可用，稍后自动重试"
                    )
                else:
                    self.offline_status.setText("离线页面暂不可用，准备更新…")
            else:
                self.offline_status.setText("离线页面已就绪")
        else:
            self.offline_status.setText("暂无离线页面")

    def _start_offline_refresh_if_needed(self) -> None:
        """Kick off background archive when the page is missing or a stale stub."""
        if getattr(self, "_folder_absent", False) or not self.managed_path.is_dir():
            return
        info = self.files.ensure_info_dir(self.managed_path)
        index = info / DEFAULT_INDEX_NAME
        try:
            if index.is_file() and index.stat().st_size > 0:
                if not is_stub_offline_page(index):
                    return
                if is_archive_cooldown_active(info):
                    return
        except OSError:
            pass

        if self._offline_worker and self._offline_worker.isRunning():
            return

        self.offline_status.setText("正在更新离线页面...")
        worker = OfflinePageWorker(
            info,
            self.metadata.published_file_id,
            metadata=self.metadata,
            parent=self,
        )
        worker.finished_ok.connect(self._on_offline_refresh_ok)
        worker.finished_error.connect(self._on_offline_refresh_error)
        worker.finished.connect(worker.deleteLater)
        self._offline_worker = worker
        worker.start()

    def _on_offline_refresh_ok(self, path: str) -> None:
        self.metadata.offline_page_path = path
        p = Path(path)
        if p.is_file() and not is_stub_offline_page(p):
            self.offline_status.setText("离线页面已更新")
        else:
            self.offline_status.setText("离线页面暂不可用，稍后自动重试")

    def _on_offline_refresh_error(self, message: str) -> None:
        self.offline_status.setText(f"离线页面更新失败：{message}")

    def _set_cover(self, path: Path | None) -> None:
        """Placeholder first; decode via CoverLoaderManager (not UI-thread QPixmap)."""
        self.cover_label.setPixmap(_placeholder(COVER_W, COVER_H))
        if path is None or not path.is_file():
            return
        from services.cover_loader import CoverLoaderManager

        token = f"dialog:{id(self)}:{path}"
        prev = getattr(self, "_cover_token", "") or ""
        mgr = CoverLoaderManager.instance()
        if prev and prev != token:
            mgr.cancel(prev)
        self._cover_token = token
        if not getattr(self, "_cover_loader_connected", False):
            mgr.image_ready.connect(self._on_dialog_cover_ready)
            self._cover_loader_connected = True
        managed = getattr(self, "managed_path", None) or path.parent
        mgr.request(
            token,
            managed,
            cover_ref=str(path),
            width=COVER_W,
            height=COVER_H,
        )

    def _on_dialog_cover_ready(self, token: object, image: object) -> None:
        if str(token) != getattr(self, "_cover_token", ""):
            return
        from PySide6.QtGui import QImage

        if not isinstance(image, QImage) or image.isNull():
            return
        pix = QPixmap.fromImage(image)
        if not pix.isNull():
            self.cover_label.setPixmap(pix)

    def _open_offline(self) -> None:
        # Strict guards — never hand an empty / missing path to the OS browser.
        # Canonical resolver only — ignore stale metadata.offline_page_path.
        index = self._index_path()
        if index is None or not str(index).strip():
            # No floating tip / white toast — refresh quietly if possible.
            self._start_offline_refresh_if_needed()
            return
        try:
            abs_path = str(Path(index).resolve())
        except OSError:
            abs_path = ""
        if not abs_path or not Path(abs_path).exists():
            self._start_offline_refresh_if_needed()
            return
        self.metadata.offline_page_path = abs_path
        if is_stub_offline_page(abs_path):
            QMessageBox.information(
                self,
                "离线页面",
                "离线页面暂不可用，稍后自动重试。\n仍可打开当前占位页查看错误信息。",
            )
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(abs_path))
        if not ok:
            return

    def _open_folder(self) -> None:
        folder = self.managed_path.resolve()
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def _open_steam(self) -> None:
        resolved = getattr(self, "_resolved", None)
        url = ""
        if resolved is not None:
            url = str(resolved.source_url or "").strip()
        if not url:
            url = str(self.metadata.url or "").strip() or self.metadata.workshop_url
        if url:
            webbrowser.open(url)

    def _save_notes(self) -> None:
        mid = self.metadata.published_file_id
        if not str(mid).isdigit():
            QMessageBox.warning(self, "保存失败", "缺少有效的 Mod ID。")
            return
        try:
            from core.db_manager import get_db

            info = get_db().get_mod_display_info(mid)
            payload = {
                "display_name": info.user_display_name if info else "",
                "custom_description": info.custom_description if info else "",
                "user_notes": self.notes_edit.toPlainText(),
                "favorite": info.favorite if info else False,
            }
            self._display_info = get_db().update_mod_user_metadata(mid, payload)
            QMessageBox.information(self, "已保存", "备注已写入数据库。")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt API
        worker = self._offline_worker
        if worker and worker.isRunning():
            worker.requestInterruption()
            # Do not wait for Steam — let the worker finish in background
            worker.setParent(None)
        super().closeEvent(event)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def _placeholder(width: int, height: int) -> QPixmap:
    pix = QPixmap(width, height)
    pix.fill(QColor("#1b2838"))
    painter = QPainter(pix)
    painter.setPen(QColor("#3d5a73"))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "No Preview")
    painter.end()
    return pix


_DIALOG_STYLE = """
QDialog#modDetailDialog {
    background-color: #121820;
    color: #e8eef5;
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
}
QLabel#detailTitle {
    font-size: 20px;
    font-weight: 600;
    color: #66c0f4;
}
QLabel#detailMeta {
    color: #8b9bb0;
    font-size: 13px;
}
QPushButton#detailAction {
    background-color: #2a475e;
    border: none;
    border-radius: 6px;
    padding: 8px 12px;
    color: #e8eef5;
}
QPushButton#detailAction:hover {
    background-color: #3d6a8a;
}
QPushButton#syncButton {
    background-color: #66c0f4;
    color: #0b1520;
    font-weight: 600;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
}
QTabWidget::pane {
    border: 1px solid #243044;
    border-radius: 8px;
    background: #171e28;
}
QTabBar::tab {
    background: #1a2330;
    color: #8b9bb0;
    padding: 8px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background: #2a475e;
    color: #66c0f4;
}
QTextEdit, QTextBrowser {
    background-color: #1a2330;
    border: 1px solid #2c3a4d;
    border-radius: 6px;
    color: #e8eef5;
    padding: 8px;
    selection-background-color: #3d7ea6;
}
QPushButton {
    background-color: #2a475e;
    border: none;
    border-radius: 6px;
    padding: 8px 14px;
    color: #e8eef5;
}
QPushButton:hover { background-color: #3d6a8a; }
"""
