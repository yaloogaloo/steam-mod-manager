from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from config import GameConfig

_SCHEMA = """
CREATE TABLE mods (
    mod_id INTEGER PRIMARY KEY,
    app_id INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT 'steam',
    external_id TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    internal_id TEXT NOT NULL DEFAULT '',
    last_known_path TEXT NOT NULL DEFAULT '',
    folder_present INTEGER NOT NULL DEFAULT 1
);
"""


def make_db(path: Path, rows: list[dict] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_SCHEMA)
        for row in rows or []:
            conn.execute(
                """
                INSERT INTO mods (
                    app_id, platform, workspace_id, internal_id,
                    last_known_path, folder_present, external_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row.get("app_id", 262060)),
                    str(row.get("platform", "steam")),
                    str(row.get("workspace_id", "")),
                    str(row.get("internal_id", "")),
                    str(row.get("last_known_path", "")),
                    int(row.get("folder_present", 1)),
                    str(row.get("external_id", row.get("workspace_id", ""))),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def write_meta(mod_dir: Path, payload: dict) -> Path:
    info = mod_dir / ".info"
    info.mkdir(parents=True, exist_ok=True)
    path = info / "metadata.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def steam_mod(
    root: Path,
    name: str,
    workspace_id: str,
    *,
    internal_id: str = "",
    extra: dict | None = None,
) -> Path:
    mod = root / name
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "content.txt").write_text(f"smm-{workspace_id}", encoding="utf-8")
    payload = {
        "workspace_id": workspace_id,
        "title": name,
        "internal_id": internal_id,
    }
    if extra:
        payload.update(extra)
    write_meta(mod, payload)
    if internal_id:
        (mod / ".info" / "internal_id").write_text(internal_id, encoding="utf-8")
    return mod


def game_pair(tmp_path: Path) -> GameConfig:
    workshop = tmp_path / "workshop"
    smm = tmp_path / "smm"
    workshop.mkdir()
    smm.mkdir()
    return GameConfig("暗黑地牢", "262060", workshop, smm)
