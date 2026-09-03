"""Process-wide single-instance lock for the Steam Mod Manager GUI.

Uses Qt ``QLockFile`` so a second user-started application cannot open the
production DB. Windows venv launcher PIDs are irrelevant: only the real
interpreter executes this module.
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path

from PySide6.QtCore import QLockFile

from core.paths import DATABASE_FILENAME

logger = logging.getLogger(__name__)

LOCK_FILENAME = "app_instance.lock"

_lock: QLockFile | None = None
_atexit_registered = False


def instance_lock_path() -> Path:
    """Lock file under the app data directory — never the SQLite DB file."""
    from core import paths

    return paths.data_dir() / LOCK_FILENAME


def instance_lock_is_database_path(path: Path | None = None) -> bool:
    """True when *path* would collide with the production SQLite file."""
    lock = (path if path is not None else instance_lock_path()).resolve()
    from core import paths

    db = paths.database_path().resolve()
    if lock == db:
        return True
    if lock.name == DATABASE_FILENAME:
        return True
    return False


def acquire_instance_lock(*, path: Path | None = None, timeout_ms: int = 0) -> bool:
    """Try to become the single GUI instance.

    Must be called before ``get_db()``. Does not remove unknown lock files;
    stale locks are recovered by ``QLockFile``.
    """
    global _lock, _atexit_registered
    if _lock is not None and _lock.isLocked():
        return True

    lock_path = Path(path) if path is not None else instance_lock_path()
    if lock_path.name == DATABASE_FILENAME:
        logger.error("refusing to use SQLite path as instance lock: %s", lock_path)
        return False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(int(timeout_ms)):
        logger.error(
            "Steam Mod Manager is already running (lock=%s)",
            lock_path,
        )
        return False
    _lock = lock
    if not _atexit_registered:
        atexit.register(release_instance_lock)
        _atexit_registered = True
    logger.info("acquired single-instance lock path=%s", lock_path)
    return True


def release_instance_lock() -> None:
    """Release the process lock. Safe to call when no lock is held."""
    global _lock
    lock = _lock
    _lock = None
    if lock is None:
        return
    try:
        if lock.isLocked():
            lock.unlock()
    except Exception:  # noqa: BLE001
        logger.debug("instance lock unlock failed", exc_info=True)


def reset_instance_lock_for_tests() -> None:
    """Drop any held lock (pytest isolation)."""
    release_instance_lock()
