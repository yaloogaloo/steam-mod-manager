"""Startup lifecycle diagnostics — locate black-screen flash (no fixes here)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QPaintEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QMainWindow, QWidget

if TYPE_CHECKING:
    pass

_T0: float | None = None
_APP_CREATED_AT: float | None = None


def _now_ms() -> float:
    global _T0
    t = time.perf_counter()
    if _T0 is None:
        _T0 = t
    return (t - _T0) * 1000.0


def reset_startup_timeline() -> None:
    global _T0, _APP_CREATED_AT
    _T0 = time.perf_counter()
    _APP_CREATED_AT = _T0


def mark_qapplication_created() -> None:
    global _APP_CREATED_AT
    _APP_CREATED_AT = time.perf_counter()
    if _T0 is None:
        reset_startup_timeline()
    log_startup("QApplication created")


def log_startup(message: str) -> None:
    try:
        from core.debug_config import ui_trace_enabled, performance_log_enabled

        if not ui_trace_enabled() and not performance_log_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    print(f"[startup-timeline] +{_now_ms():.1f}ms {message}", flush=True)


def _hwnd_hint(widget: QWidget) -> str:
    try:
        wid = int(widget.internalWinId() or 0)
        if wid:
            return f"hwnd=0x{wid:x}"
    except Exception:  # noqa: BLE001
        pass
    try:
        wid = int(widget.winId())
        return f"hwnd=0x{wid:x}"
    except Exception:  # noqa: BLE001
        return "hwnd=none"


def _translucent_hint(widget: QWidget) -> bool:
    try:
        return bool(widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
    except Exception:  # noqa: BLE001
        return False


def log_window_event(widget: QWidget, event_name: str) -> None:
    """First paint/resize/show on MainWindow — full geometry snapshot."""
    try:
        from core.debug_config import ui_trace_enabled, performance_log_enabled

        if not ui_trace_enabled() and not performance_log_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    geom = widget.geometry()
    frame = widget.frameGeometry()
    print(
        f"[WINDOW-EVENT] event={event_name} "
        f"timestamp=+{_now_ms():.1f}ms "
        f"size={widget.width()}x{widget.height()} "
        f"geometry={geom.x()},{geom.y()},{geom.width()}x{geom.height()} "
        f"frame={frame.x()},{frame.y()},{frame.width()}x{frame.height()} "
        f"windowState={widget.windowState()!r} "
        f"isVisible={widget.isVisible()} "
        f"translucent={_translucent_hint(widget)} "
        f"{_hwnd_hint(widget)}",
        flush=True,
    )


class StartupLifecycleMixin:
    """Mixin for MainWindow — log first paint/resize/show only."""

    _logged_paint: bool = False
    _logged_resize: bool = False
    _logged_show: bool = False

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if not self._logged_paint:
            self._logged_paint = True
            log_window_event(self, "paint")
        super().paintEvent(event)  # type: ignore[misc]

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        if not self._logged_resize:
            self._logged_resize = True
            log_window_event(self, "resize")
        super().resizeEvent(event)  # type: ignore[misc]

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        if not self._logged_show:
            self._logged_show = True
            log_window_event(self, "show")
            log_startup("MainWindow first showEvent")
        super().showEvent(event)  # type: ignore[misc]


def dump_startup_surface_audit(window: QMainWindow) -> None:
    """Post-show snapshot: attrs + screen geometry."""
    try:
        from core.debug_config import ui_trace_enabled, performance_log_enabled

        if not ui_trace_enabled() and not performance_log_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    screens = window.screen()
    screen_geom = screens.availableGeometry() if screens else None
    sg = (
        f"{screen_geom.width()}x{screen_geom.height()}"
        if screen_geom is not None
        else "?"
    )
    print(
        f"[startup-audit] post-show "
        f"size={window.width()}x{window.height()} "
        f"frame={window.frameGeometry().width()}x{window.frameGeometry().height()} "
        f"screen_available={sg} "
        f"maximized={window.isMaximized()} "
        f"fullscreen={window.isFullScreen()} "
        f"translucent={_translucent_hint(window)} "
        f"opacity={window.windowOpacity():.2f} "
        f"{_hwnd_hint(window)}",
        flush=True,
    )
