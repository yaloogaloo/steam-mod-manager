"""MainWindow taskbar icon — Qt vs Win32 HWND (after UI popup fixes)."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from ui.window_chrome import (
    app_icon_path,
    diagnose_and_bind_win32_taskbar_icon,
    install_frameless_main_window,
    read_win32_hwnd_icons,
)


def test_mainwindow_icon_remains_valid_after_ui_popup_fixes() -> None:
    """MainWindow icon remains valid after UI popup fixes."""
    path = app_icon_path()
    assert path.is_file(), f"missing icon: {path}"
    assert path.stat().st_size > 0

    app = QApplication.instance() or QApplication([])
    icon = QIcon(str(path))
    assert not icon.isNull()
    assert icon.availableSizes()

    window = QMainWindow()
    window.setCentralWidget(QLabel("probe"))
    window.setWindowIcon(icon)
    install_frameless_main_window(window, title="probe")
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.show()
    app.processEvents()

    if sys.platform == "win32":
        snap = diagnose_and_bind_win32_taskbar_icon(window)
        assert snap["hwnd"] != 0
        after = snap["after"]
        # Win32 path must leave a non-zero per-window icon (taskbar source).
        assert after["window_big"] or after["window_small"]
        # Re-read via helper to confirm WM_GETICON stickiness.
        hwnd_icons = read_win32_hwnd_icons(snap["hwnd"])
        assert hwnd_icons["window_big"] or hwnd_icons["window_small"]
    else:
        assert not window.windowIcon().isNull()

    window.close()
