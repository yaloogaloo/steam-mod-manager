"""Shared helpers for Identity Collision Recovery (Phase 3).

Read-only helpers + evidence extraction. Never modifies lifecycle contracts.
Never guesses identity from folder name / path alone.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")

CODE_INTERNAL_ID_COLLISION = "INTERNAL_ID_COLLISION"
CODE_WORKSPACE_COLLISION = "WORKSPACE_COLLISION"
CODE_EXTERNAL_COLLISION = "EXTERNAL_COLLISION"
CODE_INFO_DB_MISMATCH = "INFO_DB_IDENTITY_MISMATCH"

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"

# Nexus game slug → Steam app_id (URL evidence only — never folder names).
_NEXUS_SLUG_APP_ID: dict[str, int] = {
    "stardewvalley": 413150,
    "baldursgate3": 1086940,
    "cyberpunk2077": 1091500,
    "witcher3": 292030,
    "palworld": 1623730,
    "kingdomcomedeliverance2": 1771300,
    "skyrimspecialedition": 489830,
}


def text(value: Any) -> str:
    return str(value or "").strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nexus_url_game_slug(url: str) -> str:
    raw = text(url)
    if not raw or "nexusmods.com" not in raw.lower():
        return ""
    try:
        parts = [p for p in urlparse(raw).path.split("/") if p]
        if "mods" in parts:
            idx = parts.index("mods")
            if idx > 0:
                return parts[idx - 1].lower()
    except Exception:
        return ""
    return ""


def nexus_mod_id_from_url(url: str) -> str:
    m = re.search(r"/mods/(\d+)", text(url), re.IGNORECASE)
    return m.group(1) if m else ""


def app_id_for_nexus_slug(slug: str) -> int:
    return int(_NEXUS_SLUG_APP_ID.get(text(slug).lower(), 0) or 0)


def norm_path(path: str | Path) -> str:
    raw = text(path)
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower().replace("/", "\\")
    except OSError:
        return raw.lower().replace("/", "\\")


def read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
    for info_name in INFO_DIR_NAMES:
        for meta_name in METADATA_NAMES:
            meta = folder / info_name / meta_name
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                return {"_read_error": True}, str(meta)
            if isinstance(data, dict):
                return data, str(meta)
            return {"_read_error": True}, str(meta)
    return None, ""


def write_info_patch(info_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Patch ``.info/metadata.json``; filesystem proof writes ``entity_key`` only."""
    from services.mod_identity import normalize_info_entity_key_payload

    payload: dict[str, Any] = {}
    if info_path.is_file():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
    for key, value in updates.items():
        if value is None:
            continue
        payload[key] = value
    # Canonical filesystem binding key is entity_key (never dual-write legacy).
    payload, _ = normalize_info_entity_key_payload(payload)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def iter_managed_folders(library: Path) -> list[Path]:
    if not library.is_dir():
        return []
    out: list[Path] = []
    for game_dir in sorted(library.iterdir()):
        if not game_dir.is_dir() or game_dir.name.startswith("."):
            continue
        for child in sorted(game_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.append(child)
    return out


def load_db_rows(db_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        want = [
            "mod_id",
            "internal_id",
            "app_id",
            "platform",
            "external_id",
            "workspace_id",
            "title",
            "display_name",
            "source_url",
            "last_known_path",
            "folder_present",
            "source_type",
            "identity_status",
        ]
        select = ", ".join(c for c in want if c in cols)
        rows = con.execute(f"SELECT {select} FROM mods").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def db_summary(row: dict[str, Any]) -> dict[str, Any]:
    mid = text(row.get("mod_id"))
    iid = text(row.get("internal_id")) or mid
    return {
        "mod_id": mid,
        "internal_id": iid,
        "app_id": int(row.get("app_id") or 0),
        "platform": text(row.get("platform") or row.get("source_type")),
        "external_id": text(row.get("external_id")),
        "workspace_id": text(row.get("workspace_id")),
        "title": text(row.get("title") or row.get("display_name")),
        "source_url": text(row.get("source_url")),
        "last_known_path": text(row.get("last_known_path")),
        "folder_present": int(row.get("folder_present") or 0),
    }


def info_summary(
    payload: dict[str, Any], folder: Path, info_path: str
) -> dict[str, Any]:
    from services.mod_identity import read_entity_key

    url = text(payload.get("url") or payload.get("source_url"))
    try:
        app_id = int(payload.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    slug = nexus_url_game_slug(url)
    if app_id <= 0 and slug:
        app_id = app_id_for_nexus_slug(slug)
    return {
        "folder": str(folder),
        "info_path": info_path,
        # Report field: filesystem binding value (entity_key / legacy sidecar key).
        "internal_id": read_entity_key(payload),
        "title": text(payload.get("title") or payload.get("display_name")),
        "source_url": url,
        "external_id": text(
            payload.get("external_id") or payload.get("published_file_id")
        ),
        "workspace_id": text(payload.get("workspace_id")),
        "platform": text(payload.get("platform") or payload.get("source_type")),
        "app_id": app_id,
        "nexus_slug": slug,
        "game_folder": folder.parent.name if folder.parent else "",
    }


def identity_fingerprint(rec: dict[str, Any]) -> str:
    """Stable evidence fingerprint — never folder name."""
    return "|".join(
        [
            text(rec.get("platform")).lower(),
            str(int(rec.get("app_id") or 0)),
            text(rec.get("external_id")),
            text(rec.get("source_url")).lower(),
            text(rec.get("workspace_id")),
            text(rec.get("title")).casefold(),
        ]
    )


def entity_key(row: dict[str, Any]) -> str:
    """Canonical entity key for indexing: internal_id column or mod_id PK."""
    return text(row.get("internal_id")) or text(row.get("mod_id"))


def dump_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def default_paths(root: Path) -> tuple[Path, Path, Path]:
    db = root / "data" / "mod_manager.db"
    library = root / "mod"
    out_dir = root / "tools" / "_audit_out"
    return db, library, out_dir
