"""Sync Center view — game picker, library path, sync controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.db_manager import DatabaseManager, get_db
from core.game_info import GameInfo
from core.paths import default_mod_library, extract_app_id_from_workshop_path
from services.sync import SyncOptions

from .game_info_worker import GameInfoWorker
from .game_preview_card import GamePreviewCard
from .sync_thread import (
    OfflinePagesSyncWorker,
    SyncWorker,
    summarize_offline_result,
    summarize_result,
)

_SETTINGS_ORG = "SteamModManager"
_SETTINGS_APP = "WorkshopLibrary"
_SETTING_PROXY = "network/proxy_url"
_SETTING_STEAM_COOKIE = "network/steam_cookie"


class SyncCenterView(QWidget):
    """View A: pick a game, preview it, run workshop / offline sync."""

    sync_completed = Signal()
    paths_changed = Signal(str, str)  # workshop (from game), target
    request_open_library = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._sync_worker: SyncWorker | None = None
        self._offline_worker: OfflinePagesSyncWorker | None = None
        self._info_worker: GameInfoWorker | None = None
        self._selected_app_id: int | None = None
        self._workshop_path: str = ""
        self._loading_games = False
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

        self._build_ui()
        self._restore_proxy_setting()
        self._restore_steam_cookie_setting()

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        panel = QFrame()
        panel.setObjectName("controlPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)

        heading = QLabel("同步中心")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        panel_layout.addWidget(heading)

        hint = QLabel(
            "选择游戏后，系统会读取该游戏在「游戏部署」中配置的创意工坊目录。"
            "未配置工坊路径的游戏将跳过工坊同步（例如无 Steam 工坊的游戏）。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        panel_layout.addWidget(hint)

        picker_col = QVBoxLayout()
        picker_col.setSpacing(4)
        picker_label = QLabel("选择游戏")
        picker_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        picker_col.addWidget(picker_label)
        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        self.game_combo = QComboBox()
        self.game_combo.setObjectName("syncGameCombo")
        self.game_combo.currentIndexChanged.connect(self._on_game_selected)
        picker_row.addWidget(self.game_combo, stretch=1)
        self.refresh_games_btn = QPushButton("刷新列表")
        self.refresh_games_btn.setObjectName("browseButton")
        self.refresh_games_btn.clicked.connect(self.refresh_games)
        picker_row.addWidget(self.refresh_games_btn)
        picker_col.addLayout(picker_row)
        panel_layout.addLayout(picker_col)

        self.workshop_status = QLabel("工坊路径：未选择游戏")
        self.workshop_status.setObjectName("subtitleLabel")
        self.workshop_status.setWordWrap(True)
        panel_layout.addWidget(self.workshop_status)

        self.target_edit = QLineEdit()
        default_lib = str(default_mod_library())
        self.target_edit.setPlaceholderText(default_lib)
        self.target_edit.setText(default_lib)
        self.target_edit.textChanged.connect(self._emit_paths)
        panel_layout.addLayout(
            self._path_row(
                "本地 Mod 目标库（默认：项目根目录 / mod）",
                self.target_edit,
                self._browse_target,
            )
        )

        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(
            "可选，例如 http://127.0.0.1:7890（本地加速器/梯子端口）"
        )
        self.proxy_edit.editingFinished.connect(self._save_proxy_setting)
        proxy_col = QVBoxLayout()
        proxy_col.setSpacing(4)
        proxy_label = QLabel("网络代理 (可选)")
        proxy_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        proxy_col.addWidget(proxy_label)
        proxy_col.addWidget(self.proxy_edit)
        panel_layout.addLayout(proxy_col)

        self.steam_cookie_edit = QLineEdit()
        self.steam_cookie_edit.setPlaceholderText(
            "可选，浏览器 Cookie：steamLoginSecure=…; sessionid=…; steamCountry=…"
        )
        self.steam_cookie_edit.editingFinished.connect(self._save_steam_cookie_setting)
        cookie_col = QVBoxLayout()
        cookie_col.setSpacing(4)
        cookie_label = QLabel("Steam Cookie (可选，用于离线网页同步)")
        cookie_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        cookie_col.addWidget(cookie_label)
        cookie_col.addWidget(self.steam_cookie_edit)
        panel_layout.addLayout(cookie_col)

        self.force_overwrite_check = QCheckBox("强制覆盖/重新生成已存在的 Mod")
        self.force_overwrite_check.setToolTip(
            "勾选后将重新复制文件并重新拉取封面。"
            "Steam 离线网页请使用下方独立按钮同步。"
        )
        panel_layout.addWidget(self.force_overwrite_check)

        preview_label = QLabel("当前游戏")
        preview_label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        panel_layout.addWidget(preview_label)

        self.game_card = GamePreviewCard()
        panel_layout.addWidget(self.game_card)

        action_row = QHBoxLayout()
        self.sync_btn = QPushButton("全量同步")
        self.sync_btn.setObjectName("syncButton")
        self.sync_btn.clicked.connect(self.start_sync)
        action_row.addWidget(self.sync_btn, stretch=2)

        self.offline_sync_btn = QPushButton("同步 Steam 离线网页")
        self.offline_sync_btn.setToolTip(
            "独立任务：低频下载已入库 Mod 的 Steam 离线网页。"
            "全量同步不会执行此项。"
        )
        self.offline_sync_btn.clicked.connect(self.start_offline_pages_sync)
        action_row.addWidget(self.offline_sync_btn, stretch=2)

        self.open_library_btn = QPushButton("查看 Mod 库")
        self.open_library_btn.clicked.connect(self.request_open_library.emit)
        action_row.addWidget(self.open_library_btn, stretch=1)
        panel_layout.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        panel_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("就绪。")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        panel_layout.addWidget(self.status_label)

        root.addWidget(panel)
        root.addStretch(1)

    def _path_row(self, label_text: str, edit: QLineEdit, browse_slot) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet("color: #8b9bb0; font-size: 12px;")
        col.addWidget(label)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(edit, stretch=1)
        browse = QPushButton("浏览…")
        browse.setObjectName("browseButton")
        browse.clicked.connect(browse_slot)
        row.addWidget(browse)
        col.addLayout(row)
        return col

    # ------------------------------------------------------------------
    # Public setters (settings restore)
    # ------------------------------------------------------------------

    def set_paths(self, workshop: str, target: str) -> None:
        """
        Restore library target path.

        *workshop* is ignored as a global setting — workshop paths live on
        each game's deploy config. Kept for call-site compatibility.
        """
        del workshop
        self.target_edit.blockSignals(True)
        if target:
            self.target_edit.setText(target)
        elif not self.target_edit.text().strip():
            self.target_edit.setText(str(default_mod_library()))
        self.target_edit.blockSignals(False)
        self.refresh_games()
        self._emit_paths()

    def workshop_path(self) -> str:
        return str(self._workshop_path or "").strip()

    def target_path(self) -> str:
        text = self.target_edit.text().strip()
        return text or str(default_mod_library())

    def selected_app_id(self) -> int | None:
        return self._selected_app_id

    def proxy_url(self) -> str:
        return self.proxy_edit.text().strip()

    def steam_cookie(self) -> str:
        return self.steam_cookie_edit.text().strip()

    def refresh_games(self) -> None:
        """Reload games from SQLite and keep the current AppID if possible."""
        previous = int(self._selected_app_id or 0)
        games = [g for g in self._database().list_games() if g.app_id]

        self._loading_games = True
        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        self.game_combo.addItem("— 选择游戏 —", 0)
        for game in games:
            label = game.name.strip() or f"App_{game.app_id}"
            self.game_combo.addItem(f"{label}  ({game.app_id})", int(game.app_id))

        select_index = 0
        if previous > 0:
            for i in range(self.game_combo.count()):
                if int(self.game_combo.itemData(i) or 0) == previous:
                    select_index = i
                    break
        self.game_combo.setCurrentIndex(select_index)
        self.game_combo.blockSignals(False)
        self._loading_games = False

        if select_index > 0:
            self._apply_selected_game(int(self.game_combo.currentData() or 0))
        else:
            self._clear_selected_game()

    def _restore_proxy_setting(self) -> None:
        saved = self._settings.value(_SETTING_PROXY, "", str)
        if saved:
            self.proxy_edit.setText(saved)

    def _save_proxy_setting(self) -> None:
        self._settings.setValue(_SETTING_PROXY, self.proxy_url())

    def _restore_steam_cookie_setting(self) -> None:
        saved = self._settings.value(_SETTING_STEAM_COOKIE, "", str)
        if saved:
            self.steam_cookie_edit.setText(saved)

    def _save_steam_cookie_setting(self) -> None:
        self._settings.setValue(_SETTING_STEAM_COOKIE, self.steam_cookie())

    # ------------------------------------------------------------------
    # Game selection → workshop_path
    # ------------------------------------------------------------------

    def _on_game_selected(self, index: int) -> None:
        if self._loading_games:
            return
        app_id = int(self.game_combo.itemData(index) or 0)
        if app_id <= 0:
            self._clear_selected_game()
            return
        self._apply_selected_game(app_id)

    def _clear_selected_game(self) -> None:
        self._selected_app_id = None
        self._workshop_path = ""
        self.workshop_status.setText("工坊路径：未选择游戏")
        self.game_card.clear()
        self._emit_paths()

    def _apply_selected_game(self, app_id: int) -> None:
        self._selected_app_id = app_id
        cfg = self._database().get_game_deploy_config(app_id)
        workshop = (cfg.workshop_path if cfg is not None else "") or ""
        self._workshop_path = workshop.strip()
        if self._workshop_path:
            self.workshop_status.setText(f"工坊路径：{self._workshop_path}")
        else:
            self.workshop_status.setText(
                "工坊路径：未配置（此游戏将跳过创意工坊同步）"
            )
        self._emit_paths()
        self._load_game_preview(app_id)

    def _emit_paths(self) -> None:
        self.paths_changed.emit(self.workshop_path(), self.target_path())

    def _load_game_preview(self, app_id: int) -> None:
        self.game_card.show_loading(app_id)
        self.status_label.setText(f"正在查询 AppID {app_id} 的商店信息…")

        if self._info_worker and self._info_worker.isRunning():
            self._info_worker.requestInterruption()
        worker = GameInfoWorker(app_id)
        worker.finished_ok.connect(self._on_game_info_ok)
        worker.finished_error.connect(self._on_game_info_error)
        self._info_worker = worker
        worker.start()

    def _on_game_info_ok(self, info: object, cover_path: object) -> None:
        assert isinstance(info, GameInfo)
        if self._selected_app_id and info.app_id != self._selected_app_id:
            return
        cover = None
        if isinstance(cover_path, str) and cover_path:
            pix = QPixmap(cover_path)
            if not pix.isNull():
                cover = pix
        self.game_card.show_info(info, cover)
        self.status_label.setText(
            f"已识别游戏：{info.display_name}（AppID {info.app_id}）"
        )

    def _on_game_info_error(self, message: str) -> None:
        app_id = self._selected_app_id or 0
        self.game_card.show_info(GameInfo.fallback(app_id, message))
        self.status_label.setText(f"游戏信息获取失败，将使用 App_{app_id}")

    # ------------------------------------------------------------------
    # Browse
    # ------------------------------------------------------------------

    def _browse_target(self) -> None:
        start = self.target_path() or str(default_mod_library())
        path = QFileDialog.getExistingDirectory(self, "选择本地 Mod 目标库", start)
        if path:
            self.target_edit.setText(path)
            Path(path).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def _any_sync_running(self) -> bool:
        return bool(
            (self._sync_worker and self._sync_worker.isRunning())
            or (self._offline_worker and self._offline_worker.isRunning())
        )

    def _set_sync_buttons_enabled(self, enabled: bool) -> None:
        self.sync_btn.setEnabled(enabled)
        self.offline_sync_btn.setEnabled(enabled)

    def start_sync(self) -> None:
        if self._any_sync_running():
            QMessageBox.information(self, "同步进行中", "请等待当前同步完成。")
            return

        if not self._selected_app_id:
            QMessageBox.information(self, "请选择游戏", "请先在下拉菜单中选择要同步的游戏。")
            return

        workshop = self.workshop_path()
        target = self.target_path()
        self.target_edit.setText(target)

        # No workshop path → game has no Steam Workshop; skip quietly.
        if not workshop:
            self.progress_bar.setValue(0)
            self.status_label.setText(
                "当前游戏未配置创意工坊路径，已跳过工坊同步。"
            )
            self._emit_paths()
            return

        if not Path(workshop).is_dir():
            QMessageBox.warning(
                self,
                "路径无效",
                "该游戏配置的 Steam 创意工坊目录不存在或不是文件夹。"
                "请到「游戏部署」中修正。",
            )
            return

        Path(target).mkdir(parents=True, exist_ok=True)
        self._emit_paths()
        self._save_proxy_setting()
        self._save_steam_cookie_setting()

        self._set_sync_buttons_enabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("正在启动同步…")

        # Flat scan when path already points at a single AppID folder
        recursive = extract_app_id_from_workshop_path(workshop) is None

        worker = SyncWorker(
            workshop,
            target,
            SyncOptions(
                skip_existing=True,
                archive_pages=False,
                download_covers=True,
                overwrite_files=self.force_overwrite_check.isChecked(),
                recursive_scan=recursive,
                proxy_url=self.proxy_url(),
                steam_cookie=self.steam_cookie(),
            ),
        )
        worker.progress_changed.connect(self._on_sync_progress)
        worker.sync_finished.connect(self._on_sync_finished)
        worker.sync_failed.connect(self._on_sync_failed)
        worker.finished.connect(self._on_full_sync_thread_finished)
        self._sync_worker = worker
        worker.start()

    def start_offline_pages_sync(self) -> None:
        if self._any_sync_running():
            QMessageBox.information(self, "同步进行中", "请等待当前同步完成。")
            return

        target = self.target_path()
        self.target_edit.setText(target)
        if not target:
            QMessageBox.warning(self, "路径无效", "请先设置本地 Mod 目标库。")
            return

        Path(target).mkdir(parents=True, exist_ok=True)
        self._save_proxy_setting()
        self._save_steam_cookie_setting()
        self._set_sync_buttons_enabled(False)
        self.progress_bar.setValue(1)
        self.status_label.setText(
            "Steam 离线网页同步\n"
            "正在处理:\n0/…\n"
            "等待:\n…\n"
            "已完成:\n0/…\n"
            "状态:\n正在排队…"
        )

        worker = OfflinePagesSyncWorker(
            target,
            proxy_url=self.proxy_url(),
            steam_cookie=self.steam_cookie(),
        )
        worker.progress_changed.connect(self._on_sync_progress)
        worker.sync_finished.connect(self._on_offline_sync_finished)
        worker.sync_failed.connect(self._on_sync_failed)
        worker.finished.connect(self._on_offline_sync_thread_finished)
        self._offline_worker = worker
        worker.start()

    def _on_sync_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        self.status_label.setText(message)

    def _on_sync_finished(self, result: object) -> None:
        from services.sync import SyncResult

        assert isinstance(result, SyncResult)
        self.progress_bar.setValue(100)
        summary = summarize_result(result)
        self.status_label.setText(summary + "  ·  可切换到「Mod 库」查看。")
        if result.failed:
            details = "\n".join(
                f"• {(m.published_file_id if m else '?')}: {err}"
                for m, err in result.failed[:8]
            )
            extra = "" if len(result.failed) <= 8 else f"\n…另有 {len(result.failed) - 8} 项"
            QMessageBox.warning(
                self,
                "部分同步失败",
                f"{summary}\n\n失败详情：\n{details}{extra}",
            )
        else:
            QMessageBox.information(
                self,
                "同步完成",
                f"{summary}\n\n请切换到「Mod 库」浏览已整理的 Mod。",
            )
        self.sync_completed.emit()

    def _on_offline_sync_finished(self, result: object) -> None:
        from services.sync import SyncResult

        assert isinstance(result, SyncResult)
        self.progress_bar.setValue(100)
        summary = summarize_offline_result(result)
        self.status_label.setText(summary)
        details_lines: list[str] = []
        for m, err in result.rate_limited[:8]:
            details_lines.append(
                f"• 429 {(m.published_file_id if m else '?')}: {err}"
            )
        for m, err in result.failed[:8]:
            details_lines.append(
                f"• 失败 {(m.published_file_id if m else '?')}: {err}"
            )
        if details_lines:
            shown = len(result.rate_limited) + len(result.failed)
            extra_n = shown - len(details_lines)
            extra = f"\n…另有 {extra_n} 项" if extra_n > 0 else ""
            QMessageBox.warning(
                self,
                "离线网页同步结果",
                f"{summary}\n\n详情：\n" + "\n".join(details_lines) + extra,
            )
        else:
            QMessageBox.information(self, "离线网页同步完成", summary)
        self.sync_completed.emit()

    def _on_sync_failed(self, error_message: str) -> None:
        self.status_label.setText(f"同步失败：{error_message}")
        QMessageBox.critical(self, "同步失败", error_message)

    def _on_full_sync_thread_finished(self) -> None:
        self._sync_worker = None
        if not self._any_sync_running():
            self._set_sync_buttons_enabled(True)

    def _on_offline_sync_thread_finished(self) -> None:
        self._offline_worker = None
        if not self._any_sync_running():
            self._set_sync_buttons_enabled(True)

    def shutdown(self) -> None:
        if self._sync_worker and self._sync_worker.isRunning():
            self._sync_worker.requestInterruption()
            self._sync_worker.wait(3000)
        if self._offline_worker and self._offline_worker.isRunning():
            self._offline_worker.requestInterruption()
            self._offline_worker.wait(3000)
        if self._info_worker and self._info_worker.isRunning():
            self._info_worker.wait(2000)
