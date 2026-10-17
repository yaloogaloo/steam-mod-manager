"""Graceful GUI shutdown: stop DB workers, then close SQLite if idle."""

from __future__ import annotations

import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

JOIN_TIMEOUT_S = 3.0

_shutting_down = False
_db_closed = False
_order: list[str] = []
_test_workers_busy = False
_window_ref: weakref.ref | None = None


def shutdown_order() -> list[str]:
    return list(_order)


def is_shutting_down() -> bool:
    return bool(_shutting_down)


def db_closed_on_shutdown() -> bool:
    return bool(_db_closed)


def register_main_window(window: Any) -> None:
    global _window_ref
    _window_ref = weakref.ref(window)


def set_test_workers_busy(busy: bool) -> None:
    """Test E: pretend a DB worker is still running so close is skipped."""
    global _test_workers_busy
    _test_workers_busy = bool(busy)


def reset_shutdown_state_for_tests() -> None:
    global _shutting_down, _db_closed, _order, _test_workers_busy, _window_ref
    _shutting_down = False
    _db_closed = False
    _order = []
    _test_workers_busy = False
    _window_ref = None
    try:
        from core.db_manager import DatabaseManager

        DatabaseManager._app_shutdown = False
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.library_reconcile import reset_reconcile_async_state

        reset_reconcile_async_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.metadata_backup_sync import reset_backup_rebuild_async_state

        reset_backup_rebuild_async_state()
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.deploy import reset_deploy_conflict_scan_state

        reset_deploy_conflict_scan_state()
    except Exception:  # noqa: BLE001
        pass


def shutdown_runtime(
    window: Any | None = None,
    *,
    join_timeout_s: float = JOIN_TIMEOUT_S,
) -> bool:
    """Stop workers, then close SQLite only if no DB worker is still running.

    Idempotent. Returns True when the singleton connection was closed.
    """
    global _shutting_down, _db_closed
    from core.db_manager import DatabaseManager

    already = _shutting_down
    _shutting_down = True
    DatabaseManager.begin_app_shutdown()
    if not already:
        _order.append("shutdown_begin")

    win = window
    if win is None and _window_ref is not None:
        win = _window_ref()

    stop_failed = False
    if win is not None:
        try:
            win.sync_view.shutdown()
        except Exception:  # noqa: BLE001
            stop_failed = True
            logger.exception("sync_view.shutdown failed")
        try:
            win.library_view.shutdown_workers()
        except Exception:  # noqa: BLE001
            stop_failed = True
            logger.exception("library_view.shutdown_workers failed")

    from services.library_reconcile import (
        join_reconcile_thread,
        request_reconcile_shutdown,
    )
    from services.metadata_backup_sync import (
        join_backup_rebuild_thread,
        request_backup_rebuild_shutdown,
    )
    from services.deploy import (
        join_deploy_conflict_scan,
        request_deploy_conflict_scan_shutdown,
    )

    request_reconcile_shutdown()
    request_backup_rebuild_shutdown()
    request_deploy_conflict_scan_shutdown()

    rec_idle = join_reconcile_thread(join_timeout_s)
    bak_idle = join_backup_rebuild_thread(join_timeout_s)
    dep_idle = join_deploy_conflict_scan(join_timeout_s)
    load_idle = True
    if win is not None:
        try:
            load_idle = not win.library_view.library_load_is_running()
        except Exception:  # noqa: BLE001
            load_idle = True

    if "workers_stopped" not in _order:
        _order.append("workers_stopped")

    busy = (not rec_idle) or (not bak_idle) or (not dep_idle) or (not load_idle)
    if busy or _test_workers_busy or stop_failed:
        if "db_close_skipped" not in _order:
            _order.append("db_close_skipped")
        logger.warning(
            "UNSAFE_TO_CLOSE_DB_BEFORE_WORKER_TERMINATION rec_idle=%s "
            "bak_idle=%s dep_idle=%s load_idle=%s test_busy=%s",
            rec_idle,
            bak_idle,
            dep_idle,
            load_idle,
            _test_workers_busy,
        )
        return False

    DatabaseManager.close_singleton()
    _db_closed = True
    if "db_close" not in _order:
        _order.append("db_close")
    return True
