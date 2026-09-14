"""Ephemeral ``cache/offline_view`` helpers — fingerprint hit + LRU policy.

Cache is never Source of Truth. Safe to delete entirely; rebuilt on OPEN miss
from LIVE/Backup manifest + Asset Store.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from pathlib import Path

from services.asset_manifest import AssetManifest

logger = logging.getLogger(__name__)

FINGERPRINT_FILENAME = "manifest_fingerprint"
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB


def fingerprint_manifest(manifest: AssetManifest) -> str:
    """Stable fingerprint of manifest content (does not change manifest format)."""
    lines = [f"v{int(manifest.schema_version)}"]
    for ref in sorted(manifest.assets, key=lambda a: a.path):
        lines.append(f"{ref.path}|{ref.sha256}|{int(ref.size)}")
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_fingerprint(view_dir: Path | str) -> str:
    path = Path(view_dir) / FINGERPRINT_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_fingerprint(view_dir: Path | str, fingerprint: str) -> None:
    path = Path(view_dir) / FINGERPRINT_FILENAME
    path.write_text(str(fingerprint).strip() + "\n", encoding="utf-8")


def live_view_dirname(*, mod_id: str = "", index: Path | None = None) -> str:
    """Prefer ``live_<mod_id>``; fall back to path-hash key."""
    mid = str(mod_id or "").strip()
    if mid.isdigit():
        return f"live_{mid}"
    if index is not None:
        dig = hashlib.sha256(str(Path(index).resolve()).encode("utf-8")).hexdigest()
        return f"live_{dig[:16]}"
    return f"live_{int(time.time())}"


def backup_view_dirname(dest_offline: Path | str) -> str:
    dig = hashlib.sha256(str(Path(dest_offline).resolve()).encode("utf-8")).hexdigest()
    return f"backup_{dig[:16]}"


def resolve_live_view_dir(
    *,
    mod_id: str = "",
    index: Path | None = None,
    root: Path | None = None,
) -> Path:
    from core.paths import offline_view_cache_dir

    base = Path(root) if root is not None else offline_view_cache_dir()
    return base / live_view_dirname(mod_id=mod_id, index=index)


def is_offline_view_path(path: Path | str | None) -> bool:
    """True when *path* is inside ``cache/offline_view`` (the only OPEN root)."""
    if path is None:
        return False
    try:
        resolved = Path(path).resolve()
        from core.paths import offline_view_cache_dir

        root = offline_view_cache_dir().resolve()
        resolved.relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def require_offline_view_path(path: Path | None) -> Path | None:
    """Return *path* only when it is a ``cache/offline_view`` index."""
    if path is None:
        return None
    if is_offline_view_path(path):
        return Path(path)
    logger.warning("refusing OPEN path outside cache/offline_view: %s", path)
    return None


def touch_lru(view_dir: Path | str) -> None:
    """Bump directory mtime for LRU (best-effort)."""
    path = Path(view_dir)
    try:
        now = time.time()
        path.touch(exist_ok=True)
        # Also touch fingerprint so nested mtime tools see activity.
        fp = path / FINGERPRINT_FILENAME
        if fp.is_file():
            fp.touch(exist_ok=True)
        else:
            path.utime(None)
        del now
    except OSError:
        try:
            path.utime(None)
        except OSError:
            pass


def try_offline_view_hit(
    view_dir: Path | str,
    expected_fingerprint: str,
    *,
    index_name: str = "index.html",
) -> Path | None:
    """
    Return openable index when cache tree matches fingerprint.

    Does **not** verify Asset Store hashes — fingerprint covers manifest
    identity; HTML asset refs must exist on disk beside the index.
    """
    from services.info_asset_runtime import missing_live_asset_refs

    view = Path(view_dir)
    expected = str(expected_fingerprint or "").strip().lower()
    if not expected or not view.is_dir():
        return None
    stored = read_fingerprint(view).lower()
    if stored != expected:
        return None
    index = view / index_name
    try:
        if not index.is_file():
            return None
    except OSError:
        return None
    missing = missing_live_asset_refs(index)
    if missing:
        return None
    touch_lru(view)
    return index.resolve()


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += int(f.stat().st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def list_offline_view_entries(root: Path | None = None) -> list[tuple[Path, float, int]]:
    """Return ``(dir, mtime, size_bytes)`` for each offline_view child."""
    from core.paths import offline_view_cache_dir

    base = Path(root) if root is not None else offline_view_cache_dir()
    if not base.is_dir():
        return []
    out: list[tuple[Path, float, int]] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        try:
            mtime = float(child.stat().st_mtime)
        except OSError:
            continue
        out.append((child, mtime, _dir_size_bytes(child)))
    return out


def enforce_offline_view_lru(
    *,
    root: Path | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    dry_run: bool = True,
) -> dict[str, object]:
    """Delete oldest offline_view trees until under entry/byte caps."""
    entries = list_offline_view_entries(root)
    entries.sort(key=lambda item: item[1])  # oldest first
    total_bytes = sum(size for _, _, size in entries)
    to_remove: list[Path] = []

    # Drop oldest while over caps.
    live = list(entries)
    while live and (
        len(live) > max(0, int(max_entries))
        or total_bytes > max(0, int(max_bytes))
    ):
        victim, _mtime, size = live.pop(0)
        to_remove.append(victim)
        total_bytes -= size

    removed = 0
    freed = 0
    for path in to_remove:
        size = _dir_size_bytes(path)
        if dry_run:
            removed += 1
            freed += size
            continue
        try:
            shutil.rmtree(path)
            removed += 1
            freed += size
        except OSError as exc:
            logger.warning("offline_view LRU remove failed %s: %s", path, exc)

    return {
        "candidates": len(to_remove),
        "removed": removed,
        "freed_bytes": freed,
        "remaining": len(entries) - removed if not dry_run else len(entries),
        "dry_run": bool(dry_run),
        "paths": [str(p) for p in to_remove],
    }


def invalidate_view_dir(view_dir: Path | str) -> None:
    path = Path(view_dir)
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("invalidate offline_view failed %s: %s", path, exc)
