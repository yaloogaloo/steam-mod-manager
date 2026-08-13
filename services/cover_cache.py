"""LRU cache of decoded cover ``QImage`` objects (GUI-safe copies)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

MAX_COVER_CACHE = 300

_LOCK = threading.Lock()
# key -> QImage
_CACHE: OrderedDict[str, Any] = OrderedDict()


def _stamp(path: Path) -> tuple[str, float, int]:
    try:
        resolved = path.expanduser().resolve()
        st = resolved.stat()
        return str(resolved), float(st.st_mtime), int(st.st_size)
    except OSError:
        return str(path), 0.0, 0


def cover_cache_key(
    path: str | Path,
    width: int,
    height: int,
) -> str:
    loc, mtime, size = _stamp(Path(path))
    return f"{loc}|{mtime}|{size}|{int(width)}x{int(height)}"


def get_cover_image(path: str | Path, width: int, height: int) -> Any | None:
    """Return a copy of the cached ``QImage``, or ``None``."""
    key = cover_cache_key(path, width, height)
    with _LOCK:
        image = _CACHE.get(key)
        if image is None:
            return None
        _CACHE.move_to_end(key)
        try:
            return image.copy()
        except Exception:  # noqa: BLE001
            return image


def put_cover_image(
    path: str | Path,
    width: int,
    height: int,
    image: Any,
) -> None:
    if image is None:
        return
    try:
        if hasattr(image, "isNull") and image.isNull():
            return
    except Exception:  # noqa: BLE001
        return
    key = cover_cache_key(path, width, height)
    try:
        stored = image.copy()
    except Exception:  # noqa: BLE001
        stored = image
    with _LOCK:
        _CACHE[key] = stored
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_COVER_CACHE:
            _CACHE.popitem(last=False)


def invalidate_cover(path: str | Path | None = None) -> None:
    if path is None:
        with _LOCK:
            _CACHE.clear()
        return
    needle = str(Path(path))
    with _LOCK:
        drop = [key for key in _CACHE if key.startswith(needle) or needle in key]
        for key in drop:
            _CACHE.pop(key, None)


def reset_cover_cache() -> None:
    invalidate_cover(None)


def cover_cache_size() -> int:
    with _LOCK:
        return len(_CACHE)
