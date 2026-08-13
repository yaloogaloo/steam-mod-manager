"""Frameless main window keeps taskbar min/restore Win32 styles."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from ui.window_chrome import (
    install_frameless_main_window,
    on_frameless_main_window_shown,
    read_win32_frame_styles,
)


def test_frameless_keeps_taskbar_sysmenu_styles() -> None:
    """Frameless chrome must keep WS_MINIMIZEBOX/SYSMENU (no caption)."""
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setCentralWidget(QLabel("probe"))
    install_frameless_main_window(window, title="probe")
    assert not window.isVisible()
    window.show()
    on_frameless_main_window_shown(window)
    app.processEvents()

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert window.windowFlags() & Qt.WindowType.WindowSystemMenuHint
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    if sys.platform == "win32":
        bits = read_win32_frame_styles(int(window.winId()))
        assert bits["WS_SYSMENU"]
        assert bits["WS_MINIMIZEBOX"]
        assert bits["WS_MAXIMIZEBOX"]
        assert not bits["WS_CAPTION"]

    window.close()


def test_install_frameless_does_not_show_or_force_hwnd_before_show() -> None:
    """install_frameless must not make the window visible before show()."""
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setCentralWidget(QLabel("probe"))
    install_frameless_main_window(window, title="probe")
    assert not window.isVisible()
    assert int(window.internalWinId() or 0) == 0
    window.close()
