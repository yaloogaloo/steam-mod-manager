"""Shared helpers for one-shot Identity full rebuild (disk → clean Identity).

Not part of the production runtime path. Never modifies Identity Lifecycle Contract.
Never treats folder name / external_id / published_file_id as entity identity.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_LIBRARY = ROOT / "mod"
DEFAULT_DB = ROOT / "data" / "mod_manager.db"

INFO_DIR = ".info"
METADATA = "metadata.json"
LEGACY_INFO_DIR = "info"
LEGACY_METADATA = "mod.json"

POLLUTION_DIR_RE = re.compile(r"^(.+)_900000000000\d*$")
POLLUTION_ID_RE = re.compile(r"^900000000000\d+$")

ACTION_REBUILD = "REBUILD"
ACTION_DELETE_CANDIDATE = "DELETE_CANDIDATE"
ACTION_SKIP = "SKIP"


def text(value: Any) -> str:
    return str(value or "").strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_pollution_dirname(name: str) -> bool:
    return bool(POLLUTION_DIR_RE.match(text(name)))


def is_pollution_id(value: Any) -> bool:
    return bool(POLLUTION_ID_RE.match(text(value)))


def new_internal_id() -> str:
    return str(uuid.uuid4())


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data


def read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
    for info_name, meta_name in (
        (INFO_DIR, METADATA),
        (LEGACY_INFO_DIR, LEGACY_METADATA),
        (INFO_DIR, LEGACY_METADATA),
        (LEGACY_INFO_DIR, METADATA),
    ):
        meta = folder / info_name / meta_name
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"_read_error": True}, str(meta)
        if isinstance(data, dict):
            return data, str(meta)
        return {"_read_error": True}, str(meta)
    return None, ""


def write_info_entity_key(info_path: Path, internal_id: str) -> None:
    """Replace ``.info/entity_key`` only — never invent workspace from path.

    ``entity_key`` value must be Entity ``internal_id`` (not a third Mod ID).
    """
    from services.mod_identity import set_entity_key

    payload: dict[str, Any] = {}
    if info_path.is_file():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except Exception:  # noqa: BLE001
            payload = {}
    # Drop legacy PK pollution mirrors; entity proof is UUID only.
    payload.pop("mod_id", None)
    payload = set_entity_key(payload, text(internal_id))
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# Legacy alias — canonical name is :func:`write_info_entity_key`.
write_info_internal_id = write_info_entity_key


def load_game_name_to_app_id(db_path: Path) -> dict[str, int]:
    if not db_path.is_file():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
        if "name" not in cols or "app_id" not in cols:
            return {}
        out: dict[str, int] = {}
        for row in con.execute("SELECT app_id, name FROM games"):
            app_id = int(row[0] or 0)
            name = text(row[1])
            if app_id > 0 and name:
                out[name] = app_id
        return out
    finally:
        con.close()


def resolve_app_id(
    *,
    info: dict[str, Any] | None,
    game_folder: str,
    name_to_app: dict[str, int],
) -> int:
    """Game scope from ``.info.app_id`` or games.name match — never mod folder name."""
    try:
        info_app = int((info or {}).get("app_id") or 0)
    except (TypeError, ValueError):
        info_app = 0
    if info_app > 0:
        return info_app
    return int(name_to_app.get(text(game_folder), 0) or 0)


def content_file_count(folder: Path) -> int:
    count = 0
    try:
        for child in folder.rglob("*"):
            if not child.is_file():
                continue
            parts = {p.lower() for p in child.relative_to(folder).parts}
            if INFO_DIR in parts or LEGACY_INFO_DIR in parts:
                continue
            count += 1
            if count >= 50:
                break
    except OSError:
        return 0
    return count


def iter_mod_folders(library: Path) -> list[Path]:
    if not library.is_dir():
        return []
    out: list[Path] = []
    for game_dir in sorted(library.iterdir(), key=lambda p: p.name.lower()):
        if not game_dir.is_dir() or game_dir.name.startswith("."):
            continue
        if game_dir.name.startswith("_"):
            continue
        for child in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                out.append(child)
    return out


def scan_disk_entries(
    library: Path,
    *,
    db_path: Path,
) -> list[dict[str, Any]]:
    name_to_app = load_game_name_to_app_id(db_path)
    entries: list[dict[str, Any]] = []
    for folder in iter_mod_folders(library):
        info, info_path = read_info(folder)
        game = folder.parent.name
        app_id = resolve_app_id(
            info=info if info and not info.get("_read_error") else None,
            game_folder=game,
            name_to_app=name_to_app,
        )
        ws = text((info or {}).get("workspace_id")) if info else ""
        title = ""
        current_iid = ""
        platform = ""
        if info and not info.get("_read_error"):
            from services.mod_identity import read_entity_key

            title = text(info.get("title") or info.get("display_name"))
            # Prefer entity_key; legacy sidecar key internal_id accepted.
            current_iid = text(read_entity_key(info))
            platform = text(info.get("platform") or info.get("source_type")).lower()
        entries.append(
            {
                "game": game,
                "app_id": app_id,
                "workspace_id": ws,
                "title": title,
                "current_internal_id": current_iid,
                "path": str(folder.resolve()),
                "info_path": info_path,
                "platform": platform,
                "pollution_dirname": is_pollution_dirname(folder.name),
                "has_info": bool(info) and not bool((info or {}).get("_read_error")),
                "read_error": bool((info or {}).get("_read_error")),
                "content_files": content_file_count(folder),
                "info": info if info and not info.get("_read_error") else None,
            }
        )
    return entries


def mark_duplicate_workspace(entries: list[dict[str, Any]]) -> None:
    """Annotate ``duplicate_workspace`` within the same app_id only."""
    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        app_id = int(entry.get("app_id") or 0)
        ws = text(entry.get("workspace_id"))
        if app_id > 0 and ws:
            groups[(app_id, ws)].append(entry)
    for entry in entries:
        app_id = int(entry.get("app_id") or 0)
        ws = text(entry.get("workspace_id"))
        if app_id > 0 and ws and len(groups[(app_id, ws)]) > 1:
            entry["duplicate_workspace"] = True
            entry["duplicate_group_size"] = len(groups[(app_id, ws)])
        else:
            entry["duplicate_workspace"] = False
            entry["duplicate_group_size"] = 1


def select_workspace_keeper(
    members: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """
    Same-app_id workspace uniqueness keep rules (ordered):

    1. Prefer non-``*_900000000000…`` directory name
    2. Prefer more content files
    3. Prefer valid ``.info``
    4. Stable path sort
    """
    if not members:
        return None, "empty"
    pool = list(members)
    normal = [m for m in pool if not m.get("pollution_dirname")]
    if normal:
        pool = normal
    pool = sorted(
        pool,
        key=lambda m: (
            -int(m.get("content_files") or 0),
            0 if m.get("has_info") else 1,
            text(m.get("path")).lower(),
        ),
    )
    return pool[0], "prefer_normal_content_info"


def plan_workspace_actions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Phase 2+3 planning: REBUILD / DELETE_CANDIDATE / SKIP."""
    mark_duplicate_workspace(entries)
    planned: list[dict[str, Any]] = []
    used_uuids: set[str] = set()

    by_key: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    singles: list[dict[str, Any]] = []
    for entry in entries:
        app_id = int(entry.get("app_id") or 0)
        ws = text(entry.get("workspace_id"))
        if app_id > 0 and ws and entry.get("duplicate_workspace"):
            by_key[(app_id, ws)].append(entry)
        else:
            singles.append(entry)

    def _mint() -> str:
        while True:
            iid = new_internal_id()
            if iid not in used_uuids:
                used_uuids.add(iid)
                return iid

    def _row_for(
        entry: dict[str, Any],
        *,
        action: str,
        keep_reason: str = "",
        new_iid: str = "",
    ) -> dict[str, Any]:
        old_identity = {
            "internal_id": text(entry.get("current_internal_id")),
            "workspace_id": text(entry.get("workspace_id")),
            "app_id": int(entry.get("app_id") or 0),
            "path": text(entry.get("path")),
            "title": text(entry.get("title")),
            "platform": text(entry.get("platform")),
        }
        return {
            "path": text(entry.get("path")),
            "info_path": text(entry.get("info_path")),
            "game": text(entry.get("game")),
            "app_id": int(entry.get("app_id") or 0),
            "workspace_id": text(entry.get("workspace_id")),
            "title": text(entry.get("title")),
            "platform": text(entry.get("platform")),
            "old_identity": old_identity,
            "new_internal_id": new_iid,
            "action": action,
            "keep_reason": keep_reason,
            "duplicate_workspace": bool(entry.get("duplicate_workspace")),
            "pollution_dirname": bool(entry.get("pollution_dirname")),
            "content_files": int(entry.get("content_files") or 0),
            "has_info": bool(entry.get("has_info")),
            "info": entry.get("info"),
        }

    for entry in singles:
        app_id = int(entry.get("app_id") or 0)
        ws = text(entry.get("workspace_id"))
        if not entry.get("has_info"):
            planned.append(_row_for(entry, action=ACTION_SKIP, keep_reason="no_info"))
            continue
        if app_id <= 0:
            planned.append(
                _row_for(entry, action=ACTION_SKIP, keep_reason="missing_app_id")
            )
            continue
        if not ws:
            # Registration number missing — mint a fresh workspace token,
            # never from folder name / internal_id / external_id.
            from core.mod_platform import generate_unique_workspace_id

            ws = generate_unique_workspace_id(set())
            entry = dict(entry)
            entry["workspace_id"] = ws
        planned.append(
            _row_for(
                entry,
                action=ACTION_REBUILD,
                keep_reason="unique_workspace",
                new_iid=_mint(),
            )
        )

    for (_app_id, _ws), members in sorted(
        by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        keeper, reason = select_workspace_keeper(members)
        if keeper is None:
            for member in members:
                planned.append(
                    _row_for(member, action=ACTION_SKIP, keep_reason="ambiguous_dup")
                )
            continue
        keep_path = text(keeper.get("path"))
        for member in members:
            if text(member.get("path")) == keep_path:
                planned.append(
                    _row_for(
                        member,
                        action=ACTION_REBUILD,
                        keep_reason=reason,
                        new_iid=_mint(),
                    )
                )
            else:
                planned.append(
                    _row_for(
                        member,
                        action=ACTION_DELETE_CANDIDATE,
                        keep_reason=f"dup_of:{keep_path}",
                    )
                )

    planned.sort(key=lambda r: (int(r.get("app_id") or 0), text(r.get("path")).lower()))
    return planned
