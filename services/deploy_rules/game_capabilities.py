"""JSON-driven deploy capability flags (``config/game_capabilities.json``).

Callers ask ``supports_game_capability(app_id, name)``. This module is the
only reader of the capability file. Strategies must not parse the JSON
themselves.

Missing file / missing game / missing or false flag → False.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from core.paths import project_root

logger = logging.getLogger(__name__)

CONFIG_REL = Path("config") / "game_capabilities.json"
CAPABILITY_NORMALIZE_MOD_FOLDER_NAME = "normalize_mod_folder_name"

_CACHED: dict[str, Mapping[str, Any]] | None = None
_CONFIG_PATH: Path | None = None


def game_capabilities_config_path() -> Path:
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH
    return project_root() / CONFIG_REL


def set_game_capabilities_config_path(path: str | Path | None) -> None:
    """Tests: point the loader at a substitute JSON and drop the memory cache."""
    global _CONFIG_PATH, _CACHED
    _CONFIG_PATH = Path(path) if path is not None else None
    _CACHED = None


def reset_game_capabilities_cache() -> None:
    """Drop the in-memory table (does not clear a test path override)."""
    global _CACHED
    _CACHED = None


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _app_id_key(app_id: int | str) -> str:
    try:
        return str(int(app_id))
    except (TypeError, ValueError):
        return str(app_id or "").strip()


def _games_from_payload(raw: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    games = raw.get("games")
    if not isinstance(games, dict):
        games = {
            key: value
            for key, value in raw.items()
            if str(key).isdigit() and isinstance(value, dict)
        }
    table: dict[str, Mapping[str, Any]] = {}
    for key, caps in games.items():
        if not isinstance(caps, Mapping):
            continue
        label = str(key or "").strip()
        if not label or label.startswith("_"):
            continue
        try:
            label = str(int(label))
        except (TypeError, ValueError):
            continue
        table[label] = caps
    return table


def _load_games_table() -> dict[str, Mapping[str, Any]]:
    global _CACHED
    if _CACHED is not None:
        return _CACHED
    table: dict[str, Mapping[str, Any]] = {}
    path = game_capabilities_config_path()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            table = _games_from_payload(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("game capabilities config unreadable (%s): %s", path, exc)
        table = {}
    _CACHED = table
    return table


def supports_game_capability(app_id: int | str, capability_name: str) -> bool:
    """True when *app_id* has *capability_name* enabled in the JSON config."""
    name = str(capability_name or "").strip()
    if not name:
        return False
    key = _app_id_key(app_id)
    if not key:
        return False
    caps = _load_games_table().get(key)
    if not isinstance(caps, Mapping):
        return False
    return _parse_bool(caps.get(name))


__all__ = [
    "CAPABILITY_NORMALIZE_MOD_FOLDER_NAME",
    "game_capabilities_config_path",
    "reset_game_capabilities_cache",
    "set_game_capabilities_config_path",
    "supports_game_capability",
]
