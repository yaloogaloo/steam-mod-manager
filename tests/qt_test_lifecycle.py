"""Qt test lifecycle helpers — cleanup and leak detection (tests only).

Prevents cumulative QWidget / QThreadPool / QTimer pollution across UI tests
that share a process-wide ``QApplication``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("qt_test_lifecycle")


@dataclass
class QtResourceSnapshot:
    toplevel_ids: frozenset[int] = field(default_factory=frozenset)
    toplevel_count: int = 0
    timer_count: int = 0
    thread_count: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return None
    return QApplication.instance()


def snapshot_qt_resources() -> QtResourceSnapshot:
    app = _qapp()
    if app is None:
        return QtResourceSnapshot()
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QWidget

    tops = list(app.topLevelWidgets())
    details: list[dict[str, Any]] = []
    for w in tops:
        try:
            details.append(
                {
                    "type": type(w).__name__,
                    "objectName": w.objectName() or "",
                    "parent": type(w.parent()).__name__ if w.parent() is not None else None,
                    "visible": bool(w.isVisible()),
                    "id": id(w),
                }
            )
        except RuntimeError:
            continue
    timers = 0
    threads = 0
    try:
        timers = sum(1 for t in app.findChildren(QTimer) if t.isActive())
    except Exception:  # noqa: BLE001
        pass
    try:
        threads = sum(1 for t in app.findChildren(QThread) if t.isRunning())
    except Exception:  # noqa: BLE001
        pass
    # Also count QWidget children that are somehow top-level without parent.
    del QWidget  # noqa: F841 — imported for type clarity in details only
    return QtResourceSnapshot(
        toplevel_ids=frozenset(id(w) for w in tops),
        toplevel_count=len(tops),
        timer_count=timers,
        thread_count=threads,
        details=details,
    )


def settle_qt_events(*, rounds: int = 6, budget_ms: int = 50) -> None:
    """Flush deferred deletes and posted events without spinning forever."""
    app = _qapp()
    if app is None:
        return
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(rounds):
        try:
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents(QCoreApplication.ProcessEventsFlag.AllEvents, budget_ms)
        except Exception:  # noqa: BLE001
            break


def stop_active_timers(*, parents: set[int] | None = None) -> int:
    """Stop active QTimers. If *parents* given, only those parented to them."""
    app = _qapp()
    if app is None:
        return 0
    from PySide6.QtCore import QTimer

    stopped = 0
    try:
        timers = list(app.findChildren(QTimer))
    except Exception:  # noqa: BLE001
        return 0
    for timer in timers:
        try:
            if not timer.isActive():
                continue
            parent = timer.parent()
            if parents is not None:
                if parent is None or id(parent) not in parents:
                    continue
            elif parent is not None:
                # Keep timers owned by surviving widgets; only kill orphans.
                continue
            timer.stop()
            stopped += 1
        except RuntimeError:
            continue
    return stopped


def _shutdown_widget_threads(widget: object) -> None:
    """Best-effort stop QThreads / pools owned by a widget tree before delete."""
    try:
        shutdown = getattr(widget, "shutdown_workers", None)
        if callable(shutdown):
            shutdown()
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtCore import QThread, QThreadPool

        for thread in list(widget.findChildren(QThread)):  # type: ignore[attr-defined]
            try:
                if not thread.isRunning():
                    continue
                try:
                    thread.requestInterruption()
                except Exception:  # noqa: BLE001
                    pass
                thread.quit()
                thread.wait(1500)
            except RuntimeError:
                continue
        for pool in list(widget.findChildren(QThreadPool)):  # type: ignore[attr-defined]
            try:
                pool.clear()
                pool.waitForDone(1500)
            except RuntimeError:
                continue
    except Exception:  # noqa: BLE001
        pass

    # Detail-panel metadata workers are plain attrs, not always children.
    for attr in ("_metadata_worker", "_load_worker", "_import_worker", "_worker"):
        try:
            worker = getattr(widget, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if worker is None:
            continue
        try:
            if hasattr(worker, "isRunning") and worker.isRunning():
                try:
                    worker.requestInterruption()
                except Exception:  # noqa: BLE001
                    pass
                if hasattr(worker, "quit"):
                    worker.quit()
                if hasattr(worker, "wait"):
                    worker.wait(1500)
        except RuntimeError:
            continue


def destroy_toplevel_widgets() -> list[dict[str, Any]]:
    """Hide/close/deleteLater every top-level QWidget (not the QApplication)."""
    app = _qapp()
    if app is None:
        return []
    destroyed: list[dict[str, Any]] = []
    for widget in list(app.topLevelWidgets()):
        try:
            wid = id(widget)
        except RuntimeError:
            continue
        info = {
            "type": type(widget).__name__,
            "objectName": "",
            "visible": False,
            "id": wid,
            "parent": None,
        }
        try:
            info["objectName"] = widget.objectName() or ""
            info["visible"] = bool(widget.isVisible())
            parent = widget.parent()
            info["parent"] = type(parent).__name__ if parent is not None else None
        except RuntimeError:
            pass
        _shutdown_widget_threads(widget)
        try:
            widget.hide()
        except RuntimeError:
            pass
        try:
            widget.close()
        except RuntimeError:
            pass
        try:
            widget.deleteLater()
        except RuntimeError:
            pass
        destroyed.append(info)
    return destroyed


def drain_cover_loader_pool() -> None:
    try:
        from services.cover_loader import CoverLoaderManager

        CoverLoaderManager.reset_instance()
    except Exception:  # noqa: BLE001
        logger.debug("cover loader reset failed", exc_info=True)


def drain_global_thread_pools() -> None:
    try:
        from PySide6.QtCore import QThreadPool

        pool = QThreadPool.globalInstance()
        if pool is not None:
            pool.clear()
            pool.waitForDone(1000)
    except Exception:  # noqa: BLE001
        logger.debug("global QThreadPool drain failed", exc_info=True)


def format_leak_report(
    *,
    nodeid: str,
    before: QtResourceSnapshot,
    after: QtResourceSnapshot,
    destroyed: list[dict[str, Any]],
) -> str:
    lines = [
        "QT RESOURCE LEAK",
        f"test: {nodeid}",
        f"toplevel before={before.toplevel_count} after={after.toplevel_count}",
        f"active_timers before={before.timer_count} after={after.timer_count}",
        f"running_qthreads before={before.thread_count} after={after.thread_count}",
        f"destroyed_this_teardown={len(destroyed)}",
    ]
    for d in after.details[:20]:
        lines.append(
            f"  leftover: type={d.get('type')} objectName={d.get('objectName')!r} "
            f"parent={d.get('parent')} visible={d.get('visible')}"
        )
    for d in destroyed[:20]:
        lines.append(
            f"  cleaned: type={d.get('type')} objectName={d.get('objectName')!r} "
            f"visible={d.get('visible')}"
        )
    return "\n".join(lines)


def qt_teardown_pass(
    *,
    nodeid: str,
    before: QtResourceSnapshot,
    report_leaks: bool = True,
) -> None:
    """
    Teardown cycle: stop timers, destroy top-level widgets, drain pools.

    Does **not** destroy the process ``QApplication``. All top-level widgets
    are treated as test-owned; module fixtures must not leave windows alive
    across tests.
    """
    tops = []
    app = _qapp()
    if app is not None:
        try:
            tops = list(app.topLevelWidgets())
        except Exception:  # noqa: BLE001
            tops = []
    parent_ids = {id(w) for w in tops}
    stop_active_timers(parents=parent_ids)
    destroyed = destroy_toplevel_widgets()
    drain_cover_loader_pool()
    drain_global_thread_pools()
    settle_qt_events()
    destroyed += destroy_toplevel_widgets()
    settle_qt_events(rounds=4, budget_ms=40)

    after = snapshot_qt_resources()
    if report_leaks and after.toplevel_count > 0:
        logger.warning(
            "%s",
            format_leak_report(
                nodeid=nodeid,
                before=before,
                after=after,
                destroyed=destroyed,
            ),
        )
    elif destroyed:
        logger.info(
            "qt_teardown cleaned %s widgets for %s",
            len(destroyed),
            nodeid,
        )
