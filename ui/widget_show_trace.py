"""Global QWidget show/setVisible tracer — catch parentless control flashes.

Install via ``install_widget_show_trace(app)`` from ``launch_gui``.

Wraps ``QWidget.show`` / ``QWidget.setVisible`` and logs
``TOP LEVEL CHILD SUSPECT`` when a control becomes visible without a parent.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QWidget,
)

logger = logging.getLogger("widget_show_trace")

_ORIG_SHOW: Callable[..., Any] | None = None
_ORIG_SET_VISIBLE: Callable[..., Any] | None = None
_STATE: WidgetShowTraceState | None = None  # type: ignore[name-defined]

_ALLOWED_TOPLEVEL_TYPES = (
    QMainWindow,
    QDialog,
    QMessageBox,
    QMenu,
)

_CONTROL_TYPES = (
    QPushButton,
    QLabel,
    QCheckBox,
    QToolButton,
    QFrame,
)


def _stack_text(limit: int = 12) -> str:
    frames = traceback.extract_stack(limit=limit + 8)[:-2]
    lines: list[str] = []
    for fr in frames[-limit:]:
        if "widget_show_trace.py" in (fr.filename or ""):
            continue
        lines.append(f"  {fr.filename}:{fr.lineno} in {fr.name}")
    return "\n".join(lines[-limit:])


def _widget_text(w: QWidget) -> str:
    try:
        if hasattr(w, "text") and callable(w.text):
            return str(w.text() or "")[:80]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _is_allowed_toplevel(w: QWidget) -> bool:
    """True for intentional windows/popups — never for orphan controls."""
    # Control widgets shown without a parent are never "allowed".
    if isinstance(w, _CONTROL_TYPES):
        return False
    if isinstance(w, _ALLOWED_TOPLEVEL_TYPES):
        return True
    # Use windowType() — do NOT bitwise-test Popup against Window flags;
    # Qt Popup includes the Window bit so every QWidget window would match.
    try:
        wtype = w.windowType()
    except Exception:  # noqa: BLE001
        return False
    if wtype in (
        Qt.WindowType.Popup,
        Qt.WindowType.ToolTip,
        Qt.WindowType.SplashScreen,
    ):
        return True
    if type(w).__name__ in {"QTipLabel", "QComboBoxPrivateContainer"}:
        return True
    return False


def geom_is_tiny(w: QWidget, *, max_side: int = 48) -> bool:
    return int(w.width()) <= max_side and int(w.height()) <= max_side


def describe_show_widget(w: QWidget) -> str:
    geom = w.geometry()
    return (
        f"class={type(w).__name__} objectName={w.objectName()!r} "
        f"text={_widget_text(w)!r} parent={w.parent()!r} "
        f"flags=0x{int(w.windowFlags()):x} "
        f"geom={geom.width()}x{geom.height()}+{geom.x()}+{geom.y()} "
        f"visible={w.isVisible()} toplevel={w.isWindow()} "
        f"isWindow={w.isWindow()}"
    )


class WidgetShowTraceState(QObject):
    """Mutable counters shared by the show/setVisible hooks."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.suspect_count = 0
        self.on_suspect: Callable[[QWidget], None] | None = None


def _active_state() -> WidgetShowTraceState | None:
    return _STATE


def _probe_becoming_visible(w: QWidget) -> None:
    state = _active_state()
    if state is None:
        return
    detail = describe_show_widget(w)
    logger.info("QWidget.Show %s", detail)

    if w.parent() is not None:
        return
    if _is_allowed_toplevel(w):
        return

    state.suspect_count += 1
    stack = _stack_text()
    msg = (
        f"=== TOP LEVEL CHILD SUSPECT ===\n"
        f"{detail}\n"
        f"stack:\n{stack}"
    )
    logger.warning(msg)
    try:
        from core.debug_config import ui_trace_enabled

        if ui_trace_enabled():
            print(msg, flush=True)
    except Exception:  # noqa: BLE001
        pass
    if state.on_suspect is not None:
        try:
            state.on_suspect(w)
        except Exception:  # noqa: BLE001
            pass

    if isinstance(w, _CONTROL_TYPES) or (w.isWindow() and geom_is_tiny(w)):
        warn = (
            f"Illegal top level widget detected: "
            f"{type(w).__name__} objectName={w.objectName()!r} "
            f"size={w.width()}x{w.height()}"
        )
        logger.warning(warn)
        try:
            from core.debug_config import ui_trace_enabled

            if ui_trace_enabled():
                print(f"[widget-show-trace] {warn}", flush=True)
        except Exception:  # noqa: BLE001
            pass


def install_widget_show_trace(
    app: QApplication | None = None,
) -> WidgetShowTraceState:
    """Install once; wrap QWidget.show / setVisible for parentless detection."""
    from core.debug_config import ui_trace_enabled

    global _ORIG_SHOW, _ORIG_SET_VISIBLE, _STATE
    application = app or QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication required")

    if _STATE is not None:
        return _STATE

    state = WidgetShowTraceState(application)
    _STATE = state
    application._widget_show_trace_state = state  # type: ignore[attr-defined]

    if _ORIG_SHOW is None:
        _ORIG_SHOW = QWidget.show
        _ORIG_SET_VISIBLE = QWidget.setVisible

        def _traced_show(self: QWidget) -> None:
            _probe_becoming_visible(self)
            assert _ORIG_SHOW is not None
            return _ORIG_SHOW(self)

        def _traced_set_visible(self: QWidget, visible: bool) -> None:
            if visible:
                _probe_becoming_visible(self)
            assert _ORIG_SET_VISIBLE is not None
            return _ORIG_SET_VISIBLE(self, visible)

        # Patch concrete control classes too — PySide binds QPushButton.show to
        # the C++ method, so assigning only QWidget.show is not enough.
        for cls in (
            QWidget,
            QPushButton,
            QLabel,
            QCheckBox,
            QToolButton,
            QFrame,
            QDialog,
            QMainWindow,
            QMessageBox,
        ):
            cls.show = _traced_show  # type: ignore[method-assign, assignment]
            cls.setVisible = _traced_set_visible  # type: ignore[method-assign, assignment]

    logger.info("widget_show_trace installed")
    if ui_trace_enabled():
        print("[widget-show-trace] installed (show/setVisible guard)", flush=True)
    return state
