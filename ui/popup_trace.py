"""Diagnostic-only: log temporary popup / tooltip / top-level Show events.

Install via ``install_popup_trace()``. Does not change behavior.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QLabel, QToolTip, QWidget

logger = logging.getLogger("popup_trace")

_INSTALLED = False
_ORIG_SHOW_TEXT = None


def _caller_summary(depth: int = 6) -> str:
    frames = traceback.extract_stack(limit=depth + 4)[:-2]
    parts: list[str] = []
    for fr in frames[-depth:]:
        parts.append(f"{fr.filename}:{fr.lineno}:{fr.name}")
    return " <- ".join(parts)


def log_popup(widget_type: str, *, detail: str = "", caller: str | None = None) -> None:
    """Minimal POPUP CREATED breadcrumb — no-op unless ``ui_trace`` is on."""
    try:
        from core.debug_config import ui_trace_enabled

        if not ui_trace_enabled():
            return
    except Exception:  # noqa: BLE001
        return
    logger.warning(
        "POPUP CREATED | type=%s | detail=%s | caller=%s",
        widget_type,
        detail or "-",
        caller or _caller_summary(),
    )


def _describe_widget(w: QWidget) -> str:
    name = w.objectName() or ""
    cls = type(w).__name__
    geom = w.geometry()
    tip = ""
    try:
        tip = (w.toolTip() or "")[:80]
    except Exception:  # noqa: BLE001
        tip = ""
    text = ""
    if isinstance(w, QLabel):
        text = (w.text() or "")[:80]
    flags = int(w.windowFlags())
    return (
        f"cls={cls} objectName={name!r} visible={w.isVisible()} "
        f"geom={geom.width()}x{geom.height()}+{geom.x()}+{geom.y()} "
        f"toplevel={w.isWindow()} flags=0x{flags:x} "
        f"text={text!r} tooltip={tip!r}"
    )


class PopupTraceFilter(QObject):
    """Log Show / ToolTip events for top-level or tip-like widgets."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        et = event.type()
        if et == QEvent.Type.ToolTip:
            log_popup(
                "QEvent.ToolTip",
                detail=f"watched={type(watched).__name__} name={getattr(watched, 'objectName', lambda: '')()}",
            )
        if et == QEvent.Type.Show and isinstance(watched, QWidget):
            # Tip / floating / parentless windows are the bug suspects.
            is_tip = type(watched).__name__ in {"QTipLabel", "QLabel"}
            if watched.isWindow() or is_tip or watched.parent() is None:
                log_popup(
                    f"QWidget.Show:{type(watched).__name__}",
                    detail=_describe_widget(watched),
                )
        return super().eventFilter(watched, event)


def _traced_show_text(*args: Any, **kwargs: Any) -> None:
    log_popup("QToolTip.showText", detail=repr(args[:3]))
    assert _ORIG_SHOW_TEXT is not None
    return _ORIG_SHOW_TEXT(*args, **kwargs)


def install_popup_trace(app: QApplication | None = None) -> PopupTraceFilter:
    """Install app-wide Show/ToolTip tracing + wrap QToolTip.showText."""
    from core.debug_config import ui_trace_enabled

    if not ui_trace_enabled():
        application = app or QApplication.instance()
        if application is None:
            raise RuntimeError("QApplication required")
        return PopupTraceFilter(application)
    global _INSTALLED, _ORIG_SHOW_TEXT
    application = app or QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication required")
    filt = PopupTraceFilter(application)
    application.installEventFilter(filt)
    if not _INSTALLED:
        _ORIG_SHOW_TEXT = QToolTip.showText
        QToolTip.showText = staticmethod(_traced_show_text)  # type: ignore[method-assign, assignment]
        _INSTALLED = True
    log_popup("popup_trace.installed", detail="eventFilter+QToolTip.showText wrap")
    return filt
