"""Main application window with Sync Center / Mod Library / Deploy routing."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QShowEvent
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
from .startup_lifecycle import StartupLifecycleMixin, log_startup
from .styles import APP_STYLE
from .sync_view import SyncCenterView
from .window_chrome import (
    TITLE_BAR_STYLE,
    apply_frameless_main_window_flags,
    install_frameless_main_window,
    on_frameless_main_window_shown,
)

ORG_NAME = "SteamModManager"
APP_NAME = "WorkshopLibrary"
SETTING_TARGET = "paths/target_dir"
SETTING_GEOMETRY = "ui/geometry"
SETTING_GAME_FILTER = "ui/game_filter"
SETTING_PAGE = "ui/page_index"

PAGE_SYNC = 0
PAGE_LIBRARY = 1
PAGE_DEPLOY = 2
_VALID_PAGES = (PAGE_SYNC, PAGE_LIBRARY, PAGE_DEPLOY)


class MainWindow(StartupLifecycleMixin, QMainWindow):
    def __init__(self) -> None:
        log_startup("MainWindow __init__ start")
        super().__init__()
        # Flags before any geometry / UI / native HWND — show() must stay last.
        apply_frameless_main_window_flags(self)
        log_startup("setWindowFlags done (frameless)")
        self.setWindowTitle("Steam 创意工坊 Mod 本地管理器")
        # Room for nav(128) + game(140) + 4-card Mod grid(~852) + detail(350).
        self.setMinimumSize(QSize(1520, 700))
        self.resize(1600, 820)
        log_startup(
            f"resize done size={self.width()}x{self.height()} "
            f"translucent={self.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)}"
        )

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.setStyleSheet(APP_STYLE + "\n" + TITLE_BAR_STYLE)
        log_startup("stylesheet applied")
        self._build_ui()
        log_startup("setCentralWidget + layout built")
        # Title strip only — flags already applied; no winId before show.
        self._title_bar = install_frameless_main_window(
            self, title=self.windowTitle(), flags_already_applied=True
        )
        log_startup("install_frameless_main_window done")
        self._restore_settings()
        self._native_chrome_ready = False
        log_startup(
            f"MainWindow __init__ end visible={self.isVisible()} "
            f"size={self.width()}x{self.height()} state={self.windowState()!r} "
            f"translucent={self.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)}"
        )

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        StartupLifecycleMixin.showEvent(self, event)
        if not self._native_chrome_ready:
            self._native_chrome_ready = True
            on_frameless_main_window_shown(self)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # ---- Left navigation ----
        nav_width = 128
        nav_wrap = QVBoxLayout()
        nav_wrap.setSpacing(8)
        brand = QLabel("Steam Mod\nManager")
        brand.setObjectName("titleLabel")
        brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        brand.setMaximumWidth(nav_width)
        nav_wrap.addWidget(brand)

        sub = QLabel("备份 · 整理 · 离线浏览 · 部署")
        sub.setObjectName("subtitleLabel")
        sub.setWordWrap(True)
        sub.setMaximumWidth(nav_width)
        nav_wrap.addWidget(sub)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setFixedWidth(nav_width)
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
        self.library_view.request_open_game_settings.connect(
            self._on_open_game_settings
        )
        self.deploy_view.config_saved.connect(self._on_game_config_saved)

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
            # Soft reload — reuse warm snapshot; Refresh button still forces
            self.library_view.refresh(force=False)
        elif row == PAGE_DEPLOY:
            self.deploy_view.refresh()
        elif row == PAGE_SYNC:
            self.sync_view.refresh_games()
        self.settings.setValue(SETTING_PAGE, row)

    def _goto_page(self, index: int) -> None:
        self.nav_list.setCurrentRow(index)

    def _on_open_game_settings(self, app_id: int) -> None:
        self._goto_page(PAGE_DEPLOY)
        self.deploy_view.refresh()
        self.deploy_view.select_app_id(int(app_id or 0))

    def _on_game_config_saved(self, app_id: int) -> None:
        del app_id
        # Recalculate Library game header / status after path save
        if self.stack.currentIndex() == PAGE_LIBRARY:
            self.library_view.refresh()

    def _on_paths_changed(self, workshop: str, target: str) -> None:
        # Workshop path is per-game in SQLite; only persist the shared library root.
        del workshop
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
        # Legacy global workshop path is no longer the source of truth.
        target = self.settings.value(SETTING_TARGET, "", str) or str(default_mod_library())
        self.sync_view.set_paths("", target)
        self.library_view.set_target_root(target)

        saved_filter = self.settings.value(SETTING_GAME_FILTER, ALL_GAMES_LABEL, str)
        self.library_view.set_preferred_filter(saved_filter)

        page = int(self.settings.value(SETTING_PAGE, PAGE_SYNC))
        page = page if page in _VALID_PAGES else PAGE_SYNC
        self.nav_list.setCurrentRow(page)
        if page == PAGE_LIBRARY:
            self.library_view.refresh(force=False)
        elif page == PAGE_DEPLOY:
            self.deploy_view.refresh()
        elif page == PAGE_SYNC:
            self.sync_view.refresh_games()

        geometry = self.settings.value(SETTING_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)
            log_startup(
                f"restoreGeometry done size={self.width()}x{self.height()} "
                f"state={self.windowState()!r}"
            )
        else:
            log_startup(
                f"restoreGeometry skipped (default) size={self.width()}x{self.height()} "
                f"state={self.windowState()!r}"
            )

        # Lightweight deploy consistency scan (deployed rows only; no auto-fix)
        # Deploy audit after first paints — avoid contending with Library load.
        QTimer.singleShot(750, self._run_startup_deploy_audit)
        # Backfill missing metadata backups off the UI thread
        QTimer.singleShot(0, self._run_startup_backup_rebuild)

    def _run_startup_backup_rebuild(self) -> None:
        try:
            from services.library_reconcile import start_reconcile_library_async

            root = getattr(self.library_view, "_target_root", None) or self.sync_view.target_path()
            start_reconcile_library_async(root)
            log_startup("reconcile_library scheduled")
        except Exception as exc:  # noqa: BLE001
            log_startup(f"reconcile_library skip: {exc}")
            try:
                from services.metadata_backup_sync import (
                    start_rebuild_missing_metadata_backup_async,
                )

                start_rebuild_missing_metadata_backup_async(root)
                log_startup("rebuild_missing_metadata_backup fallback scheduled")
            except Exception as exc2:  # noqa: BLE001
                log_startup(f"backup rebuild skip: {exc2}")

    def _run_startup_deploy_audit(self) -> None:
        try:
            self.library_view.set_target_root(self.sync_view.target_path())
            self.library_view.run_deploy_audit()
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.setValue(SETTING_TARGET, self.sync_view.target_path())
        self.settings.setValue(SETTING_GEOMETRY, self.saveGeometry())
        self.settings.setValue(SETTING_PAGE, self.stack.currentIndex())
        self.settings.setValue(SETTING_GAME_FILTER, self.library_view.current_filter())
        self.sync_view.shutdown()
        super().closeEvent(event)
