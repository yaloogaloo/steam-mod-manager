"""In-memory ``metadata.json`` cache keyed by file path + mtime/size."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INFO_DIR_NAME = ".info"
LEGACY_INFO_DIR_NAME = "info"
METADATA_FILENAME = "metadata.json"
LEGACY_METADATA_FILENAME = "mod.json"

_LOCK = threading.Lock()
# resolved file path -> (mtime, size, data)
_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _candidates(managed_path: Path) -> list[Path]:
    modern = managed_path / INFO_DIR_NAME
    legacy = managed_path / LEGACY_INFO_DIR_NAME
    info = modern if modern.is_dir() else (legacy if legacy.is_dir() else modern)
    return [info / METADATA_FILENAME, info / LEGACY_METADATA_FILENAME]


def _file_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path)


def load_metadata(path: str | Path) -> dict[str, Any] | None:
    """
    Load ``.info/metadata.json`` for a managed Mod folder.

    Cache hit when the file mtime and size are unchanged.
    """
    root = Path(path)
    for candidate in _candidates(root):
        if not candidate.is_file():
            continue
        try:
            st = candidate.stat()
        except OSError:
            continue
        key = _file_key(candidate)
        mtime = float(st.st_mtime)
        size = int(st.st_size)
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and hit[0] == mtime and hit[1] == size:
                return hit[2]
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read metadata %s: %s", candidate, exc)
            continue
        if not isinstance(parsed, dict):
            continue
        with _LOCK:
            _CACHE[key] = (mtime, size, parsed)
        return parsed
    return None


def invalidate_metadata(path: str | Path | None = None) -> None:
    """Drop one folder/file entry, or the entire cache when *path* is None."""
    if path is None:
        with _LOCK:
            _CACHE.clear()
        return
    target = Path(path)
    keys: list[str] = []
    try:
        resolved = str(target.expanduser().resolve())
    except OSError:
        resolved = str(target)
    with _LOCK:
        for key in _CACHE:
            if key == resolved or key.startswith(resolved + "\\") or key.startswith(
                resolved + "/"
            ):
                keys.append(key)
        for key in keys:
            _CACHE.pop(key, None)
        # Also drop known metadata filenames under a managed folder.
        for candidate in _candidates(target):
            _CACHE.pop(_file_key(candidate), None)


def reset_metadata_cache() -> None:
    invalidate_metadata(None)


def metadata_cache_size() -> int:
    with _LOCK:
        return len(_CACHE)
