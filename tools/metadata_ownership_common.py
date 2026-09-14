"""Shared helpers for Metadata Ownership Isolation Recovery."""

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
BACKUP_DIR_NAME = "mod_backup"

CODE_METADATA_FOREIGN_OWNER = "METADATA_FOREIGN_OWNER"
CODE_INFO_METADATA_POLLUTED = "INFO_METADATA污染"
CODE_BACKUP_FOREIGN_OWNER = "BACKUP_FOREIGN_OWNER"
CODE_BACKUP_LEGACY_KEY = "BACKUP_LEGACY_KEY_RISK"

DECISION_APPROVE = "APPROVE"

_NEXUS_SLUG_APP: dict[str, int] = {
    "stardewvalley": 413150,
    "baldursgate3": 1086940,
    "cyberpunk2077": 1091500,
    "witcher3": 292030,
    "palworld": 1623730,
    "kingdomcomedeliverance2": 1771300,
    "skyrimspecialedition": 489830,
}

# Description text markers used only as *foreign content evidence* (not identity).
_APP_DESC_MARKERS: dict[int, tuple[str, ...]] = {
    413150: ("stardew valley", "星露谷", "星露谷物语"),
    1086940: ("baldur's gate 3", "baldurs gate 3", "博德之门3", "博德之门３"),
    1091500: ("cyberpunk 2077", "赛博朋克2077"),
    292030: ("the witcher 3", "巫师3", "巫师３"),
}


def text(value: Any) -> str:
    return str(value or "").strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nexus_slug(url: str) -> str:
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


def app_id_from_url(url: str) -> int:
    return int(_NEXUS_SLUG_APP.get(nexus_slug(url), 0) or 0)


def dump_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def default_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    return (
        root / "data" / "mod_manager.db",
        root / "mod",
        root / "data",
        root / "tools" / "_audit_out",
    )


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


def write_info_patch(info_path: Path, updates: dict[str, Any]) -> None:
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
            payload.pop(key, None)
        else:
            payload[key] = value
    # Canonical filesystem binding key is entity_key (never dual-write legacy).
    payload, _ = normalize_info_entity_key_payload(payload)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
            "description",
            "custom_description",
            "source_url",
            "last_known_path",
            "cover_path",
            "preview_url",
        ]
        select = ", ".join(c for c in want if c in cols)
        return [dict(r) for r in con.execute(f"SELECT {select} FROM mods")]
    finally:
        con.close()


def entity_summary(row: dict[str, Any]) -> dict[str, Any]:
    mid = text(row.get("mod_id"))
    return {
        "internal_id": text(row.get("internal_id")) or mid,
        "mod_id": mid,
        "app_id": int(row.get("app_id") or 0),
        "workspace_id": text(row.get("workspace_id")),
        "external_id": text(row.get("external_id")),
        "title": text(row.get("title") or row.get("display_name")),
        "source_url": text(row.get("source_url")),
        "description_len": len(text(row.get("description"))),
        "custom_description_len": len(text(row.get("custom_description"))),
        "last_known_path": text(row.get("last_known_path")),
        "cover_path": text(row.get("cover_path")),
    }


def metadata_app_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    url = text(payload.get("url") or payload.get("source_url"))
    try:
        app = int(payload.get("app_id") or 0)
    except (TypeError, ValueError):
        app = 0
    url_app = app_id_from_url(url)
    return {
        "metadata_app_id": app or url_app,
        "metadata_url": url,
        "metadata_title": text(payload.get("title") or payload.get("display_name")),
        "metadata_source": text(payload.get("platform") or payload.get("source_type")),
        "declared_app_id": app,
        "url_app_id": url_app,
        "description_len": len(text(payload.get("description"))),
        "custom_description_len": len(
            text(payload.get("custom_description") or payload.get("description"))
        ),
    }


_DESC_HASH_RE = re.compile(r"\s+")


def desc_fingerprint(text_value: str) -> str:
    raw = _DESC_HASH_RE.sub(" ", text(text_value)).strip().casefold()
    if len(raw) < 40:
        return ""
    return raw[:240]


def description_foreign_app_markers(description: str, *, entity_app_id: int) -> int:
    """
    Return another app_id when *description* strongly names that game and not
    the entity's game. Used as pollution evidence only — never as identity.
    """
    body = text(description).casefold()
    if len(body) < 20:
        return 0
    entity = int(entity_app_id or 0)
    own_hit = False
    if entity in _APP_DESC_MARKERS:
        own_hit = any(m.casefold() in body for m in _APP_DESC_MARKERS[entity])
    foreign_hits: list[int] = []
    for app, markers in _APP_DESC_MARKERS.items():
        if app == entity:
            continue
        if any(m.casefold() in body for m in markers):
            foreign_hits.append(app)
    if foreign_hits and not own_hit:
        return foreign_hits[0]
    return 0
