"""Window chrome helpers — app icon, dark titlebar, frameless title strip."""

from __future__ import annotations

import ctypes
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
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

# Win32 icon / Shell diagnostics (taskbar uses HWND icons, not Qt alone).
_WM_SETICON = 0x0080
_WM_GETICON = 0x007F
_ICON_SMALL = 0
_ICON_BIG = 1
_GCLP_HICON = -14
_GCLP_HICONSM = -34
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_APP_USER_MODEL_ID = "SteamModManager.WorkshopLibrary"

# Taskbar minimize/restore (do not swallow these).
_WM_SYSCOMMAND = 0x0112
_SC_SIZE = 0xF000
_SC_MOVE = 0xF010
_SC_MINIMIZE = 0xF020
_SC_MAXIMIZE = 0xF030
_SC_CLOSE = 0xF060
_SC_RESTORE = 0xF120
_SC_MASK = 0xFFF0
_GWL_STYLE = -16
_WS_SYSMENU = 0x00080000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_WS_CAPTION = 0x00C00000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_SC_NAMES = {
    _SC_SIZE: "SC_SIZE",
    _SC_MOVE: "SC_MOVE",
    _SC_MINIMIZE: "SC_MINIMIZE",
    _SC_MAXIMIZE: "SC_MAXIMIZE",
    _SC_CLOSE: "SC_CLOSE",
    _SC_RESTORE: "SC_RESTORE",
}

# Frameless + taskbar toggle (no native caption bar).
FRAMELESS_MAIN_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.Window
    | Qt.WindowType.WindowSystemMenuHint
    | Qt.WindowType.WindowMinimizeButtonHint
    | Qt.WindowType.WindowMaximizeButtonHint
)

_window_init_t0: float | None = None


def log_window_init(message: str) -> None:
    """Startup timeline probe for black-flash / init-order debugging."""
    try:
        from core.debug_config import ui_trace_enabled, performance_log_enabled

        if not ui_trace_enabled() and not performance_log_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    global _window_init_t0
    import time

    now = time.perf_counter()
    if _window_init_t0 is None:
        _window_init_t0 = now
    print(f"[window-init] +{(now - _window_init_t0) * 1000:.1f}ms {message}")


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
    if icon.isNull():
        return
    app.setWindowIcon(icon)
    # Windows Shell groups taskbar buttons by AppUserModelID; set early so
    # frameless HWND recreation still maps to this process icon identity.
    if sys.platform == "win32":
        try:
            shell32 = ctypes.windll.shell32
            shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [
                ctypes.c_wchar_p
            ]
            shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
            shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
        except Exception:  # noqa: BLE001
            pass


def _widget_hwnd(widget: QWidget) -> int:
    try:
        return int(widget.winId())
    except Exception:  # noqa: BLE001
        return 0


def get_app_user_model_id() -> str:
    """Return the process AppUserModelID, or empty string if unset / unavailable."""
    if sys.platform != "win32":
        return ""
    try:
        shell32 = ctypes.windll.shell32
        ptr = ctypes.c_wchar_p()
        shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [
            ctypes.POINTER(ctypes.c_wchar_p)
        ]
        shell32.GetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
        hr = shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return ""
        value = str(ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        return value
    except Exception:  # noqa: BLE001
        return ""


def read_win32_hwnd_icons(hwnd: int) -> dict[str, int]:
    """Read class + per-window icon handles for *hwnd* (0 = unset)."""
    empty = {
        "class_big": 0,
        "class_small": 0,
        "window_big": 0,
        "window_small": 0,
    }
    if sys.platform != "win32" or not hwnd:
        return empty
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get_class = user32.GetClassLongPtrW
        get_class.restype = ctypes.c_void_p
    else:
        get_class = user32.GetClassLongW
        get_class.restype = ctypes.c_void_p
    user32.SendMessageW.restype = ctypes.c_void_p
    class_big = int(get_class(hwnd, _GCLP_HICON) or 0)
    class_small = int(get_class(hwnd, _GCLP_HICONSM) or 0)
    window_big = int(
        user32.SendMessageW(hwnd, _WM_GETICON, _ICON_BIG, 0) or 0
    )
    window_small = int(
        user32.SendMessageW(hwnd, _WM_GETICON, _ICON_SMALL, 0) or 0
    )
    return {
        "class_big": class_big,
        "class_small": class_small,
        "window_big": window_big,
        "window_small": window_small,
    }


def load_win32_hicons_from_app_ico() -> tuple[int, int]:
    """Load ICON_BIG / ICON_SMALL HICONs from ``resources/app.ico`` via Win32."""
    if sys.platform != "win32":
        return 0, 0
    path = app_icon_path()
    if not path.is_file():
        return 0, 0
    user32 = ctypes.windll.user32
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.LoadImageW.restype = ctypes.c_void_p
    path_s = str(path)
    h_big = int(
        user32.LoadImageW(None, path_s, _IMAGE_ICON, 32, 32, _LR_LOADFROMFILE)
        or 0
    )
    h_small = int(
        user32.LoadImageW(None, path_s, _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)
        or 0
    )
    return h_big, h_small


def bind_win32_window_icons(hwnd: int, hicon_big: int, hicon_small: int) -> None:
    """Bind HICONs to *hwnd* with ``WM_SETICON`` (not Qt ``setWindowIcon``)."""
    if sys.platform != "win32" or not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.SendMessageW.restype = ctypes.c_void_p
    if hicon_big:
        user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, hicon_big)
    if hicon_small:
        user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, hicon_small)


def diagnose_and_bind_win32_taskbar_icon(widget: QWidget) -> dict:
    """Temporary startup probe: Qt vs Win32 HWND icons vs AppUserModelID.

    If Win32 HWND icons are empty, bind ``app.ico`` via ``WM_SETICON`` only
    (no Qt ``setWindowIcon``). Returns a snapshot for logs / tests.
    """
    qt_icon = widget.windowIcon()
    if qt_icon.isNull():
        qt_icon = app_icon()
    sizes = [f"{s.width()}x{s.height()}" for s in qt_icon.availableSizes()]
    hwnd = _widget_hwnd(widget)
    aumid = get_app_user_model_id()
    before = read_win32_hwnd_icons(hwnd)
    class_name = ""
    if sys.platform == "win32" and hwnd:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        class_name = buf.value

    probe_big, probe_small = load_win32_hicons_from_app_ico()

    try:
        from core.debug_config import ui_trace_enabled

        _print = print if ui_trace_enabled() else (lambda *a, **k: None)
    except Exception:  # noqa: BLE001
        _print = lambda *a, **k: None  # noqa: E731

    _print("[taskbar-icon] Qt icon:")
    _print(f"  isNull={qt_icon.isNull()}")
    _print(f"  availableSizes={sizes}")
    _print("[taskbar-icon] Win32:")
    _print(f"  HWND={hwnd:#x}" if hwnd else "  HWND=0")
    _print(f"  className={class_name!r}")
    _print(f"  GetClassLongPtr GCLP_HICON     (big)  ={before['class_big']:#x}")
    _print(f"  GetClassLongPtr GCLP_HICONSM   (small)={before['class_small']:#x}")
    _print(f"  WM_GETICON ICON_BIG                  ={before['window_big']:#x}")
    _print(f"  WM_GETICON ICON_SMALL                ={before['window_small']:#x}")
    _print(f"  LoadImage(app.ico) big={probe_big:#x} small={probe_small:#x}")
    _print(f"  AppUserModelID={aumid!r} (expected={_APP_USER_MODEL_ID!r})")
    if aumid and aumid != _APP_USER_MODEL_ID:
        _print(
            "[taskbar-icon] WARNING: AppUserModelID differs from expected — "
            "Shell may group this HWND with another app's icon cache entry."
        )

    bound = False
    # Taskbar paints from per-window icons; class icons are often 0 on Qt HWNDs.
    win_icons_empty = not (before["window_big"] or before["window_small"])
    class_icons_empty = not (before["class_big"] or before["class_small"])
    if hwnd and win_icons_empty:
        _print(
            f"[taskbar-icon] Win32 HWND icons empty "
            f"(class_empty={class_icons_empty}); binding via WM_SETICON"
        )
        h_big, h_small = probe_big, probe_small
        if h_big or h_small:
            # Keep handles alive for process lifetime (do not DestroyIcon).
            widget.setProperty("_win32_hicon_big", h_big)
            widget.setProperty("_win32_hicon_small", h_small)
            bind_win32_window_icons(hwnd, h_big, h_small)
            bound = True
            probe_big = probe_small = 0  # ownership transferred to window
        after = read_win32_hwnd_icons(hwnd)
        _print("[taskbar-icon] Win32 after WM_SETICON:")
        _print(f"  WM_GETICON ICON_BIG   ={after['window_big']:#x}")
        _print(f"  WM_GETICON ICON_SMALL ={after['window_small']:#x}")
        _print(f"  GCLP_HICON            ={after['class_big']:#x}")
        _print(f"  GCLP_HICONSM          ={after['class_small']:#x}")
        snap_after = after
    else:
        snap_after = before
        if hwnd and not win_icons_empty:
            _print(
                "[taskbar-icon] Win32 HWND icons already set. "
                "If the taskbar is still blank/black → Explorer icon cache "
                "(not Qt / not empty HWND). See cache reset steps in console "
                "footer."
            )
            _print(
                "[taskbar-icon] cache-check:\n"
                "  1) ie4uinit.exe -show\n"
                "  2) taskkill /f /im explorer.exe && "
                "del /a /q \"%LOCALAPPDATA%\\IconCache.db\" && "
                "del /a /f /q \"%LOCALAPPDATA%\\Microsoft\\Windows\\"
                "Explorer\\iconcache*\" && start explorer.exe\n"
                "  3) fully quit this app, relaunch, re-check taskbar"
            )

    # Destroy probe LoadImage handles if we did not hand them to the HWND.
    if sys.platform == "win32":
        if probe_big:
            ctypes.windll.user32.DestroyIcon(probe_big)
        if probe_small:
            ctypes.windll.user32.DestroyIcon(probe_small)

    return {
        "hwnd": hwnd,
        "class_name": class_name,
        "qt_null": bool(qt_icon.isNull()),
        "qt_sizes": sizes,
        "app_user_model_id": aumid,
        "before": before,
        "after": snap_after,
        "bound_via_wm_seticon": bound,
    }


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


def polish_top_level_window(widget: QWidget, *, native: bool = True) -> None:
    """Icon (+ optional dark native titlebar) for dialogs / main window.

    Always re-apply ``setWindowIcon`` — after ``setWindowFlags`` (e.g. frameless)
    Qt may still report a non-null inherited icon while the Win32 Shell handle
    is stale, which shows up as a blank/black taskbar icon.

    Pass ``native=False`` before the first ``show()`` so we do not call
    ``winId()`` early (that + ``SetWindowPos(SWP_FRAMECHANGED)`` can flash a
    black fullscreen HWND on Windows).
    """
    icon = app_icon()
    if not icon.isNull():
        widget.setWindowIcon(icon)
    if native:
        enable_windows_dark_titlebar(widget)


def apply_frameless_main_window_flags(window: QMainWindow) -> None:
    """Apply frameless flags once — must run before the first ``show()``."""
    window.setWindowFlags(FRAMELESS_MAIN_FLAGS)
    # Never translucent on the main HWND — breaks Windows taskbar icon paint.
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    log_window_init(
        f"flags applied frameless={bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)} "
        f"minHint={bool(window.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint)} "
        f"sysMenu={bool(window.windowFlags() & Qt.WindowType.WindowSystemMenuHint)}"
    )


class DarkTitleBarFilter(QObject):
    """Apply dark titlebar + app icon when top-level windows are shown."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(
            watched, (QDialog, QMainWindow, QMessageBox)
        ):
            polish_top_level_window(watched)
        return super().eventFilter(watched, event)


class SysCommandProbeFilter(QAbstractNativeEventFilter):
    """Temporary: log WM_SYSCOMMAND (taskbar click → SC_MINIMIZE / SC_RESTORE).

    Does not consume messages — always returns False so Qt/DefWindowProc run.
    """

    def __init__(self, window: QWidget | None = None) -> None:
        super().__init__()
        self._window = window

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        try:
            et = bytes(eventType) if not isinstance(eventType, bytes) else eventType
        except Exception:  # noqa: BLE001
            et = b""
        if et not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False
        if sys.platform != "win32":
            return False
        try:
            # MSG* passed as sip.voidptr / int
            addr = int(message)
            # MSG: hwnd, message, wParam, lParam, time, pt
            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t),
                    ("lParam", ctypes.c_size_t),
                    ("time", ctypes.c_uint),
                    ("pt_x", ctypes.c_long),
                    ("pt_y", ctypes.c_long),
                ]

            msg = MSG.from_address(addr)
        except Exception:  # noqa: BLE001
            return False
        if msg.message != _WM_SYSCOMMAND:
            return False
        cmd = int(msg.wParam) & _SC_MASK
        name = _SC_NAMES.get(cmd, f"SC_0x{cmd:04X}")
        state = "?"
        win = self._window
        if win is not None:
            try:
                state = str(win.windowState())
            except Exception:  # noqa: BLE001
                state = "?"
        try:
            from core.debug_config import ui_trace_enabled

            if ui_trace_enabled():
                print(
                    f"[syscommand] WM_SYSCOMMAND wParam=0x{int(msg.wParam):X} "
                    f"cmd={name} (0x{cmd:X}) windowState={state} hwnd={msg.hwnd}"
                )
        except Exception:  # noqa: BLE001
            pass
        return False


def install_syscommand_probe(app: QApplication, window: QWidget) -> SysCommandProbeFilter:
    """Install temporary WM_SYSCOMMAND logger; keep returned filter alive."""
    probe = SysCommandProbeFilter(window)
    app.installNativeEventFilter(probe)
    app.setProperty("_syscommand_probe", probe)
    try:
        from core.debug_config import ui_trace_enabled

        if ui_trace_enabled():
            print("[syscommand] probe installed (WM_SYSCOMMAND passthrough logger)")
    except Exception:  # noqa: BLE001
        pass
    return probe


def read_win32_frame_styles(hwnd: int) -> dict[str, bool]:
    """Whether taskbar-relevant WS_* bits are present (no caption required)."""
    empty = {
        "WS_SYSMENU": False,
        "WS_MINIMIZEBOX": False,
        "WS_MAXIMIZEBOX": False,
        "WS_CAPTION": False,
    }
    if sys.platform != "win32" or not hwnd:
        return empty
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        style = int(user32.GetWindowLongPtrW(hwnd, _GWL_STYLE) or 0)
    else:
        style = int(user32.GetWindowLongW(hwnd, _GWL_STYLE) or 0)
    return {
        "WS_SYSMENU": bool(style & _WS_SYSMENU),
        "WS_MINIMIZEBOX": bool(style & _WS_MINIMIZEBOX),
        "WS_MAXIMIZEBOX": bool(style & _WS_MAXIMIZEBOX),
        "WS_CAPTION": bool(style & _WS_CAPTION),
    }


def ensure_frameless_taskbar_sysmenu(window: QWidget) -> None:
    """Keep frameless chrome but restore WS_SYSMENU / MIN / MAX for taskbar.

    ``FramelessWindowHint`` alone drops these bits; Explorer then cannot
    toggle minimize/restore via the taskbar button. Does not set WS_CAPTION.
    """
    if sys.platform != "win32":
        return
    hwnd = _widget_hwnd(window)
    if not hwnd:
        hwnd = int(window.winId())
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
    else:
        get_long = user32.GetWindowLongW
        set_long = user32.SetWindowLongW
    style = int(get_long(hwnd, _GWL_STYLE) or 0)
    needed = _WS_SYSMENU | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX
    new_style = (style | needed) & ~_WS_CAPTION
    if new_style == style:
        return
    set_long(hwnd, _GWL_STYLE, new_style)
    user32.SetWindowPos(
        hwnd,
        0,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE
        | _SWP_NOSIZE
        | _SWP_NOZORDER
        | _SWP_NOACTIVATE
        | _SWP_FRAMECHANGED,
    )
    bits = read_win32_frame_styles(hwnd)
    try:
        from core.debug_config import ui_trace_enabled

        if ui_trace_enabled():
            print(
                f"[syscommand] ensure styles hwnd={hwnd:#x} "
                f"SYSMENU={bits['WS_SYSMENU']} MINBOX={bits['WS_MINIMIZEBOX']} "
                f"MAXBOX={bits['WS_MAXIMIZEBOX']} CAPTION={bits['WS_CAPTION']}"
            )
    except Exception:  # noqa: BLE001
        pass


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


def install_frameless_main_window(
    window: QMainWindow,
    *,
    title: str,
    flags_already_applied: bool = False,
) -> WindowTitleBar:
    """
    Convert *window* to a frameless shell with a custom dark title bar.

    Existing ``centralWidget`` is preserved under the title strip.

    Does **not** create a native HWND (no ``winId`` / ``SetWindowPos``) so this
    stays safe to call before ``show()``. Taskbar WS_* bits come from
    ``FRAMELESS_MAIN_FLAGS``; ``ensure_frameless_taskbar_sysmenu`` runs on show.
    """
    if not flags_already_applied:
        apply_frameless_main_window_flags(window)
    # setWindowFlags recreates the native window; re-bind icon immediately.
    icon = app_icon()
    if not icon.isNull():
        window.setWindowIcon(icon)
    log_window_init(
        f"icon applied isNull={window.windowIcon().isNull()} "
        f"sizes={len(window.windowIcon().availableSizes())}"
    )

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
    # Icon only — avoid winId() before show (black flash).
    polish_top_level_window(window, native=False)
    return title_bar


def on_frameless_main_window_shown(window: QMainWindow) -> None:
    """Post-show native polish: dark titlebar + taskbar WS_* if still missing.

    Safe after ``show()``: Qt flags usually already set min/sysmenu bits, so
    ``ensure_frameless_taskbar_sysmenu`` is a no-op (no ``SetWindowPos``).
    """
    ensure_frameless_taskbar_sysmenu(window)
    enable_windows_dark_titlebar(window)
    log_window_init(
        f"native polish after show visible={window.isVisible()} "
        f"state={window.windowState()!r}"
    )


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
