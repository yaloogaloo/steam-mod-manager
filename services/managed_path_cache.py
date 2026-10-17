"""Managed-folder path cache — Internal ID → path (not an identity axis).

Library / Deploy / Detail should hit this cache after Reconcile refreshes
``last_known_path``. Full-library discovers are cold-path only.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
# (library_root_key, mod_id) → absolute path string
_CACHE: dict[tuple[str, str], str] = {}
_STATS = {"hits": 0, "misses": 0, "puts": 0, "invalidations": 0}


def _root_key(library_root: str | Path | None) -> str:
    if library_root is None:
        return ""
    try:
        return str(Path(library_root).expanduser().resolve())
    except OSError:
        return str(Path(library_root))


def cache_stats() -> dict[str, int]:
    with _LOCK:
        return dict(_STATS)


def reset_managed_path_cache_stats() -> None:
    with _LOCK:
        for key in _STATS:
            _STATS[key] = 0


def invalidate_managed_path_cache(
    mod_id: str | int | None = None,
    *,
    library_root: str | Path | None = None,
) -> None:
    """Drop one entity, one library root, or the entire cache."""
    with _LOCK:
        _STATS["invalidations"] += 1
        mid = str(mod_id or "").strip()
        root = _root_key(library_root)
        if not mid and not root:
            _CACHE.clear()
            return
        drop: list[tuple[str, str]] = []
        for key in _CACHE:
            if mid and key[1] != mid:
                continue
            if root and key[0] != root:
                continue
            if mid or root:
                drop.append(key)
        for key in drop:
            _CACHE.pop(key, None)


def put_managed_path(
    mod_id: str | int,
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> None:
    mid = str(mod_id or "").strip()
    if not mid:
        return
    try:
        abs_path = str(Path(path).expanduser().resolve())
    except OSError:
        abs_path = str(path)
    if not abs_path:
        return
    root = _root_key(library_root)
    if not root:
        try:
            root = str(Path(abs_path).parent.parent.resolve())
        except OSError:
            root = ""
    with _LOCK:
        _CACHE[(root, mid)] = abs_path
        _STATS["puts"] += 1


def get_cached_managed_path(
    mod_id: str | int,
    *,
    library_root: str | Path | None = None,
) -> Path | None:
    mid = str(mod_id or "").strip()
    if not mid:
        return None
    root = _root_key(library_root)
    with _LOCK:
        raw = _CACHE.get((root, mid))
        if raw is None:
            _STATS["misses"] += 1
            return None
        _STATS["hits"] += 1
    try:
        candidate = Path(raw)
        if candidate.is_dir():
            return candidate
    except OSError:
        pass
    invalidate_managed_path_cache(mid, library_root=library_root)
    return None


def remember_resolved(
    mod_id: str | int,
    path: Path | None,
    *,
    library_root: str | Path | None = None,
) -> None:
    if path is None:
        return
    put_managed_path(mod_id, path, library_root=library_root)
