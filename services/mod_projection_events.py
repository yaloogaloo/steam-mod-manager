"""Mod projection invalidation — single event for Library display sync.

ARCHITECTURE RULE
-----------------
After any mutation that changes Mod **display** data in SQLite:

    notify_mod_changed(internal_id)

That call:

1. ``refresh_projection(internal_id)`` — re-read the full Layer-1 row into warm cache
   (runs on the caller thread; cache/DB only — never QWidget)
2. Notify LibraryView to rebind Viewport from the replaced ``ModCardData``
   (**always on the Qt GUI thread**)

Forbidden: field-level notify (cover/offline/deploy/name), full Library refresh,
filesystem scan, or reconcile inside this path.

Thread model: worker threads (ImportWorker / ModRefreshWorker / FS L1 / deploy)
must never invoke UI listeners inline. Dispatch uses a QObject signal with
``Qt.QueuedConnection`` so ``LibraryView.on_mod_changed`` / ``show_mod`` cannot
run off the GUI thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

ModChangedListener = Callable[[str], None]

_LOCK = threading.Lock()
_LISTENERS: list[ModChangedListener] = []
_BRIDGE: Any = None


def subscribe_mod_changed(listener: ModChangedListener) -> None:
    with _LOCK:
        if listener not in _LISTENERS:
            _LISTENERS.append(listener)


def unsubscribe_mod_changed(listener: ModChangedListener) -> None:
    with _LOCK:
        try:
            _LISTENERS.remove(listener)
        except ValueError:
            pass


def reset_mod_changed_listeners() -> None:
    """Test helper — clear all projection listeners."""
    with _LOCK:
        _LISTENERS.clear()


def notify_mods_changed(internal_ids: Iterable[str | int]) -> None:
    """Batch projection refresh + UI notify. One coalesced Library pass."""
    seen: list[str] = []
    for raw in internal_ids:
        mid = str(raw or "").strip()
        if mid and mid not in seen:
            seen.append(mid)
    if not seen:
        return
    try:
        from services.mod_library_cache import get_library_cache

        cache = get_library_cache()
        for mid in seen:
            try:
                cache.refresh_projection(mid)
            except Exception:  # noqa: BLE001
                logger.debug("refresh_projection failed internal_id=%s", mid, exc_info=True)
    except Exception:  # noqa: BLE001
        from services.crash_trace import log_exception

        log_exception("notify_mods_changed.refresh_projection")
    if _should_marshal_to_gui():
        bridge = _gui_bridge()
        if bridge is None:
            return
        for mid in seen:
            bridge.changed.emit(mid)
        return
    for mid in seen:
        _invoke_listeners(mid)


def notify_mod_changed(internal_id: str | int) -> None:
    """
    Invalidate and refresh one Mod's Library Projection after DB commit.

    Callers must pass the internal Mod id only — never a field list.

    ``refresh_projection`` stays synchronous on the caller thread.
    UI listeners are marshaled onto the Qt GUI thread when a QApplication
    exists and the caller is not already that thread.
    """
    mid = str(internal_id or "").strip()
    if not mid:
        return
    try:
        from services.mod_library_cache import get_library_cache

        get_library_cache().refresh_projection(mid)
    except Exception:  # noqa: BLE001
        from services.crash_trace import log_exception

        log_exception("notify_mod_changed.refresh_projection", internal_id=mid)
        logger.debug("refresh_projection failed internal_id=%s", mid, exc_info=True)
    if _should_marshal_to_gui():
        bridge = _gui_bridge()
        if bridge is None:
            logger.warning(
                "notify_mod_changed skipped UI listeners (no GUI bridge) internal_id=%s",
                mid,
            )
            return
        bridge.changed.emit(mid)
        return
    _invoke_listeners(mid)


def _invoke_listeners(mid: str) -> None:
    with _LOCK:
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(mid)
        except Exception:  # noqa: BLE001
            from services.crash_trace import log_exception

            log_exception("notify_mod_changed.listener", internal_id=mid)
            logger.debug(
                "mod_changed listener failed internal_id=%s", mid, exc_info=True
            )


def _should_marshal_to_gui() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication, QThread
    except Exception:  # noqa: BLE001
        return False
    app = QCoreApplication.instance()
    if app is None:
        return False
    return QThread.currentThread() is not app.thread()


def _gui_bridge() -> Any:
    """QObject that lives on the GUI thread and queues listener dispatch."""
    global _BRIDGE
    try:
        from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal
    except Exception:  # noqa: BLE001
        return None
    app = QCoreApplication.instance()
    if app is None:
        return None
    if _BRIDGE is not None:
        return _BRIDGE

    class _ProjectionBridge(QObject):
        changed = Signal(str)

        def __init__(self) -> None:
            super().__init__()
            self.changed.connect(
                self._dispatch,
                Qt.ConnectionType.QueuedConnection,
            )

        def _dispatch(self, mid: str) -> None:
            _invoke_listeners(str(mid or "").strip())

    with _LOCK:
        if _BRIDGE is None:
            bridge = _ProjectionBridge()
            gui = app.thread()
            if QThread.currentThread() is not gui:
                bridge.moveToThread(gui)
            _BRIDGE = bridge
        return _BRIDGE
