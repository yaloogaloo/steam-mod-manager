"""Mod projection invalidation — single event for Library display sync.

ARCHITECTURE RULE
-----------------
After any mutation that changes Mod **display** data in SQLite:

    notify_mod_changed(internal_id)

That call:

1. ``refresh_projection(internal_id)`` — re-read the full Layer-1 row into warm cache
2. Notify LibraryView to rebind Viewport from the replaced ``ModCardData``

Forbidden: field-level notify (cover/offline/deploy/name), full Library refresh,
filesystem scan, or reconcile inside this path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

ModChangedListener = Callable[[str], None]

_LOCK = threading.Lock()
_LISTENERS: list[ModChangedListener] = []


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


def notify_mod_changed(internal_id: str | int) -> None:
    """
    Invalidate and refresh one Mod's Library Projection after DB commit.

    Callers must pass the internal Mod id only — never a field list.
    """
    mid = str(internal_id or "").strip()
    if not mid:
        return
    try:
        from services.mod_library_cache import get_library_cache

        get_library_cache().refresh_projection(mid)
    except Exception:  # noqa: BLE001
        logger.debug("refresh_projection failed internal_id=%s", mid, exc_info=True)
    with _LOCK:
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(mid)
        except Exception:  # noqa: BLE001
            logger.debug(
                "mod_changed listener failed internal_id=%s", mid, exc_info=True
            )
