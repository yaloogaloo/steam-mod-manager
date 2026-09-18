"""Unified Qt window ownership / lifecycle.

ARCHITECTURE RULE
-----------------
Import Mod once spawned orphan semi-transparent Qt floating windows because
controls (``QRadioButton`` / overlays) became visible while still parentless
— a free ``QHBoxLayout`` does **not** reparent until it is attached to a
widget. That accident is a Window ownership failure, not a stylesheet bug.

Forbidden forever:
- ``QDialog`` / ``QMessageBox`` / ``QProgressDialog`` without an explicit parent
- ``show()`` / ``setVisible(True)`` on control widgets while ``parent() is None``
- Local hide/close "workarounds" that paper over orphan top-level HWNDs
- Creating UI widgets from services or worker threads

Required:
- Every dialog has a clear parent (usually the calling view / main window)
- Every intentional top-level window is registered here
- Controls are parented (or their layout is on a parented widget) **before**
  any visibility change
"""

from __future__ import annotations

import logging
import weakref
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
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QToolButton,
    QWidget,
)

logger = logging.getLogger("window_lifecycle")

_ALLOWED_TOPLEVEL_TYPES = (
    QMainWindow,
    QDialog,
    QMessageBox,
    QProgressDialog,
    QMenu,
)

_CONTROL_TYPES = (
    QPushButton,
    QRadioButton,
    QLabel,
    QCheckBox,
    QToolButton,
    QFrame,
)

_ORIG_SHOW: Callable[..., Any] | None = None
_ORIG_SET_VISIBLE: Callable[..., Any] | None = None
_GUARD_STATE: WindowOwnershipState | None = None  # type: ignore[name-defined]
_REGISTERED: weakref.WeakSet[QWidget] = weakref.WeakSet()


class WindowOwnershipError(RuntimeError):
    """Raised when a dialog or top-level window violates ownership rules."""


class WindowOwnershipState(QObject):
    """Counters for the ownership guard (tests / diagnostics)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.blocked_count = 0
        self.suspect_count = 0
        self.on_blocked: Callable[[QWidget], None] | None = None


def require_dialog_parent(parent: QWidget | None, *, what: str = "QDialog") -> QWidget:
    """Every dialog must have an explicit parent — never ``None``."""
    if parent is None:
        raise WindowOwnershipError(
            f"{what} requires an explicit parent widget "
            f"(ARCHITECTURE RULE: no orphan Qt windows)"
        )
    return parent


def register_toplevel(window: QWidget) -> QWidget:
    """Mark an intentional top-level window as owned by the lifecycle layer."""
    if not isinstance(window, QWidget):
        raise TypeError("register_toplevel expects a QWidget")
    _REGISTERED.add(window)
    return window


def is_allowed_toplevel(widget: QWidget) -> bool:
    """True for intentional windows/popups — never for orphan controls."""
    if isinstance(widget, _CONTROL_TYPES):
        return False
    if widget in _REGISTERED:
        return True
    if isinstance(widget, _ALLOWED_TOPLEVEL_TYPES):
        return True
    try:
        wtype = widget.windowType()
    except Exception:  # noqa: BLE001
        return False
    if wtype in (
        Qt.WindowType.Popup,
        Qt.WindowType.ToolTip,
        Qt.WindowType.SplashScreen,
    ):
        return True
    if type(widget).__name__ in {"QTipLabel", "QComboBoxPrivateContainer"}:
        return True
    return False


def is_illegal_toplevel(widget: QWidget) -> bool:
    """Parentless control (or tiny parentless widget) must never be shown."""
    if widget.parent() is not None:
        return False
    if is_allowed_toplevel(widget):
        return False
    return True


def exec_dialog(dialog: QDialog) -> int:
    """Run a modal dialog that already has a parent; register if needed."""
    require_dialog_parent(dialog.parentWidget(), what=type(dialog).__name__)
    register_toplevel(dialog)
    return int(dialog.exec())


def describe_show_widget(widget: QWidget) -> str:
    text = ""
    try:
        if hasattr(widget, "text") and callable(widget.text):
            text = str(widget.text() or "")[:80]
    except Exception:  # noqa: BLE001
        text = ""
    geom = widget.geometry()
    return (
        f"class={type(widget).__name__} objectName={widget.objectName()!r} "
        f"text={text!r} parent={widget.parent()!r} "
        f"geom={geom.width()}x{geom.height()} "
        f"visible={widget.isVisible()} isWindow={widget.isWindow()}"
    )


def _block_illegal_show(widget: QWidget) -> bool:
    """Return True if show/setVisible(True) must be refused."""
    state = _GUARD_STATE
    if not is_illegal_toplevel(widget):
        return False
    state_ref = state
    if state_ref is not None:
        state_ref.suspect_count += 1
        state_ref.blocked_count += 1
    msg = (
        "ARCHITECTURE RULE: refused parentless control show "
        f"(orphan Qt float accident class) | {describe_show_widget(widget)}"
    )
    logger.warning(msg)
    if state_ref is not None and state_ref.on_blocked is not None:
        try:
            state_ref.on_blocked(widget)
        except Exception:  # noqa: BLE001
            pass
    return True


def install_window_ownership_guard(
    app: QApplication | None = None,
) -> WindowOwnershipState:
    """
    Enforce ownership: parentless controls must not become visible top-levels.

    This is lifecycle enforcement, not a post-hoc hide/close workaround.
    Safe to call more than once; installs show/setVisible wrappers once.
    """
    global _ORIG_SHOW, _ORIG_SET_VISIBLE, _GUARD_STATE
    application = app or QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication required")

    if _GUARD_STATE is not None:
        return _GUARD_STATE

    state = WindowOwnershipState(application)
    _GUARD_STATE = state
    application._window_ownership_state = state  # type: ignore[attr-defined]

    if _ORIG_SHOW is None:
        _ORIG_SHOW = QWidget.show
        _ORIG_SET_VISIBLE = QWidget.setVisible

        def _guarded_show(self: QWidget) -> None:
            if _block_illegal_show(self):
                return None
            assert _ORIG_SHOW is not None
            return _ORIG_SHOW(self)

        def _guarded_set_visible(self: QWidget, visible: bool) -> None:
            if visible and _block_illegal_show(self):
                return None
            assert _ORIG_SET_VISIBLE is not None
            return _ORIG_SET_VISIBLE(self, visible)

        for cls in (
            QWidget,
            QPushButton,
            QRadioButton,
            QLabel,
            QCheckBox,
            QToolButton,
            QFrame,
            QDialog,
            QMainWindow,
            QMessageBox,
            QProgressDialog,
        ):
            cls.show = _guarded_show  # type: ignore[method-assign, assignment]
            cls.setVisible = _guarded_set_visible  # type: ignore[method-assign, assignment]

    logger.info("window_ownership_guard installed")
    return state


def ownership_state() -> WindowOwnershipState | None:
    return _GUARD_STATE


def visible_illegal_toplevels() -> list[QWidget]:
    """Snapshot: visible parentless widgets that are not allowed windows."""
    app = QApplication.instance()
    if app is None:
        return []
    bad: list[QWidget] = []
    for widget in app.topLevelWidgets():
        if not widget.isVisible():
            continue
        if is_illegal_toplevel(widget):
            bad.append(widget)
            continue
        # Allowed types are fine; also flag visible parentless controls
        # that somehow remain in the top-level list.
        if isinstance(widget, _CONTROL_TYPES) and widget.parent() is None:
            bad.append(widget)
    return bad
