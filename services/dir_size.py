"""Directory size with skip rules + mtime cache (detail size badge)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

# Skip these directory names anywhere in the walk.
_SKIP_DIR_NAMES = frozenset({".cache", "cache"})
# Skip these names only when the parent directory is ``.info`` / ``info``.
_SKIP_UNDER_INFO = frozenset({"offline", "assets"})
_INFO_DIRS = frozenset({".info", "info"})

_LOCK = threading.Lock()
# resolved root -> (root_mtime, total_bytes)
_CACHE: dict[str, tuple[float, int]] = {}


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


def directory_size(path: str | Path) -> int:
    """
    Sum file sizes under *path*, skipping ``.info/offline``, ``.info/assets``,
    and ``.cache`` trees. Cached until the root folder mtime changes.
    """
    root = Path(path)
    if not root.is_dir():
        return 0
    try:
        root_mtime = float(root.stat().st_mtime)
    except OSError:
        return 0
    key = _root_key(root)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == root_mtime:
            return int(hit[1])

    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            parent_name = os.path.basename(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if not _should_skip_dir(name, parent_name)
            ]
            for name in filenames:
                file_path = os.path.join(dirpath, name)
                try:
                    total += os.path.getsize(file_path)
                except OSError:
                    continue
    except OSError:
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
