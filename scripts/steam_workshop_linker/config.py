"""Load games.json and mapped_games.json. No SMM runtime imports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class ConfigError(ValueError):
    """Invalid linker config or mapped-games record."""


@dataclass(frozen=True)
class GameConfig:
    name: str
    app_id: str
    workshop_root: Path
    smm_mod_root: Path


def _require_text(raw: dict, key: str, *, context: str) -> str:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{context} missing field {key!r}")
    text = str(value).strip()
    if not text:
        raise ConfigError(f"{context} field {key!r} is empty")
    return text


def load_games(path: Path) -> list[GameConfig]:
    if not path.is_file():
        raise ConfigError(f"games.json not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("games.json must be an object")
    games_raw = payload.get("games")
    if not isinstance(games_raw, list) or not games_raw:
        raise ConfigError("games.json requires a non-empty games array")

    games: list[GameConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(games_raw):
        context = f"games[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be an object")
        name = _require_text(item, "name", context=context)
        app_id = _require_text(item, "app_id", context=context)
        workshop = _require_text(item, "workshop_root", context=context)
        smm = _require_text(item, "smm_mod_root", context=context)
        if app_id in seen:
            raise ConfigError(f"duplicate app_id {app_id!r}")
        seen.add(app_id)
        games.append(
            GameConfig(
                name=name,
                app_id=app_id,
                workshop_root=Path(workshop).expanduser(),
                smm_mod_root=Path(smm).expanduser(),
            )
        )
    return games


def load_mapped_games(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ConfigError(f"mapped_games.json is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("mapped_games.json must be an object")
    mapped = payload.get("mapped_games", {})
    if mapped is None:
        return {}
    if not isinstance(mapped, dict):
        raise ConfigError("mapped_games must be an object keyed by app_id")
    out: dict[str, dict] = {}
    for key, value in mapped.items():
        app_id = str(key).strip()
        if not app_id:
            continue
        out[app_id] = value if isinstance(value, dict) else {}
    return out


def mapped_record(game: GameConfig, *, mapped_at: str | None = None) -> dict[str, str]:
    when = mapped_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "name": game.name,
        "mapped_at": when,
        "smm_mod_root": str(game.smm_mod_root),
        "workshop_root": str(game.workshop_root),
    }


def save_mapped_game(path: Path, game: GameConfig, existing: dict[str, dict] | None = None) -> dict[str, dict]:
    """Atomically record that *game* was processed. History only — not FS truth."""
    records = dict(existing if existing is not None else load_mapped_games(path))
    records[game.app_id] = mapped_record(game)
    atomic_write_json(path, {"mapped_games": records})
    return records


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
