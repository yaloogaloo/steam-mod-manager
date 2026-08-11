"""Window chrome helpers — app icon, dark titlebar, frameless title strip."""

from __future__ import annotations

import ctypes
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.styles import ACCENT_ERROR, BACKGROUND_PRIMARY, BORDER_DEFAULT, TEXT_PRIMARY

# DWM attribute for immersive dark mode (Win10 1809+ / Win11).
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19


def resources_dir() -> Path:
    """Project ``resources/`` next to the repo root (or frozen exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "resources"
    return Path(__file__).resolve().parent.parent / "resources"


def app_icon_path() -> Path:
    return resources_dir() / "app.ico"


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    path = app_icon_path()
    if path.is_file():
        return QIcon(str(path))
    return QIcon()


def apply_application_icon(app) -> None:
    """Set the process-wide window icon (taskbar + all top-level windows)."""
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)


def enable_windows_dark_titlebar(widget: QWidget) -> None:
    """Tint the native Windows titlebar dark to match the app theme."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
    except Exception:  # noqa: BLE001
        return
    if not hwnd:
        return
    value = ctypes.c_int(1)
    dwmapi = ctypes.windll.dwmapi
    # Prefer modern attribute; fall back for older Win10 builds.
    for attr in (
        _DWMWA_USE_IMMERSIVE_DARK_MODE,
        _DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1,
    ):
        try:
            hr = dwmapi.DwmSetWindowAttribute(
                hwnd,
                attr,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if hr == 0:
                break
        except Exception:  # noqa: BLE001
            continue


def polish_top_level_window(widget: QWidget) -> None:
    """Icon + dark native titlebar for dialogs / message boxes / main window."""
    icon = app_icon()
    if not icon.isNull() and widget.windowIcon().isNull():
        widget.setWindowIcon(icon)
    enable_windows_dark_titlebar(widget)


class DarkTitleBarFilter(QObject):
    """Apply dark titlebar + app icon when top-level windows are shown."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(
            watched, (QDialog, QMainWindow, QMessageBox)
        ):
            polish_top_level_window(watched)
        return super().eventFilter(watched, event)


class WindowTitleBar(QWidget):
    """Minimal frameless drag strip with minimize / maximize / close."""

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("windowTitleBar")
        self.setFixedHeight(36)
        self._drag_pos: QPoint | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 6, 0)
        row.setSpacing(8)

        self._title = QLabel(title)
        self._title.setObjectName("windowTitleBarLabel")
        self._title.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self._title, stretch=1)

        self.btn_min = QPushButton("─")
        self.btn_max = QPushButton("□")
        self.btn_close = QPushButton("✕")
        for btn, name in (
            (self.btn_min, "windowCaptionButton"),
            (self.btn_max, "windowCaptionButton"),
            (self.btn_close, "windowCaptionCloseButton"),
        ):
            btn.setObjectName(name)
            btn.setFixedSize(36, 28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(btn)

        self.btn_min.clicked.connect(self.minimize_requested.emit)
        self.btn_max.clicked.connect(self.maximize_requested.emit)
        self.btn_close.clicked.connect(self.close_requested.emit)

    def set_title(self, title: str) -> None:
        self._title.setText(title)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._drag_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            host = self.window()
            if host is not None and not host.isMaximized():
                delta = event.globalPosition().toPoint() - self._drag_pos
                host.move(host.pos() + delta)
                self._drag_pos = event.globalPosition().toPoint()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
        super().mouseDoubleClickEvent(event)


def install_frameless_main_window(window: QMainWindow, *, title: str) -> WindowTitleBar:
    """
    Convert *window* to a frameless shell with a custom dark title bar.

    Existing ``centralWidget`` is preserved under the title strip.
    """
    window.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
    )
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    old_central = window.takeCentralWidget()
    shell = QWidget()
    shell.setObjectName("framelessShell")
    shell_layout = QVBoxLayout(shell)
    shell_layout.setContentsMargins(1, 1, 1, 1)
    shell_layout.setSpacing(0)

    title_bar = WindowTitleBar(title, shell)
    title_bar.minimize_requested.connect(window.showMinimized)
    title_bar.maximize_requested.connect(
        lambda: window.showNormal() if window.isMaximized() else window.showMaximized()
    )
    title_bar.close_requested.connect(window.close)
    shell_layout.addWidget(title_bar)

    if old_central is not None:
        shell_layout.addWidget(old_central, stretch=1)

    window.setCentralWidget(shell)
    polish_top_level_window(window)
    return title_bar


TITLE_BAR_STYLE = f"""
QWidget#framelessShell {{
    background-color: {BACKGROUND_PRIMARY};
    border: 1px solid {BORDER_DEFAULT};
}}
QWidget#windowTitleBar {{
    background-color: {BACKGROUND_PRIMARY};
    border-bottom: 1px solid {BORDER_DEFAULT};
}}
QLabel#windowTitleBarLabel {{
    color: {TEXT_PRIMARY};
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#windowCaptionButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
}}
QPushButton#windowCaptionButton:hover {{
    background-color: rgba(102, 192, 244, 0.18);
}}
QPushButton#windowCaptionCloseButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
}}
QPushButton#windowCaptionCloseButton:hover {{
    background-color: {ACCENT_ERROR};
    color: #ffffff;
}}
"""
