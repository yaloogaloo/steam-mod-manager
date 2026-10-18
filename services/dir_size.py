"""Current local payload bytes; management metadata and caches are excluded."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Skip these directory names anywhere in the walk.
_SKIP_DIR_NAMES = frozenset({".cache", "cache", ".info", "info", "backups", "import_cache", "历史版本"})
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


def _is_link(path: str) -> bool:
    observed = os.lstat(path)
    return stat.S_ISLNK(observed.st_mode) or bool(getattr(observed, "st_file_attributes", 0) & 0x400)


def _walk_error(error: OSError) -> None:
    raise error


def directory_size(
    path: str | Path,
    *,
    cancel_check: Callable[[], bool] | None = None,
    use_cache: bool = True,
) -> int:
    """
    Sum stored payload bytes under *path*. Exclude SMM metadata, backups and
    caches. Retained archives count as stored bytes, not unpacked download size.
    Root-mtime caching is a hint; content mutations must explicitly invalidate it.

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
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_walk_error):
            if cancel_check is not None and cancel_check():
                raise DirectorySizeCancelled()
            dirnames[:] = [
                name
                for name in dirnames
                if name.casefold() not in _SKIP_DIR_NAMES and not _is_link(os.path.join(dirpath,name))
            ]
            dirs += 1
            for name in filenames:
                files += 1
                file_path = os.path.join(dirpath, name)
                if name.casefold() in {".is_missing_content", "deploy_manifest.json"} or _is_link(file_path):
                    continue
                total += os.path.getsize(file_path)
    except DirectorySizeCancelled:
        raise
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
