"""Directory size with skip rules + mtime cache (detail size badge)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Skip these directory names anywhere in the walk.
_SKIP_DIR_NAMES = frozenset({".cache", "cache"})
# Skip these names only when the parent directory is ``.info`` / ``info``.
_SKIP_UNDER_INFO = frozenset({"offline", "assets"})
_INFO_DIRS = frozenset({".info", "info"})

_LOCK = threading.Lock()
# resolved root -> (root_mtime, total_bytes)
_CACHE: dict[str, tuple[float, int]] = {}


class DirectorySizeCancelled(Exception):
    """Walk aborted because a newer observation superseded this job."""


def _root_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path)


def _should_skip_dir(name: str, parent_name: str) -> bool:
    if name in _SKIP_DIR_NAMES:
        return True
    if name in _SKIP_UNDER_INFO and parent_name in _INFO_DIRS:
        return True
    return False


def directory_size(
    path: str | Path,
    *,
    cancel_check: Callable[[], bool] | None = None,
    use_cache: bool = True,
) -> int:
    """
    Sum file sizes under *path*, skipping ``.info/offline``, ``.info/assets``,
    and ``.cache`` trees. Cached until the root folder mtime changes.

    A missing directory returns 0 here (legacy). Observation callers must
    distinguish missing vs empty **before** using this value as ``ok`` bytes.
    ``os.walk`` does not follow directory symlinks/junctions (``followlinks=False``).
    """
    root = Path(path)
    if not root.is_dir():
        return 0
    try:
        root_mtime = float(root.stat().st_mtime)
    except OSError:
        return 0
    key = _root_key(root)
    if use_cache:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and hit[0] == root_mtime:
                return int(hit[1])

    total = 0
    t0 = time.perf_counter()
    files = 0
    dirs = 0
    ui = False
    try:
        from services.deploy_e2e import e2e_op, is_ui_thread

        ui = is_ui_thread()
    except Exception:  # noqa: BLE001
        e2e_op = None  # type: ignore[assignment]
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            if cancel_check is not None and cancel_check():
                raise DirectorySizeCancelled()
            parent_name = os.path.basename(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if not _should_skip_dir(name, parent_name)
            ]
            dirs += 1
            for name in filenames:
                files += 1
                file_path = os.path.join(dirpath, name)
                try:
                    total += os.path.getsize(file_path)
                except OSError:
                    continue
    except DirectorySizeCancelled:
        raise
    except OSError:
        pass
    if e2e_op is not None:
        try:
            e2e_op(
                "directory_size",
                (time.perf_counter() - t0) * 1000.0,
                files=files,
                dirs=dirs,
                bytes_count=int(total),
            )
            if ui:
                from services.deploy_e2e import e2e_event

                e2e_event(
                    "UI_THREAD_FS",
                    operation="directory_size",
                    path=str(root),
                    files=files,
                    bytes=int(total),
                )
        except Exception:  # noqa: BLE001
            pass

    with _LOCK:
        _CACHE[key] = (root_mtime, int(total))
    return int(total)


def invalidate_directory_size(path: str | Path | None = None) -> None:
    if path is None:
        with _LOCK:
            _CACHE.clear()
        return
    key = _root_key(Path(path))
    with _LOCK:
        _CACHE.pop(key, None)


def reset_directory_size_cache() -> None:
    invalidate_directory_size(None)
