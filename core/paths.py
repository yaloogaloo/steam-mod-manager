"""Project-root path helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# core/paths.py → parents[1] == project root (steam-mod-manager/)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MOD_DIR_NAME = "mod"
DATA_DIR_NAME = "data"
CACHE_DIR_NAME = "cache"
LOGS_DIR_NAME = "logs"
CONFIG_DIR_NAME = "config"
LOAD_ORDER_DIR_NAME = "load_order"
DATABASE_FILENAME = "mod_manager.db"
MOD_TYPES_FILENAME = "mod_types.json"
OFFLINE_VIEW_DIR_NAME = "offline_view"
ASSET_CACHE_DIR_NAME = "asset_cache"
ASSET_STORE_DIR_NAME = "asset_store"
COLLECTION_COVERS_DIR_NAME = "collection_covers"
IMPORT_CACHE_DIR_NAME = "import_cache"
HEADERS_CACHE_DIR_NAME = "headers"
TEMP_CACHE_DIR_NAME = "temp"
ASSET_CACHE_PRUNE_STAMP_NAME = ".asset_cache_prune_stamp"

# Regenerable trees that must live under cache/, never data/.
CACHE_SUBDIR_NAMES = (
    OFFLINE_VIEW_DIR_NAME,
    ASSET_CACHE_DIR_NAME,
    IMPORT_CACHE_DIR_NAME,
    HEADERS_CACHE_DIR_NAME,
    TEMP_CACHE_DIR_NAME,
)

# Durable data/ names that migrate_legacy_data_caches must never relocate or delete.
_PROTECTED_DATA_NAMES = frozenset(
    {
        DATABASE_FILENAME,
        f"{DATABASE_FILENAME}-wal",
        f"{DATABASE_FILENAME}-shm",
        "mod_backup",
        ASSET_STORE_DIR_NAME,
        "deploy_backup",
        COLLECTION_COVERS_DIR_NAME,
        "identity_repair_quarantine",
        MOD_TYPES_FILENAME,
        "mod_types.legacy_migrated",
    }
)

_LEGACY_CACHE_MIGRATED = False


def project_root() -> Path:
    """Absolute path to the repository / application root."""
    return _PROJECT_ROOT


def default_mod_library() -> Path:
    """
    Default local Mod library: ``<project_root>/mod``.

    Creates the directory if it does not exist.
    """
    path = project_root() / MOD_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """``<project_root>/data`` — durable business state (DB, Backup, Asset Store).

    Regenerable performance caches belong under :func:`get_cache_dir`, not here.
    Runtime logs belong under :func:`logs_dir`.
    """
    path = project_root() / DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    """``<project_root>/logs`` — runtime logs (safe to delete).

    Crash / exception traces belong here, not under ``data/``. Not Source of Truth.
    """
    path = project_root() / LOGS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    """``<project_root>/cache`` — regenerable performance layer (safe to delete).

    Entire tree may be removed; app must still start. Contents are rebuilt from
    ``data/`` + ``mod/`` + Asset Store. Never treat cache as Source of Truth.

    Does **not** scan cache contents on startup. Legacy ``data/`` cache trees
    are relocated with a directory-level rename when still present.
    """
    path = project_root() / CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    _maybe_migrate_legacy_caches()
    return path


def cache_temp_dir() -> Path:
    """``<project_root>/cache/temp`` — short-lived task files."""
    path = get_cache_dir() / TEMP_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def offline_view_cache_dir() -> Path:
    """``<project_root>/cache/offline_view`` — ephemeral ``file://`` OPEN trees.

    Sole production location for offline_view materializations.
    Not Backup. Not Asset Store. Not Identity. Safe to delete anytime.
    Never under ``data/``.
    """
    path = get_cache_dir() / OFFLINE_VIEW_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def offline_view_dir() -> Path:
    """Deprecated alias of :func:`offline_view_cache_dir` (tests / old imports).

    Production code must call :func:`offline_view_cache_dir` directly.
    """
    return offline_view_cache_dir()


def asset_cache_dir() -> Path:
    """URL-keyed Steam static cache: ``<project_root>/cache/asset_cache``.

    Regenerable. Not Asset Store. Not Source of Truth.
    """
    path = get_cache_dir() / ASSET_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def import_cache_dir() -> Path:
    """Temporary extract staging: ``<project_root>/cache/import_cache``.

    Regenerable. Used by import and deploy unpack. Not Backup.
    """
    path = get_cache_dir() / IMPORT_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def headers_cache_dir() -> Path:
    """Steam Store header images: ``<project_root>/cache/headers``.

    Regenerable from the network. Not durable metadata.
    """
    path = get_cache_dir() / HEADERS_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_data_offline_view_dir() -> Path:
    """Retired path ``data/offline_view`` — must not be created or written.

    Exists only for regression assertions / cleanup tooling.
    """
    return data_dir() / OFFLINE_VIEW_DIR_NAME


def legacy_data_asset_cache_dir() -> Path:
    """Retired path ``data/asset_cache`` — must not be created or written."""
    return data_dir() / ASSET_CACHE_DIR_NAME


def legacy_data_import_cache_dir() -> Path:
    """Retired path ``data/import_cache`` — must not be created or written."""
    return data_dir() / IMPORT_CACHE_DIR_NAME


def legacy_data_headers_dir() -> Path:
    """Retired path ``data/headers`` — must not be created or written."""
    return data_dir() / HEADERS_CACHE_DIR_NAME


def config_dir() -> Path:
    """``<project_root>/config`` — local configuration (not production DB)."""
    path = project_root() / CONFIG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_order_dir() -> Path:
    """``<project_root>/config/load_order`` — per-game SMM load-order files."""
    path = config_dir() / LOAD_ORDER_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_store_dir() -> Path:
    """
    Durable content-addressed asset store root: ``<project_root>/data/asset_store``.

    Returns the path only. Does **not** create the store.
    Distinct from ``asset_cache_dir()`` (URL-keyed regenerable cache under cache/).
    """
    return data_dir() / ASSET_STORE_DIR_NAME


def collection_covers_dir() -> Path:
    """Collection-owned covers: ``<project_root>/data/collection_covers``."""
    path = data_dir() / COLLECTION_COVERS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    """SQLite snapshot DB: ``<project_root>/data/mod_manager.db``."""
    return data_dir() / DATABASE_FILENAME


def mod_types_path() -> Path:
    """User-editable Type Definition file: ``<project_root>/data/mod_types.json``."""
    return data_dir() / MOD_TYPES_FILENAME


def _dir_is_empty(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return False
    return False


def _relocate_dir(src: Path, dest: Path) -> str:
    """Directory-level rename. Never copies or hashes file contents."""
    if src.name in _PROTECTED_DATA_NAMES:
        raise RuntimeError(f"refusing to relocate protected data name: {src.name}")
    if not src.exists():
        return "src_absent"
    if not src.is_dir():
        return "src_not_dir"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_dir() and _dir_is_empty(dest):
            dest.rmdir()
        else:
            return "dest_exists_nonempty"
    os.replace(src, dest)
    return "moved"


def _relocate_file(src: Path, dest: Path) -> str:
    if src.name in _PROTECTED_DATA_NAMES:
        raise RuntimeError(f"refusing to relocate protected data name: {src.name}")
    if not src.exists():
        return "src_absent"
    if not src.is_file():
        return "src_not_file"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        src.unlink(missing_ok=True)
        return "dest_exists_src_removed"
    os.replace(src, dest)
    return "moved"


def migrate_legacy_data_caches(
    *,
    data_root: Path | None = None,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """Move regenerable caches from ``data/`` to ``cache/`` (directory rename).

    Never touches DB, Backup, Asset Store, or Deployment trees.
    Never walks or hashes asset contents.
    """
    data_root = Path(data_root) if data_root is not None else project_root() / DATA_DIR_NAME
    cache_root = Path(cache_root) if cache_root is not None else project_root() / CACHE_DIR_NAME
    cache_root.mkdir(parents=True, exist_ok=True)

    moves = (
        (ASSET_CACHE_DIR_NAME, ASSET_CACHE_DIR_NAME),
        (IMPORT_CACHE_DIR_NAME, IMPORT_CACHE_DIR_NAME),
        (HEADERS_CACHE_DIR_NAME, HEADERS_CACHE_DIR_NAME),
    )
    results: dict[str, Any] = {
        "data_root": str(data_root),
        "cache_root": str(cache_root),
        "dirs": {},
        "stamp": None,
        "protected_untouched": sorted(_PROTECTED_DATA_NAMES),
    }
    for name, dest_name in moves:
        if name in _PROTECTED_DATA_NAMES or dest_name in _PROTECTED_DATA_NAMES:
            raise RuntimeError(f"refusing protected cache migrate name: {name}")
        src = data_root / name
        dest = cache_root / dest_name
        results["dirs"][name] = {
            "src": str(src),
            "dest": str(dest),
            "status": _relocate_dir(src, dest),
        }

    stamp_src = data_root / ASSET_CACHE_PRUNE_STAMP_NAME
    stamp_dest = cache_root / ASSET_CACHE_DIR_NAME / ASSET_CACHE_PRUNE_STAMP_NAME
    results["stamp"] = {
        "src": str(stamp_src),
        "dest": str(stamp_dest),
        "status": _relocate_file(stamp_src, stamp_dest),
    }
    return results


def _maybe_migrate_legacy_caches() -> None:
    """One-shot directory relocate for process-local canonical roots.

    Skips when ``data_dir()`` is isolated (pytest). Does not scan cache/.
    """
    global _LEGACY_CACHE_MIGRATED
    if _LEGACY_CACHE_MIGRATED:
        return
    canonical_data = (project_root() / DATA_DIR_NAME).resolve()
    try:
        if data_dir().resolve() != canonical_data:
            _LEGACY_CACHE_MIGRATED = True
            return
    except OSError:
        _LEGACY_CACHE_MIGRATED = True
        return
    migrate_legacy_data_caches(
        data_root=canonical_data,
        cache_root=project_root() / CACHE_DIR_NAME,
    )
    _LEGACY_CACHE_MIGRATED = True


def extract_app_id_from_workshop_path(path: str | Path) -> int | None:
    """
    Extract a Steam AppID from a workshop content path.

    Examples
    --------
    ``.../workshop/content/1623730`` → ``1623730``
    ``.../workshop/content/1623730/12345`` → ``1623730``
      (parent content segment wins when the leaf is a mod id)
    ``.../workshop/content`` → ``None``
    """
    try:
        resolved = Path(path).expanduser()
        parts = list(resolved.parts)
    except (OSError, TypeError, ValueError):
        return None

    # Prefer: .../content/<appid>/...
    for index, part in enumerate(parts):
        if part.lower() == "content" and index + 1 < len(parts):
            candidate = parts[index + 1]
            if candidate.isdigit():
                return int(candidate)

    # Fallback: trailing numeric folder
    name = resolved.name
    if name.isdigit():
        return int(name)

    return None
