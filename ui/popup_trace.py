"""Diagnostic-only: log temporary popup / tooltip / top-level Show events.

Does not change behavior. Enable with ``ui_trace``.
"""

from __future__ import annotations

import logging
import traceback

logger = logging.getLogger("popup_trace")


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
