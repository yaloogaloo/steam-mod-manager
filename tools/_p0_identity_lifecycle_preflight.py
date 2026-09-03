#!/usr/bin/env python3
"""PHASE A: read-only production identity preflight. Never mutates production.

Does not call core.paths.data_dir() / default_mod_library() (those mkdir).
Writes reports only under docs/ (not DB, not sidecars, not managed folders).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mod_platform import is_internal_mod_id, normalize_platform  # noqa: E402
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME  # noqa: E402
from services.identity_invariants import (  # noqa: E402
    classify_production_id_row,
    scan_id_architecture_source,
    scan_reconcile_identity_lifecycle,
)
from services.identity_repair import open_readonly_sqlite  # noqa: E402
from services.mod_identity import source_url_embeds_internal  # noqa: E402

PROD_DB = ROOT / "data" / "mod_manager.db"
PROD_LIB = ROOT / "mod"
PROD_DATA = ROOT / "data"
FOCUS_WS = "10782"
FOCUS_INTERNAL_3054 = "9000000000003054"
FOCUS_INTERNAL_3456 = "9000000000003456"
EMPTY_MOD_TOKEN = "Empty Mod 0798b9fd"
COLLISION_WS = ("10782", "1720", "6183", "97")
OUT_DIR = ROOT / "docs"
PREFLIGHT_PATH = OUT_DIR / "P0_IDENTITY_LIFECYCLE_PREFLIGHT.json"
PLAN_PATH = OUT_DIR / "P0_IDENTITY_LIFECYCLE_PRODUCTION_REPAIR_PLAN.json"


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sidecar_identity(folder: Path) -> dict:
    meta = folder / INFO_DIR_NAME / METADATA_FILENAME
    offline = folder / INFO_DIR_NAME / "offline" / "metadata.json"
    data = _read_json(meta) or {}
    off = _read_json(offline) or {}
    files = []
    try:
        files = sorted(p.name for p in folder.iterdir())
    except OSError:
        files = []
    content_files = [
        name
        for name in files
        if name not in {INFO_DIR_NAME, "info", "历史版本"}
    ]
    return {
        "folder": str(folder),
        "exists": folder.is_dir(),
        "sidecar_path": str(meta) if meta.is_file() else "",
        "sidecar_exists": meta.is_file(),
        "offline_exists": offline.is_file(),
        "child_names": files,
        "content_names": content_files,
        "content_count": len(content_files),
        "url": data.get("url") or data.get("source_url") or "",
        "workspace_id": data.get("workspace_id") or "",
        "published_file_id": data.get("published_file_id") or "",
        "external_id": data.get("external_id") or "",
        "platform": data.get("platform") or data.get("source_type") or "",
        "internal_id": data.get("internal_id") or "",
        "title": data.get("title") or data.get("display_name") or "",
        "identity_status": data.get("identity_status") or "",
        "offline_original_url": off.get("original_url") or "",
        "offline_source_file": off.get("source_file") or "",
    }


def _folder_has_payload(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    skip = {INFO_DIR_NAME, "info", "历史版本"}
    try:
        for child in folder.iterdir():
            if child.name in skip:
                continue
            if child.is_file() and child.stat().st_size > 0:
                return True
            if child.is_dir():
                return True
    except OSError:
        return False
    return False


def _text_hit(text: str, needles: tuple[str, ...]) -> bool:
    blob = str(text or "")
    return any(n in blob for n in needles)


def scan_db(conn: sqlite3.Connection) -> dict:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()]
    want = [
        "mod_id",
        "app_id",
        "title",
        "display_name",
        "platform",
        "external_id",
        "workspace_id",
        "source_url",
        "last_known_path",
        "folder_present",
        "internal_id",
        "library_status",
        "content_status",
        "source_type",
        "updated_at",
    ]
    select = ", ".join(c for c in want if c in cols)
    rows = [_row_dict(r) for r in conn.execute(f"SELECT {select} FROM mods")]
    by_ws: dict[str, list[dict]] = {}
    internal_url = []
    pub_eq_internal = []
    steam_pk_10782 = None
    for row in rows:
        mid = str(row.get("mod_id") or "")
        ws = str(row.get("workspace_id") or "").strip()
        url = str(row.get("source_url") or "")
        if ws:
            by_ws.setdefault(ws, []).append(
                {
                    "internal_id": mid,
                    "platform": row.get("platform"),
                    "app_id": row.get("app_id"),
                    "external_id": row.get("external_id"),
                    "last_known_path": row.get("last_known_path"),
                }
            )
        if url and is_internal_mod_id(mid) and source_url_embeds_internal(url, internal_pk=mid):
            internal_url.append(
                {
                    "internal_id": mid,
                    "workspace_id": ws,
                    "platform": row.get("platform"),
                    "app_id": row.get("app_id"),
                    "source_url": url,
                    "title": row.get("title") or row.get("display_name"),
                    "last_known_path": row.get("last_known_path"),
                    "classify": classify_production_id_row(row),
                }
            )
        if mid == FOCUS_WS:
            steam_pk_10782 = row
    collisions = {
        ws: items
        for ws, items in by_ws.items()
        if len(items) > 1 and ws in COLLISION_WS or (len(items) > 1 and ws.isdigit())
    }
    # Keep named collisions plus all numeric multi-row workspace_ids
    numeric_collisions = {
        ws: items for ws, items in by_ws.items() if len(items) > 1
    }
    focus_10782 = [
        r
        for r in rows
        if str(r.get("workspace_id") or "").strip() == FOCUS_WS
        or str(r.get("mod_id") or "") == FOCUS_WS
        or str(r.get("external_id") or "").strip() == FOCUS_WS
        or _text_hit(str(r.get("source_url") or ""), ("/mods/10782", "id=10782"))
    ]
    focus_3054 = [
        r for r in rows if str(r.get("mod_id") or "") == FOCUS_INTERNAL_3054
    ]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    audit = []
    if "identity_audit_log" in tables:
        for mid in (FOCUS_INTERNAL_3456, FOCUS_INTERNAL_3054, FOCUS_WS):
            logs = conn.execute(
                """
                SELECT created_at, field_name, old_value, new_value, source, reason
                FROM identity_audit_log
                WHERE CAST(mod_id AS TEXT)=?
                ORDER BY id DESC LIMIT 20
                """,
                (mid,),
            ).fetchall()
            audit.append({"internal_id": mid, "log": [_row_dict(x) for x in logs]})
    return {
        "mods_count": len(rows),
        "tables": tables,
        "steam_pk_10782": steam_pk_10782,
        "workspace_10782_rows": focus_10782,
        "internal_3054_rows": focus_3054,
        "internal_id_steam_urls": internal_url,
        "named_workspace_collisions": {
            k: numeric_collisions.get(k, []) for k in COLLISION_WS
        },
        "all_workspace_collisions_count": len(numeric_collisions),
        "all_workspace_collision_ids": sorted(numeric_collisions.keys())[:50],
        "audit_focus": audit,
        "classify_3054": (
            classify_production_id_row(focus_3054[0]) if focus_3054 else "ABSENT"
        ),
        "classify_10782_rows": [
            {
                "internal_id": str(r.get("mod_id")),
                "status": classify_production_id_row(r),
            }
            for r in focus_10782
        ],
    }


def scan_filesystem(conn: sqlite3.Connection) -> dict:
    hits: list[dict] = []
    empty_mod: list[dict] = []
    needles = (
        "10782",
        "9000000000003054",
        "9000000000003456",
        "/mods/10782",
        "id=10782",
        "id=9000000000003054",
        "id=9000000000003456",
    )
    if not PROD_LIB.is_dir():
        return {"library_exists": False, "hits": [], "empty_mod": []}
    for game_dir in sorted(PROD_LIB.iterdir()):
        if not game_dir.is_dir():
            continue
        try:
            children = list(game_dir.iterdir())
        except OSError:
            continue
        for folder in children:
            if not folder.is_dir():
                continue
            name = folder.name
            if EMPTY_MOD_TOKEN.lower() in name.lower() or name.startswith("Empty Mod"):
                empty_mod.append(_sidecar_identity(folder))
            meta = folder / INFO_DIR_NAME / METADATA_FILENAME
            data = _read_json(meta) or {}
            blob = json.dumps(data, ensure_ascii=False)
            if (
                _text_hit(blob, needles)
                or _text_hit(name, ("10782", "Flashbacks", EMPTY_MOD_TOKEN, "BetterUI"))
                or str(data.get("workspace_id") or "") == FOCUS_WS
                or str(data.get("published_file_id") or "")
                in {FOCUS_WS, FOCUS_INTERNAL_3054, FOCUS_INTERNAL_3456}
            ):
                info = _sidecar_identity(folder)
                info["game_folder"] = game_dir.name
                info["has_payload"] = _folder_has_payload(folder)
                hits.append(info)
    # Dedup by folder path
    seen: set[str] = set()
    uniq = []
    for item in hits:
        key = item["folder"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return {
        "library_exists": True,
        "library_root": str(PROD_LIB),
        "focus_folder_hits": uniq,
        "empty_mod_folders": empty_mod,
    }


def scan_backups() -> dict:
    backup_root = PROD_DATA / "backups"
    out = {"backup_root_exists": backup_root.is_dir(), "focus": []}
    if not backup_root.is_dir():
        return out
    for mid in (FOCUS_INTERNAL_3456, FOCUS_INTERNAL_3054, FOCUS_WS):
        d = backup_root / mid
        meta = d / "metadata.json"
        data = _read_json(meta) or {}
        out["focus"].append(
            {
                "internal_id": mid,
                "dir_exists": d.is_dir(),
                "metadata_exists": meta.is_file(),
                "url": data.get("url") or data.get("source_url") or "",
                "workspace_id": data.get("workspace_id") or "",
                "published_file_id": data.get("published_file_id") or "",
                "platform": data.get("platform") or data.get("source_type") or "",
                "internal_id_field": data.get("internal_id") or "",
            }
        )
    return out


def isolated_reconcile_check(focus_folders: list[Path]) -> dict:
    """Copy DB + relevant folders to temp; reconcile; never touch production."""
    import tempfile

    from core.db_manager import DatabaseManager
    from services.library_reconcile import reconcile_library
    from services.mod_metadata_resolver import list_visible_mods

    tmp = Path(tempfile.mkdtemp(prefix="smm_p0_lifecycle_iso_"))
    iso_data = tmp / "data"
    iso_lib = tmp / "mod"
    iso_data.mkdir(parents=True)
    iso_lib.mkdir(parents=True)
    iso_db = iso_data / "mod_manager.db"
    shutil.copy2(PROD_DB, iso_db)
    copied = []
    for folder in focus_folders:
        if not folder.is_dir():
            continue
        try:
            rel = folder.resolve().relative_to(PROD_LIB.resolve())
        except ValueError:
            continue
        dest = iso_lib / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, dest)
        copied.append(str(rel))
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(iso_db)
    before = {}
    with db._lock:
        for mid in (FOCUS_INTERNAL_3456, FOCUS_INTERNAL_3054, FOCUS_WS):
            row = db._conn.execute(
                """
                SELECT mod_id, platform, workspace_id, source_url, external_id
                FROM mods WHERE CAST(mod_id AS TEXT)=?
                """,
                (mid,),
            ).fetchone()
            before[mid] = dict(row) if row else None
        ws_rows = db._conn.execute(
            "SELECT mod_id, platform, workspace_id FROM mods WHERE TRIM(COALESCE(workspace_id,''))='10782'"
        ).fetchall()
        before["ws_10782"] = [dict(r) for r in ws_rows]
        before["mods_count"] = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]

    # Patch path helpers used by reconcile orphan scan.
    import core.paths as paths_mod
    import services.metadata_backup as bak_mod

    orig_data = paths_mod.data_dir
    orig_lib = paths_mod.default_mod_library
    orig_bak = bak_mod.data_dir
    paths_mod.data_dir = lambda: iso_data
    paths_mod.default_mod_library = lambda: iso_lib
    bak_mod.data_dir = lambda: iso_data
    notes = []
    try:
        result1 = reconcile_library(iso_lib)
        result2 = reconcile_library(iso_lib)
        notes = list(result1.notes)[:40]
    finally:
        paths_mod.data_dir = orig_data
        paths_mod.default_mod_library = orig_lib
        bak_mod.data_dir = orig_bak

    after = {}
    with db._lock:
        for mid in (FOCUS_INTERNAL_3456, FOCUS_INTERNAL_3054, FOCUS_WS):
            row = db._conn.execute(
                """
                SELECT mod_id, platform, workspace_id, source_url, external_id
                FROM mods WHERE CAST(mod_id AS TEXT)=?
                """,
                (mid,),
            ).fetchone()
            after[mid] = dict(row) if row else None
        ws_rows = db._conn.execute(
            "SELECT mod_id, platform, workspace_id FROM mods WHERE TRIM(COALESCE(workspace_id,''))='10782'"
        ).fetchall()
        after["ws_10782"] = [dict(r) for r in ws_rows]
        after["mods_count"] = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]

    visible = {}
    try:
        for game in iso_lib.iterdir():
            if game.is_dir():
                cards = list_visible_mods(iso_lib, game.name)
                visible[game.name] = [
                    {
                        "internal_id": str(c.published_file_id),
                        "workspace_id": str(c.workspace_id),
                        "title": str(c.title or c.display_name or ""),
                    }
                    for c in cards
                ]
    except Exception as exc:  # noqa: BLE001
        visible = {"error": str(exc)}

    steam_dup = after.get(FOCUS_WS) is not None
    url_3054 = ""
    if after.get(FOCUS_INTERNAL_3054):
        url_3054 = str(after[FOCUS_INTERNAL_3054].get("source_url") or "")
    rehydrated = source_url_embeds_internal(
        url_3054, internal_pk=FOCUS_INTERNAL_3054
    )
    count_delta = int(after["mods_count"]) - int(before["mods_count"])
    DatabaseManager.reset_instance()
    # Keep tmp for inspection? User said disposable. Delete after capturing result.
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "copied_folders": copied,
        "before": _jsonable(before),
        "after": _jsonable(after),
        "reconcile_imported_pass1": result1.imported,
        "reconcile_imported_pass2": result2.imported,
        "reconcile_notes_sample": notes,
        "visible_cards": visible,
        "steam_pk_10782_created": steam_dup,
        "mods_count_delta": count_delta,
        "source_url_3054_rehydrated": rehydrated,
        "source_url_3054_after": url_3054,
        "ws_10782_count_before": len(before["ws_10782"]),
        "ws_10782_count_after": len(after["ws_10782"]),
        "SAFE_NO_STEAM_DUPLICATE": not steam_dup,
        "SAFE_NO_REHYDRATION": not rehydrated,
        "SAFE_NO_NET_INSERT": count_delta == 0,
    }


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, sqlite3.Row):
        return dict(obj)
    return obj


def classify_empty_mod(item: dict, row_3456: dict | None) -> str:
    pub = str(item.get("published_file_id") or "")
    ws = str(item.get("workspace_id") or "")
    uuid = str(item.get("internal_id") or "")
    has_payload = item.get("content_count", 0) > 0
    if row_3456:
        rid = str(row_3456.get("mod_id"))
        rws = str(row_3456.get("workspace_id") or "")
        if pub == rid or ws == rws or (uuid and uuid == str(row_3456.get("internal_id") or "")):
            if not has_payload:
                return "RESIDUAL_FOLDER"
            return "SAME_ENTITY"
    if not ws and not item.get("url") and not has_payload:
        return "UNRESOLVED"
    if not has_payload:
        return "RESIDUAL_FOLDER"
    return "MANUAL_REVIEW"


def build_plan(preflight: dict) -> dict:
    actions: list[dict] = []
    db = preflight["db"]
    fs = preflight["filesystem"]
    iso = preflight["isolated_reconcile"]
    row_3456 = None
    if db["workspace_10782_rows"]:
        for r in db["workspace_10782_rows"]:
            if str(r.get("mod_id")) == FOCUS_INTERNAL_3456:
                row_3456 = r
                break
    row_3054 = db["internal_3054_rows"][0] if db["internal_3054_rows"] else None

    # 10782 nexus entity
    if row_3456:
        actions.append(
            {
                "internal_id": FOCUS_INTERNAL_3456,
                "workspace_id": str(row_3456.get("workspace_id") or ""),
                "platform": str(row_3456.get("platform") or ""),
                "app_id": row_3456.get("app_id"),
                "filesystem_path": str(row_3456.get("last_known_path") or ""),
                "sidecar_path": "",
                "current_values": {
                    "workspace_id": row_3456.get("workspace_id"),
                    "platform": row_3456.get("platform"),
                    "external_id": row_3456.get("external_id"),
                    "source_url": row_3456.get("source_url"),
                },
                "detected_problem": "none — legal Nexus Workspace ID 10782"
                if str(row_3456.get("platform") or "").lower() == "nexus"
                else "workspace 10782 platform not nexus",
                "classification": "NO_ACTION"
                if str(row_3456.get("platform") or "").lower() == "nexus"
                else "MANUAL_REVIEW",
                "proposed_mutation": "none",
                "evidence": "platform=nexus workspace_id=10782 is the user-facing Mod ID; Steam PK 10782 is ABSENT",
                "confidence": "high",
                "mutation_risk": "none",
            }
        )
        sidecars_3456 = [
            h
            for h in fs["focus_folder_hits"]
            if str(h.get("published_file_id") or "")
            in {FOCUS_INTERNAL_3456, FOCUS_WS}
            or str(h.get("workspace_id") or "") == FOCUS_WS
        ]
        for hit in sidecars_3456:
            pub = str(hit.get("published_file_id") or "")
            if pub == FOCUS_INTERNAL_3456 and str(hit.get("platform") or "").lower() != "steam":
                actions.append(
                    {
                        "internal_id": FOCUS_INTERNAL_3456,
                        "workspace_id": str(hit.get("workspace_id") or row_3456.get("workspace_id")),
                        "platform": str(row_3456.get("platform") or ""),
                        "app_id": row_3456.get("app_id"),
                        "filesystem_path": hit["folder"],
                        "sidecar_path": hit.get("sidecar_path") or "",
                        "current_values": {
                            "published_file_id": pub,
                            "url": hit.get("url"),
                            "workspace_id": hit.get("workspace_id"),
                        },
                        "detected_problem": "sidecar published_file_id equals Internal ID; not Steam proof",
                        "classification": "SCRUB_INVALID_LEGACY_FIELD",
                        "proposed_mutation": "clear sidecar published_file_id when it equals internal_id; do not change workspace_id/platform",
                        "evidence": f"published_file_id={pub} == internal_id; platform!=steam",
                        "confidence": "high",
                        "mutation_risk": "low — sidecar legacy field only",
                    }
                )

    if db["steam_pk_10782"] is None:
        actions.append(
            {
                "internal_id": FOCUS_WS,
                "workspace_id": FOCUS_WS,
                "platform": "steam",
                "app_id": None,
                "filesystem_path": "",
                "sidecar_path": "",
                "current_values": {"mods_row": "ABSENT"},
                "detected_problem": "none — Steam PK 10782 does not exist",
                "classification": "NO_ACTION",
                "proposed_mutation": "none; do not create",
                "evidence": "isolated reconcile steam_pk_10782_created="
                + str(iso.get("steam_pk_10782_created")),
                "confidence": "high",
                "mutation_risk": "none",
            }
        )

    if row_3054:
        url = str(row_3054.get("source_url") or "")
        polluted = source_url_embeds_internal(url, internal_pk=FOCUS_INTERNAL_3054)
        actions.append(
            {
                "internal_id": FOCUS_INTERNAL_3054,
                "workspace_id": str(row_3054.get("workspace_id") or ""),
                "platform": str(row_3054.get("platform") or ""),
                "app_id": row_3054.get("app_id"),
                "filesystem_path": str(row_3054.get("last_known_path") or ""),
                "sidecar_path": "",
                "current_values": {
                    "source_url": url,
                    "platform": row_3054.get("platform"),
                    "workspace_id": row_3054.get("workspace_id"),
                    "external_id": row_3054.get("external_id"),
                },
                "detected_problem": "DB source_url embeds Internal ID as Steam filedetails id"
                if polluted
                else "DB source_url already clean",
                "classification": "SCRUB_INVALID_SOURCE_URL" if polluted else "NO_ACTION",
                "proposed_mutation": "set mods.source_url='' ; do not change workspace_id/platform"
                if polluted
                else "none",
                "evidence": url,
                "confidence": "high",
                "mutation_risk": "low — field scrub only"
                if polluted
                else "none",
            }
        )
        for hit in fs["focus_folder_hits"]:
            if FOCUS_INTERNAL_3054 in str(hit.get("folder") or "") or str(
                hit.get("published_file_id") or ""
            ) == FOCUS_INTERNAL_3054:
                surl = str(hit.get("url") or "")
                pub = str(hit.get("published_file_id") or "")
                if source_url_embeds_internal(surl, internal_pk=FOCUS_INTERNAL_3054) or pub == FOCUS_INTERNAL_3054:
                    actions.append(
                        {
                            "internal_id": FOCUS_INTERNAL_3054,
                            "workspace_id": str(row_3054.get("workspace_id") or ""),
                            "platform": str(row_3054.get("platform") or ""),
                            "app_id": row_3054.get("app_id"),
                            "filesystem_path": hit["folder"],
                            "sidecar_path": hit.get("sidecar_path") or "",
                            "current_values": {
                                "sidecar_url": surl,
                                "published_file_id": pub,
                            },
                            "detected_problem": "sidecar Internal-ID Steam URL and/or published_file_id==internal_id",
                            "classification": "SCRUB_INVALID_SOURCE_URL"
                            if source_url_embeds_internal(surl, internal_pk=FOCUS_INTERNAL_3054)
                            else "SCRUB_INVALID_LEGACY_FIELD",
                            "proposed_mutation": "clear sidecar url if Internal-ID Steam URL; clear published_file_id if == internal_id; keep workspace_id/platform",
                            "evidence": f"url={surl} published_file_id={pub}",
                            "confidence": "high",
                            "mutation_risk": "low — sidecar pollution scrub; required to prevent historical rehydrate path",
                        }
                    )

    for item in fs.get("empty_mod_folders") or []:
        kind = classify_empty_mod(item, row_3456)
        actions.append(
            {
                "internal_id": FOCUS_INTERNAL_3456 if kind in {"RESIDUAL_FOLDER", "SAME_ENTITY"} else "",
                "workspace_id": str(item.get("workspace_id") or ""),
                "platform": str(item.get("platform") or ""),
                "app_id": row_3456.get("app_id") if row_3456 else None,
                "filesystem_path": item.get("folder"),
                "sidecar_path": item.get("sidecar_path"),
                "current_values": {
                    "published_file_id": item.get("published_file_id"),
                    "workspace_id": item.get("workspace_id"),
                    "internal_id": item.get("internal_id"),
                    "content_count": item.get("content_count"),
                    "title": item.get("title"),
                },
                "detected_problem": f"Empty Mod folder classified as {kind}",
                "classification": "MANUAL_REVIEW"
                if kind not in {"RESIDUAL_FOLDER", "SAME_ENTITY"}
                else "NO_ACTION",
                "proposed_mutation": "do not delete; treat as extra filesystem observation of existing entity"
                if kind in {"RESIDUAL_FOLDER", "SAME_ENTITY"}
                else "do not delete without operator confirmation",
                "evidence": json.dumps(
                    {
                        "pub": item.get("published_file_id"),
                        "ws": item.get("workspace_id"),
                        "content": item.get("content_names"),
                    },
                    ensure_ascii=False,
                ),
                "confidence": "medium" if kind in {"RESIDUAL_FOLDER", "SAME_ENTITY"} else "low",
                "mutation_risk": "high if deleted — forbidden without explicit Empty Mod approval",
            }
        )

    for ws, items in (db.get("named_workspace_collisions") or {}).items():
        if ws == FOCUS_WS:
            continue
        if len(items) <= 1:
            actions.append(
                {
                    "internal_id": items[0]["internal_id"] if items else "",
                    "workspace_id": ws,
                    "platform": items[0]["platform"] if items else "",
                    "app_id": items[0]["app_id"] if items else None,
                    "filesystem_path": items[0]["last_known_path"] if items else "",
                    "sidecar_path": "",
                    "current_values": {"rows": items},
                    "detected_problem": "no multi-row collision",
                    "classification": "NO_ACTION",
                    "proposed_mutation": "none",
                    "evidence": "single row for this workspace_id",
                    "confidence": "high",
                    "mutation_risk": "none",
                }
            )
            continue
        plats = {str(i.get("platform") or "") for i in items}
        apps = {str(i.get("app_id") or "") for i in items}
        same_entity = len(plats) == 1 and len(apps) == 1
        actions.append(
            {
                "internal_id": ",".join(str(i["internal_id"]) for i in items),
                "workspace_id": ws,
                "platform": ",".join(sorted(plats)),
                "app_id": None,
                "filesystem_path": "",
                "sidecar_path": "",
                "current_values": {"rows": items},
                "detected_problem": "workspace_id shared by multiple internal rows",
                "classification": "MANUAL_REVIEW",
                "proposed_mutation": "none — collision is not proof of duplicate entity; do not merge/delete/mint",
                "evidence": f"platforms={sorted(plats)} app_ids={sorted(apps)} same_context={same_entity}",
                "confidence": "low",
                "mutation_risk": "high",
            }
        )

    auto = [
        a
        for a in actions
        if a["classification"]
        in {"SCRUB_INVALID_SOURCE_URL", "SCRUB_INVALID_LEGACY_FIELD"}
        and a["confidence"] == "high"
    ]
    return {
        "plan_id": "P0-IDENTITY-LIFECYCLE-PRODUCTION-REPAIR-PLAN",
        "generated_at": _utc(),
        "isolated_reconcile_safe": bool(
            iso.get("SAFE_NO_STEAM_DUPLICATE")
            and iso.get("SAFE_NO_REHYDRATION")
            and iso.get("SAFE_NO_NET_INSERT")
        ),
        "apply_authorized_if_isolated_safe": True,
        "automatic_apply_classifications": [
            "SCRUB_INVALID_SOURCE_URL",
            "SCRUB_INVALID_LEGACY_FIELD",
        ],
        "forbidden_without_manual_approval": [
            "REMOVE_INVALID_DUPLICATE",
            "REBIND_EXISTING_ENTITY",
            "RESTORE_EXISTING_IDENTITY",
            "QUARANTINE_UNRESOLVED",
        ],
        "actions": actions,
        "automatic_count": len(auto),
        "manual_review_count": len(
            [a for a in actions if a["classification"] == "MANUAL_REVIEW"]
        ),
        "no_action_count": len([a for a in actions if a["classification"] == "NO_ACTION"]),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    static_lifecycle = [
        {"code": f.violation_code, "entity": f.entity_id, "evidence": f.evidence}
        for f in scan_reconcile_identity_lifecycle(ROOT)
    ]
    static_arch = [
        {"code": f.violation_code, "entity": f.entity_id, "evidence": f.evidence}
        for f in scan_id_architecture_source(ROOT)
    ]
    if not PROD_DB.is_file():
        print("PRODUCTION DB missing", PROD_DB)
        return 2
    db = open_readonly_sqlite(PROD_DB)
    conn = db._conn  # noqa: SLF001
    try:
        db_scan = scan_db(conn)
        fs_scan = scan_filesystem(conn)
    finally:
        conn.close()
    backup_scan = scan_backups()
    focus_paths = [Path(h["folder"]) for h in fs_scan.get("focus_folder_hits") or []]
    for extra in fs_scan.get("empty_mod_folders") or []:
        p = Path(extra["folder"])
        if p not in focus_paths:
            focus_paths.append(p)
    iso = isolated_reconcile_check(focus_paths)
    preflight = {
        "generated_at": _utc(),
        "phase": "A",
        "production_mutation": "NONE",
        "production_db": str(PROD_DB),
        "production_library": str(PROD_LIB),
        "static_guards_lifecycle": static_lifecycle,
        "static_guards_architecture": static_arch,
        "static_ok": not static_lifecycle and not static_arch,
        "db": db_scan,
        "filesystem": fs_scan,
        "backups": backup_scan,
        "isolated_reconcile": iso,
    }
    PREFLIGHT_PATH.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    plan = build_plan(preflight)
    PLAN_PATH.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(
        {
            "preflight": str(PREFLIGHT_PATH),
            "plan": str(PLAN_PATH),
            "static_ok": preflight["static_ok"],
            "mods_count": db_scan["mods_count"],
            "steam_pk_10782": db_scan["steam_pk_10782"] is not None,
            "ws_10782_rows": len(db_scan["workspace_10782_rows"]),
            "internal_steam_urls": len(db_scan["internal_id_steam_urls"]),
            "empty_mod": len(fs_scan.get("empty_mod_folders") or []),
            "iso_safe_no_steam_dup": iso.get("SAFE_NO_STEAM_DUPLICATE"),
            "iso_safe_no_rehydrate": iso.get("SAFE_NO_REHYDRATION"),
            "iso_count_delta": iso.get("mods_count_delta"),
            "auto_actions": plan["automatic_count"],
            "manual_review": plan["manual_review_count"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
