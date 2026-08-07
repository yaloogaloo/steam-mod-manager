"""Main application window with Sync Center / Mod Library / Deploy routing."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.paths import default_mod_library

from .game_deploy_view import GameDeployView
from .library_view import ALL_GAMES_LABEL, ModLibraryView
from .styles import APP_STYLE
from .sync_view import SyncCenterView

ORG_NAME = "SteamModManager"
APP_NAME = "WorkshopLibrary"
SETTING_WORKSHOP = "paths/workshop_dir"
SETTING_TARGET = "paths/target_dir"
SETTING_GEOMETRY = "ui/geometry"
SETTING_GAME_FILTER = "ui/game_filter"
SETTING_PAGE = "ui/page_index"

PAGE_SYNC = 0
PAGE_LIBRARY = 1
PAGE_DEPLOY = 2
_VALID_PAGES = (PAGE_SYNC, PAGE_LIBRARY, PAGE_DEPLOY)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Steam 创意工坊 Mod 本地管理器")
        self.setMinimumSize(QSize(1080, 700))
        self.resize(1200, 780)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.setStyleSheet(APP_STYLE)
        self._build_ui()
        self._restore_settings()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(14)

        # ---- Left navigation ----
        nav_wrap = QVBoxLayout()
        nav_wrap.setSpacing(8)
        brand = QLabel("Steam Mod\nManager")
        brand.setObjectName("titleLabel")
        brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        nav_wrap.addWidget(brand)

        sub = QLabel("备份 · 整理 · 离线浏览 · 部署")
        sub.setObjectName("subtitleLabel")
        nav_wrap.addWidget(sub)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setFixedWidth(168)
        self.nav_list.addItem(QListWidgetItem("同步中心"))
        self.nav_list.addItem(QListWidgetItem("Mod 库"))
        self.nav_list.addItem(QListWidgetItem("游戏部署"))
        self.nav_list.setCurrentRow(PAGE_SYNC)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        nav_wrap.addWidget(self.nav_list, stretch=1)
        outer.addLayout(nav_wrap)

        # ---- Stacked pages ----
        self.stack = QStackedWidget()
        self.sync_view = SyncCenterView()
        self.library_view = ModLibraryView()
        self.deploy_view = GameDeployView()

        self.sync_view.paths_changed.connect(self._on_paths_changed)
        self.sync_view.sync_completed.connect(self._on_sync_completed)
        self.sync_view.request_open_library.connect(lambda: self._goto_page(PAGE_LIBRARY))
        self.library_view.filter_changed.connect(self._on_filter_changed)
        self.library_view.request_open_sync.connect(lambda: self._goto_page(PAGE_SYNC))

        self.stack.addWidget(self.sync_view)
        self.stack.addWidget(self.library_view)
        self.stack.addWidget(self.deploy_view)
        outer.addWidget(self.stack, stretch=1)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_nav_changed(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        if row == PAGE_LIBRARY:
            self.library_view.set_target_root(self.sync_view.target_path())
            self.library_view.refresh()
        elif row == PAGE_DEPLOY:
            self.deploy_view.refresh()
        self.settings.setValue(SETTING_PAGE, row)

    def _goto_page(self, index: int) -> None:
        self.nav_list.setCurrentRow(index)

    def _on_paths_changed(self, workshop: str, target: str) -> None:
        self.settings.setValue(SETTING_WORKSHOP, workshop)
        self.settings.setValue(SETTING_TARGET, target)
        self.library_view.set_target_root(target)

    def _on_sync_completed(self) -> None:
        self.library_view.set_target_root(self.sync_view.target_path())
        self.library_view.refresh()

    def _on_filter_changed(self, text: str) -> None:
        self.settings.setValue(SETTING_GAME_FILTER, text)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _restore_settings(self) -> None:
        workshop = self.settings.value(SETTING_WORKSHOP, "", str)
        target = self.settings.value(SETTING_TARGET, "", str) or str(default_mod_library())
        self.sync_view.set_paths(workshop, target)
        self.library_view.set_target_root(target)

        saved_filter = self.settings.value(SETTING_GAME_FILTER, ALL_GAMES_LABEL, str)
        self.library_view.set_preferred_filter(saved_filter)

        page = int(self.settings.value(SETTING_PAGE, PAGE_SYNC))
        page = page if page in _VALID_PAGES else PAGE_SYNC
        self.nav_list.setCurrentRow(page)
        if page == PAGE_LIBRARY:
            self.library_view.refresh()
        elif page == PAGE_DEPLOY:
            self.deploy_view.refresh()

        geometry = self.settings.value(SETTING_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)

        # Lightweight deploy consistency scan (deployed rows only; no auto-fix)
        QTimer.singleShot(0, self._run_startup_deploy_audit)

    def _run_startup_deploy_audit(self) -> None:
        try:
            self.library_view.set_target_root(self.sync_view.target_path())
            self.library_view.run_deploy_audit()
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.setValue(SETTING_WORKSHOP, self.sync_view.workshop_path())
        self.settings.setValue(SETTING_TARGET, self.sync_view.target_path())
        self.settings.setValue(SETTING_GEOMETRY, self.saveGeometry())
        self.settings.setValue(SETTING_PAGE, self.stack.currentIndex())
        self.settings.setValue(SETTING_GAME_FILTER, self.library_view.current_filter())
        self.sync_view.shutdown()
        super().closeEvent(event)
