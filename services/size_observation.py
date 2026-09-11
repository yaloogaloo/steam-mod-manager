"""Local managed-directory size observation (not Steam archive size).

ARCHITECTURE
------------
Size is a filesystem observation of the current managed folder::

    filesystem walk (services.dir_size.directory_size)
        → bounded background worker
        → mods.local_size_* (NOT updated_at / identity / file_size)
        → Layer-1 ModListItem

Rules:
- Never walk on the UI thread.
- Never walk inside ``build_library_snapshot`` / ``filter_sort_entries``.
- One Mod one job; duplicate enqueue coalesces to the latest generation.
- Bounded pool (2 workers) so one huge Mod cannot occupy Qt's global pool.
- ``unknown`` (NULL bytes) is not ``0`` (empty directory, status=ok).
- Missing directory is status=missing, never forged as ok 0 B.
- Failed walks keep last-known bytes and set status=failed.
- Root mtime is a cheap dirty hint only — not proof that size is still correct.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dir_size import DirectorySizeCancelled, invalidate_directory_size
from services import dir_size as dir_size_mod

logger = logging.getLogger(__name__)

SIZE_STATUS_UNKNOWN = "unknown"
SIZE_STATUS_OK = "ok"
SIZE_STATUS_MISSING = "missing"
SIZE_STATUS_FAILED = "failed"

MAX_SIZE_WORKERS = 2

OnSizeDone = Callable[["SizeObservation"], None]
SizeProjectionListener = Callable[["SizeObservation"], None]


@dataclass(frozen=True)
class SizeObservation:
    internal_id: str
    status: str
    size_bytes: int | None
    observed_at: str
    root_mtime: float | None
    managed_path: str
    generation: int = 0


_LOCK = threading.Lock()
_JOBS: queue.Queue[tuple[Any, ...] | None] = queue.Queue()
_WORKERS: list[threading.Thread] = []
_GENERATION: dict[str, int] = {}
_PENDING: dict[str, tuple[int, Path | None, bool, list[OnSizeDone]]] = {}
_RUNNING: set[str] = set()
_IDLE = threading.Event()
_IDLE.set()
_UI_THREAD_GUARD = True
_SEQ = 0
_BRIDGES: list[Any] = []
_SIZE_LISTENERS: list[SizeProjectionListener] = []
_SIZE_NOTIFY_BRIDGES: list[Any] = []


def set_size_observation_ui_guard(enabled: bool) -> None:
    """Test helper — allow sync observe on the pytest main thread."""
    global _UI_THREAD_GUARD
    _UI_THREAD_GUARD = bool(enabled)


def reset_size_observation() -> None:
    """Test helper — drop queued/running bookkeeping (in-flight walks may finish)."""
    global _SEQ
    with _LOCK:
        _PENDING.clear()
        _RUNNING.clear()
        _GENERATION.clear()
        _BRIDGES.clear()
        _SIZE_NOTIFY_BRIDGES.clear()
        _SEQ = 0
        _IDLE.set()
    # Drain queued jobs without waiting for in-flight walks.
    while True:
        try:
            _JOBS.get_nowait()
        except queue.Empty:
            break


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def subscribe_size_projection(listener: SizeProjectionListener) -> None:
    """LibraryView only — size scalars, never ``notify_mod_changed``."""
    with _LOCK:
        if listener not in _SIZE_LISTENERS:
            _SIZE_LISTENERS.append(listener)


def unsubscribe_size_projection(listener: SizeProjectionListener) -> None:
    with _LOCK:
        try:
            _SIZE_LISTENERS.remove(listener)
        except ValueError:
            pass


def _patch_size_projection_cache(obs: SizeObservation) -> None:
    mid = str(obs.internal_id or "").strip()
    if not mid.isdigit():
        return
    try:
        from services.mod_library_cache import get_library_cache

        get_library_cache().patch_local_size(
            mid,
            size_bytes=obs.size_bytes,
            status=obs.status,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "patch_local_size failed mid=%s", obs.internal_id, exc_info=True
        )


def _invoke_size_listeners(obs: SizeObservation) -> None:
    with _LOCK:
        listeners = list(_SIZE_LISTENERS)
    for listener in listeners:
        try:
            listener(obs)
        except Exception:  # noqa: BLE001
            logger.debug(
                "size projection listener failed mid=%s",
                obs.internal_id,
                exc_info=True,
            )


def _notify_size_projection(obs: SizeObservation) -> None:
    """Cache patch on caller thread; UI listeners marshaled like size on_done."""
    _patch_size_projection_cache(obs)
    with _LOCK:
        if not _SIZE_LISTENERS:
            return
    try:
        from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal
    except Exception:  # noqa: BLE001
        _invoke_size_listeners(obs)
        return
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() is app.thread():
        _invoke_size_listeners(obs)
        return

    class _Bridge(QObject):
        finished = Signal(object)

    bridge = _Bridge()
    bridge.moveToThread(app.thread())

    def _after(_payload: object) -> None:
        try:
            _invoke_size_listeners(obs)
        finally:
            with _LOCK:
                try:
                    _SIZE_NOTIFY_BRIDGES.remove(bridge)
                except ValueError:
                    pass

    bridge.finished.connect(_after, Qt.ConnectionType.QueuedConnection)
    with _LOCK:
        _SIZE_NOTIFY_BRIDGES.append(bridge)
    bridge.finished.emit(obs)
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _assert_not_ui_thread() -> None:
    if not _UI_THREAD_GUARD:
        return
    try:
        from PySide6.QtCore import QCoreApplication, QThread
    except Exception:  # noqa: BLE001
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    if QThread.currentThread() is app.thread():
        raise RuntimeError("observe_mod_size must not run on the UI thread")


def _ensure_workers() -> None:
    with _LOCK:
        alive = [t for t in _WORKERS if t.is_alive()]
        _WORKERS[:] = alive
        while len(_WORKERS) < MAX_SIZE_WORKERS:
            thread = threading.Thread(
                target=_worker_loop,
                name=f"mod-size-{len(_WORKERS)}",
                daemon=True,
            )
            thread.start()
            _WORKERS.append(thread)


def _worker_loop() -> None:
    while True:
        job = _JOBS.get()
        if job is None:
            return
        try:
            _run_job(*job)
        except Exception:  # noqa: BLE001
            logger.debug("size worker crash", exc_info=True)


def _probe_root(path: Path | None) -> tuple[bool, float | None]:
    if path is None:
        return False, None
    try:
        if not path.is_dir():
            return False, None
        return True, float(path.stat().st_mtime)
    except OSError:
        return False, None


def _resolve_path(
    internal_id: str,
    managed_path: str | Path | None,
    *,
    db: Any = None,
) -> Path | None:
    hint: Path | None = Path(managed_path) if managed_path is not None else None
    if hint is not None:
        try:
            if hint.is_dir():
                return hint
        except OSError:
            pass
    if not internal_id.isdigit():
        return hint
    try:
        from core.db_manager import get_db

        database = db if db is not None else get_db()
        row = database.get_mod_size_observation(internal_id) or {}
        raw = str(row.get("last_known_path") or "").strip()
        if raw:
            return Path(raw)
    except Exception:  # noqa: BLE001
        logger.debug("size path resolve failed mid=%s", internal_id, exc_info=True)
    if managed_path is not None:
        return Path(managed_path)
    return None


def _keep_last_bytes(row: dict[str, Any] | None) -> int | None:
    if not row:
        return None
    raw = row.get("local_size_bytes")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _persist(obs: SizeObservation, *, db: Any = None) -> None:
    mid = str(obs.internal_id or "").strip()
    if not mid.isdigit():
        return
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    database.update_mod_size_observation(
        mid,
        status=obs.status,
        size_bytes=obs.size_bytes,
        observed_at=obs.observed_at,
        root_mtime=obs.root_mtime,
    )


def _dispatch_done(callbacks: list[OnSizeDone], obs: SizeObservation) -> None:
    if not callbacks:
        return

    def _run() -> None:
        for cb in callbacks:
            try:
                cb(obs)
            except Exception:  # noqa: BLE001
                logger.debug("size on_done failed mid=%s", obs.internal_id, exc_info=True)

    try:
        from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal
    except Exception:  # noqa: BLE001
        _run()
        return
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() is app.thread():
        _run()
        return

    class _Bridge(QObject):
        finished = Signal(object)

    bridge = _Bridge()
    bridge.moveToThread(app.thread())

    def _after(_payload: object) -> None:
        try:
            _run()
        finally:
            with _LOCK:
                try:
                    _BRIDGES.remove(bridge)
                except ValueError:
                    pass

    bridge.finished.connect(_after, Qt.ConnectionType.QueuedConnection)
    with _LOCK:
        _BRIDGES.append(bridge)
    bridge.finished.emit(obs)


def observe_mod_size(
    internal_id: str | int,
    managed_path: str | Path | None = None,
    *,
    force: bool = False,
    db: Any = None,
    cancel_check: Callable[[], bool] | None = None,
    persist: bool = True,
) -> SizeObservation:
    """
    Walk one Mod's managed directory and optionally persist the observation.

    Must not run on the UI thread. Missing directories are ``missing``, not ``0``.
    """
    _assert_not_ui_thread()
    mid = str(internal_id or "").strip()
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    prev = None
    if mid.isdigit():
        try:
            prev = database.get_mod_size_observation(mid)
        except Exception:  # noqa: BLE001
            prev = None
    root = _resolve_path(mid, managed_path, db=database)
    exists, root_mtime = _probe_root(root)
    stamp = _utc_now()
    path_text = str(root) if root is not None else ""

    if not exists:
        obs = SizeObservation(
            internal_id=mid,
            status=SIZE_STATUS_MISSING,
            size_bytes=_keep_last_bytes(prev),
            observed_at=stamp,
            root_mtime=None,
            managed_path=path_text,
        )
        if persist and mid.isdigit():
            _persist(obs, db=database)
            _notify_size_projection(obs)
        return obs

    assert root is not None
    try:
        if force:
            invalidate_directory_size(root)
        total = int(
            dir_size_mod.directory_size(
                root,
                cancel_check=cancel_check,
                use_cache=not force,
            )
        )
    except DirectorySizeCancelled:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("directory_size failed mid=%s path=%s", mid, root, exc_info=True)
        obs = SizeObservation(
            internal_id=mid,
            status=SIZE_STATUS_FAILED,
            size_bytes=_keep_last_bytes(prev),
            observed_at=stamp,
            root_mtime=root_mtime,
            managed_path=path_text,
        )
        if persist and mid.isdigit():
            _persist(obs, db=database)
            _notify_size_projection(obs)
        return obs

    obs = SizeObservation(
        internal_id=mid,
        status=SIZE_STATUS_OK,
        size_bytes=int(total),
        observed_at=stamp,
        root_mtime=root_mtime,
        managed_path=path_text,
    )
    if persist and mid.isdigit():
        _persist(obs, db=database)
        _notify_size_projection(obs)
    return obs


def _job_still_current(mid: str, generation: int) -> bool:
    with _LOCK:
        return int(_GENERATION.get(mid, 0)) == int(generation)


def _run_job(
    mid: str,
    generation: int,
    path: Path | None,
    force: bool,
    callbacks: list[OnSizeDone],
) -> None:
    def _cancelled() -> bool:
        return not _job_still_current(mid, generation)

    obs: SizeObservation | None = None
    try:
        if _cancelled():
            raise DirectorySizeCancelled()
        obs = observe_mod_size(
            mid,
            path,
            force=force,
            cancel_check=_cancelled,
            persist=False,
        )
        if _cancelled():
            return
        from core.db_manager import get_db

        if mid.isdigit():
            _persist(obs, db=get_db())
            _notify_size_projection(obs)
        _dispatch_done(callbacks, obs)
    except DirectorySizeCancelled:
        return
    except Exception:  # noqa: BLE001
        logger.debug("size job failed mid=%s", mid, exc_info=True)
        if obs is None:
            obs = SizeObservation(
                internal_id=mid,
                status=SIZE_STATUS_FAILED,
                size_bytes=None,
                observed_at=_utc_now(),
                root_mtime=None,
                managed_path=str(path or ""),
                generation=generation,
            )
        if not _cancelled():
            _dispatch_done(callbacks, obs)
    finally:
        nxt: tuple[int, Path | None, bool, list[OnSizeDone]] | None = None
        with _LOCK:
            _RUNNING.discard(mid)
            pending = _PENDING.pop(mid, None)
            if pending is not None and int(pending[0]) != int(generation):
                nxt = pending
                _RUNNING.add(mid)
                _IDLE.clear()
            elif not _RUNNING and not _PENDING:
                _IDLE.set()
        if nxt is not None:
            n_gen, n_path, n_force, n_cb = nxt
            _JOBS.put((mid, n_gen, n_path, n_force, n_cb))


def enqueue_mod_size(
    internal_id: str | int,
    managed_path: str | Path | None = None,
    *,
    force: bool = False,
    on_done: OnSizeDone | None = None,
) -> bool:
    """
    Queue a background size observation. Never walks inline.

    Returns True when a new worker is started. False when the request is
    coalesced into an in-flight / already-queued job for the same Mod.
    """
    mid = str(internal_id or "").strip()
    if not mid.isdigit():
        return False
    path = Path(managed_path) if managed_path is not None else None
    callbacks = [on_done] if on_done is not None else []
    start: tuple[int, Path | None, bool, list[OnSizeDone]] | None = None
    with _LOCK:
        global _SEQ
        _SEQ += 1
        generation = _SEQ
        _GENERATION[mid] = generation
        if mid in _RUNNING:
            prev = _PENDING.get(mid)
            extra = list(prev[3] if prev else [])
            extra.extend(callbacks)
            _PENDING[mid] = (generation, path, bool(force), extra)
            return False
        if mid in _PENDING:
            prev = _PENDING[mid]
            extra = list(prev[3])
            extra.extend(callbacks)
            _PENDING[mid] = (generation, path, bool(force), extra)
            return False
        _RUNNING.add(mid)
        _IDLE.clear()
        start = (generation, path, bool(force), callbacks)
    assert start is not None
    _ensure_workers()
    _JOBS.put((mid, start[0], start[1], start[2], start[3]))
    return True


def note_mod_size_ready(
    internal_id: str | int,
    managed_path: str | Path | None = None,
    *,
    force: bool = False,
) -> None:
    """Import/Sync hook — enqueue, never wait, never raise into the caller."""
    try:
        enqueue_mod_size(internal_id, managed_path, force=force)
    except Exception:  # noqa: BLE001
        logger.debug(
            "note_mod_size_ready failed mid=%s", internal_id, exc_info=True
        )


def _is_dirty_row(row: dict[str, Any], *, force: bool) -> bool:
    status = str(row.get("local_size_status") or SIZE_STATUS_UNKNOWN).strip() or (
        SIZE_STATUS_UNKNOWN
    )
    if force:
        return True
    if status in {SIZE_STATUS_UNKNOWN, "", SIZE_STATUS_FAILED}:
        return True
    if status == SIZE_STATUS_MISSING:
        return bool(int(row.get("folder_present") or 0))
    if row.get("local_size_bytes") is None and status != SIZE_STATUS_MISSING:
        return True
    size_mtime = row.get("local_size_root_mtime")
    fs_mtime = row.get("fs_root_mtime")
    if size_mtime is None:
        return True
    if fs_mtime is None:
        return False
    try:
        return abs(float(size_mtime) - float(fs_mtime)) > 1e-6
    except (TypeError, ValueError):
        return True


def schedule_library_size_refresh(
    *,
    force: bool = False,
    game_id: int | None = None,
    internal_ids: Iterable[str | int] | None = None,
    db: Any = None,
) -> int:
    """
    Enqueue size jobs from SQL observation rows. Never walks here.

    ``force=True`` recalculates listed Mods (user Refresh). Still bounded by
    the 2-worker pool — it does not start 10k walks at once.
    ``force=False`` only unknown / failed / cheap root-mtime dirty rows.
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    ids = None if internal_ids is None else list(internal_ids)
    try:
        rows = database.list_mod_size_observation_rows(
            game_id=game_id, mod_ids=ids
        )
    except Exception:  # noqa: BLE001
        logger.debug("list_mod_size_observation_rows failed", exc_info=True)
        return 0
    queued = 0
    for row in rows:
        if not _is_dirty_row(row, force=force):
            continue
        mid = str(row.get("mod_id") or "").strip()
        path = str(row.get("last_known_path") or "").strip() or None
        if enqueue_mod_size(mid, path, force=force):
            queued += 1
    return queued


def wait_for_size_idle(timeout: float = 5.0) -> bool:
    """Test helper — wait until no size jobs are running or queued."""
    return bool(_IDLE.wait(timeout=max(0.05, float(timeout))))


def size_observation_busy() -> bool:
    with _LOCK:
        return bool(_RUNNING or _PENDING)
