"""Project-root path helpers."""

from __future__ import annotations

from pathlib import Path

# core/paths.py → parents[1] == project root (steam-mod-manager/)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MOD_DIR_NAME = "mod"
DATA_DIR_NAME = "data"
DATABASE_FILENAME = "mod_manager.db"


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
    """``<project_root>/data`` — caches and local app data."""
    path = project_root() / DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


ASSET_CACHE_DIR_NAME = "asset_cache"


def asset_cache_dir() -> Path:
    """Global Steam static asset cache: ``<project_root>/data/asset_cache``."""
    path = data_dir() / ASSET_CACHE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    """SQLite snapshot DB: ``<project_root>/data/mod_manager.db``."""
    return data_dir() / DATABASE_FILENAME


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
