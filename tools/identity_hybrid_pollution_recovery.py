"""Identity historical pollution recovery — hybrid / cross-game merge undo.

Phases
------
1. ``report``  — read-only scan (DB + .info disk evidence)
2. ``plan``    — KEEP_ENTITY / SPLIT_ENTITY / MANUAL_REVIEW only
3. ``apply``   — execute approved SPLIT_ENTITY items (--apply --confirm)

Hard rules
----------
- Does NOT modify Import / Sync / IdentityService create / Projection /
  workspace_id rules.
- Never auto-merge. Never auto-delete.
- Never invent identity from title / folder name alone.
- Identity evidence: DB fields + ``.info`` ``source_url`` / ``internal_id`` /
  ``external_id`` / ``workspace_id`` only.
- SPLIT creates a new Internal ID (``mod_id``) and rebinds ``.info`` + path.

Usage::

    python tools/identity_hybrid_pollution_recovery.py report
    python tools/identity_hybrid_pollution_recovery.py plan --report tools/_audit_out/hybrid_report.json
    python tools/identity_hybrid_pollution_recovery.py apply --plan tools/_audit_out/hybrid_plan.json --apply --confirm
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")

ACTION_KEEP = "KEEP_ENTITY"
ACTION_SPLIT = "SPLIT_ENTITY"
ACTION_MANUAL = "MANUAL_REVIEW"

CODE_CROSS_GAME = "cross_game_same_external_id"
CODE_HYBRID = "hybrid_entity"
CODE_IDENTITY_CONFLICT = "identity_conflict"
CODE_INFO_CONFLICT = "info_metadata_conflict"

# Nexus game slug → Steam app_id (official URL evidence only; not folder names).
_NEXUS_SLUG_APP_ID: dict[str, int] = {
    "stardewvalley": 413150,
    "baldursgate3": 1086940,
    "cyberpunk2077": 1091500,
    "witcher3": 292030,
    "palworld": 1623730,
    "kingdomcomedeliverance2": 1771300,
    "skyrimspecialedition": 489830,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nexus_url_game_slug(url: str) -> str:
    text = _text(url)
    if not text or "nexusmods.com" not in text.lower():
        return ""
    try:
        parts = [p for p in urlparse(text).path.split("/") if p]
        if "mods" in parts:
            idx = parts.index("mods")
            if idx > 0:
                return parts[idx - 1].lower()
    except Exception:
        return ""
    return ""


def nexus_mod_id_from_url(url: str) -> str:
    text = _text(url)
    m = re.search(r"/mods/(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else ""


def app_id_for_nexus_slug(slug: str) -> int:
    return int(_NEXUS_SLUG_APP_ID.get(_text(slug).lower(), 0) or 0)


def _read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
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


def _info_summary(payload: dict[str, Any], folder: Path, info_path: str) -> dict[str, Any]:
    from services.mod_identity import read_entity_key

    url = _text(payload.get("url") or payload.get("source_url"))
    return {
        "folder": str(folder),
        "info_path": info_path,
        # Report: filesystem binding (entity_key / legacy sidecar key).
        "internal_id": read_entity_key(payload),
        "title": _text(payload.get("title") or payload.get("display_name")),
        "display_name": _text(payload.get("display_name")),
        "source_url": url,
        "external_id": _text(payload.get("external_id")),
        "workspace_id": _text(payload.get("workspace_id")),
        "platform": _text(payload.get("platform") or payload.get("source_type")),
        "app_id": int(payload.get("app_id") or 0),
        "nexus_slug": nexus_url_game_slug(url),
        "nexus_mod_id": nexus_mod_id_from_url(url)
        or _text(payload.get("external_id") or payload.get("workspace_id")),
    }


def _db_entity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "internal_id": _text(row.get("internal_id")) or _text(row.get("mod_id")),
        "mod_id": _text(row.get("mod_id")),
        "platform": _text(row.get("platform")),
        "app_id": int(row.get("app_id") or 0),
        "external_id": _text(row.get("external_id")),
        "workspace_id": _text(row.get("workspace_id")),
        "title": _text(row.get("title") or row.get("display_name")),
        "source_url": _text(row.get("source_url")),
        "path": _text(row.get("last_known_path")),
        "nexus_slug": nexus_url_game_slug(_text(row.get("source_url"))),
    }


def _norm_path(path: str) -> str:
    text = _text(path)
    if not text:
        return ""
    try:
        return str(Path(text).resolve()).lower().replace("/", "\\")
    except OSError:
        return text.lower().replace("/", "\\")


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
        ]
        select = ", ".join(c for c in want if c in cols)
        rows = con.execute(f"SELECT {select} FROM mods").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def scan_disk_info(library: Path) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for folder in iter_managed_folders(library):
        payload, info_path = _read_info(folder)
        if payload is None or payload.get("_read_error"):
            continue
        evidence.append(_info_summary(payload, folder, info_path))
    return evidence


def _finding(
    *,
    code: str,
    reason: str,
    before: dict[str, Any],
    merged_two_real_entities: bool | None,
    disk_evidence: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": code,
        "reason": reason,
        "merged_two_real_entities": merged_two_real_entities,
        "before": before,
        "disk_evidence": disk_evidence or [],
    }
    if extra:
        out["extra"] = extra
    return out


def build_report(*, db_path: Path, library: Path) -> dict[str, Any]:
    rows = load_db_rows(db_path)
    disk = scan_disk_info(library)
    findings: list[dict[str, Any]] = []

    by_nexus_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in disk:
        nid = _text(ev.get("nexus_mod_id"))
        if nid.isdigit():
            by_nexus_id[nid].append(ev)

    by_internal = {
        _text(r.get("internal_id")): r
        for r in rows
        if _text(r.get("internal_id"))
    }

    # --- cross_game_same_external_id (2+ DB rows) ---
    ext_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        plat = _text(r.get("platform")).lower()
        ext = _text(r.get("external_id"))
        if not plat or not ext or ext.startswith("local/"):
            continue
        ext_groups[(plat, ext)].append(r)
    for (plat, ext), group in ext_groups.items():
        apps = {int(g.get("app_id") or 0) for g in group}
        if len(group) > 1 and len(apps) > 1:
            findings.append(
                _finding(
                    code=CODE_CROSS_GAME,
                    reason=f"(platform={plat}, external_id={ext}) spans app_ids={sorted(apps)}",
                    merged_two_real_entities=False,
                    before={"db_entities": [_db_entity(g) for g in group]},
                    disk_evidence=by_nexus_id.get(ext, []),
                    extra={"platform": plat, "external_id": ext, "app_ids": sorted(apps)},
                )
            )

    # --- hybrid entity ---
    for r in rows:
        plat = _text(r.get("platform")).lower()
        ext = _text(r.get("external_id"))
        if plat != "nexus" or not ext.isdigit():
            continue
        entity = _db_entity(r)
        related = by_nexus_id.get(ext, [])
        slugs = sorted(
            {
                s
                for s in (
                    [_text(entity.get("nexus_slug"))]
                    + [_text(e.get("nexus_slug")) for e in related]
                )
                if s
            }
        )
        path_ev = None
        path_key = _norm_path(entity["path"])
        if path_key:
            for e in related:
                if _norm_path(e["folder"]) == path_key:
                    path_ev = e
                    break
            if path_ev is None and Path(entity["path"]).is_dir():
                payload, ip = _read_info(Path(entity["path"]))
                if payload and not payload.get("_read_error"):
                    path_ev = _info_summary(payload, Path(entity["path"]), ip)

        info_conflict = False
        if path_ev is not None:
            db_title = entity["title"].casefold()
            info_title = _text(path_ev.get("title")).casefold()
            info_display = _text(path_ev.get("display_name")).casefold()
            if info_title and db_title and info_title != db_title:
                info_conflict = True
            elif info_display and db_title and info_display != db_title and info_title == db_title:
                info_conflict = True
            db_slug = _text(entity.get("nexus_slug"))
            info_slug = _text(path_ev.get("nexus_slug"))
            if db_slug and info_slug and db_slug != info_slug:
                info_conflict = True

        foreign_infos = [
            e
            for e in related
            if _text(e.get("nexus_slug"))
            and _text(e.get("nexus_slug")) != _text(entity.get("nexus_slug"))
        ]
        title_matches_foreign = bool(
            path_ev is not None
            and entity["title"]
            and any(
                entity["title"].casefold() == _text(f.get("title")).casefold()
                or entity["title"].casefold()
                == _text(f.get("display_name")).casefold()
                for f in foreign_infos
            )
        )
        # Bound .info title vs DB title + foreign slug evidence → merged
        bound_title_mismatch = bool(
            path_ev is not None
            and _text(path_ev.get("title"))
            and entity["title"]
            and _text(path_ev.get("title")).casefold() != entity["title"].casefold()
        )
        merged = bool(foreign_infos) and (
            info_conflict or title_matches_foreign or bound_title_mismatch
        )

        if merged or (len(slugs) >= 2 and path_ev is not None and foreign_infos and bound_title_mismatch):
            findings.append(
                _finding(
                    code=CODE_HYBRID,
                    reason=(
                        f"single DB entity external_id={ext} has disk .info URLs "
                        f"across Nexus slugs {slugs}; bound path metadata conflicts"
                    ),
                    merged_two_real_entities=True,
                    before={"db_entity": entity, "bound_info": path_ev},
                    disk_evidence=related,
                    extra={"nexus_slugs": slugs, "foreign_infos": foreign_infos},
                )
            )
        elif info_conflict and path_ev is not None:
            findings.append(
                _finding(
                    code=CODE_INFO_CONFLICT,
                    reason="DB entity fields disagree with bound .info metadata",
                    merged_two_real_entities=False,
                    before={"db_entity": entity, "bound_info": path_ev},
                    disk_evidence=[path_ev],
                )
            )

        if path_ev is not None:
            info_iid = _text(path_ev.get("internal_id"))
            db_iid = _text(entity.get("internal_id"))
            db_mid = _text(entity.get("mod_id"))
            if (
                info_iid
                and db_iid
                and info_iid != db_iid
                and info_iid != db_mid
                and info_iid not in by_internal
            ):
                findings.append(
                    _finding(
                        code=CODE_IDENTITY_CONFLICT,
                        reason=(
                            f"bound .info internal_id={info_iid!r} != "
                            f"DB internal_id={db_iid!r} / mod_id={db_mid!r}"
                        ),
                        merged_two_real_entities=None,
                        before={"db_entity": entity, "bound_info": path_ev},
                        disk_evidence=[path_ev],
                    )
                )

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for f in findings:
        key = (
            _text(f.get("code")),
            _text((f.get("before") or {}).get("db_entity", {}).get("mod_id"))
            or _text(str((f.get("before") or {}).get("db_entities"))),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    return {
        "phase": 1,
        "generated_at": _now(),
        "db_path": str(db_path.resolve()),
        "library": str(library.resolve()),
        "production_mutation": "NONE",
        "counts": {
            "db_rows": len(rows),
            "disk_info": len(disk),
            "findings": len(unique),
            "by_code": {
                code: sum(1 for f in unique if f.get("code") == code)
                for code in (
                    CODE_CROSS_GAME,
                    CODE_HYBRID,
                    CODE_IDENTITY_CONFLICT,
                    CODE_INFO_CONFLICT,
                )
            },
        },
        "findings": unique,
    }


def build_plan(report: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for finding in report.get("findings") or []:
        code = _text(finding.get("code"))
        before = finding.get("before") or {}
        disk = list(finding.get("disk_evidence") or [])
        merged = finding.get("merged_two_real_entities")

        if code == CODE_CROSS_GAME:
            items.append(
                {
                    "action": ACTION_KEEP,
                    "finding_code": code,
                    "reason": (
                        "Multiple DB entities already exist for the same external_id "
                        "across games — keep both; no merge/split required"
                    ),
                    "before": before,
                    "approved": False,
                }
            )
            continue

        if code == CODE_HYBRID and merged:
            entity = before.get("db_entity") or {}
            bound = before.get("bound_info") or {}
            foreign = list((finding.get("extra") or {}).get("foreign_infos") or [])
            split_targets = []
            for fr in foreign:
                slug = _text(fr.get("nexus_slug"))
                aid = app_id_for_nexus_slug(slug)
                if not slug or aid <= 0:
                    continue
                split_targets.append(
                    {
                        "disk_info": fr,
                        "app_id": aid,
                        "platform": "nexus",
                        "external_id": _text(
                            fr.get("nexus_mod_id") or entity.get("external_id")
                        ),
                        "source_url": _text(fr.get("source_url")),
                        "title": _text(fr.get("title")),
                        "info_internal_id": _text(fr.get("internal_id")),
                        "path": _text(fr.get("folder")),
                    }
                )
            by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for t in split_targets:
                by_slug[nexus_url_game_slug(t["source_url"])].append(t)

            primary_targets: list[dict[str, Any]] = []
            manual_extras: list[dict[str, Any]] = []
            for _slug, group in by_slug.items():
                primary_targets.append(group[0])
                manual_extras.extend(group[1:])

            keep_fix = {
                "mod_id": entity.get("mod_id"),
                "internal_id": entity.get("internal_id"),
                "app_id": entity.get("app_id"),
                "platform": entity.get("platform"),
                "external_id": entity.get("external_id"),
                "source_url": _text(bound.get("source_url") or entity.get("source_url")),
                "title": _text(bound.get("title") or entity.get("title")),
                "path": _text(bound.get("folder") or entity.get("path")),
                "info_path": _text(bound.get("info_path")),
            }

            if not primary_targets:
                items.append(
                    {
                        "action": ACTION_MANUAL,
                        "finding_code": code,
                        "reason": (
                            "Hybrid detected but no foreign .info with mappable "
                            "Nexus URL slug"
                        ),
                        "before": before,
                        "disk_evidence": disk,
                        "approved": False,
                    }
                )
            else:
                items.append(
                    {
                        "action": ACTION_SPLIT,
                        "finding_code": code,
                        "reason": (
                            "Two real Nexus mods (different game URL slugs) share one "
                            "DB entity — keep bound path entity; create new entity for "
                            "foreign .info URL evidence"
                        ),
                        "before": before,
                        "keep": keep_fix,
                        "create_from_info": primary_targets,
                        "manual_extra_folders": manual_extras,
                        "approved": False,
                        "notes": [
                            "workspace_id may remain equal across games",
                            "external_id may remain equal across games",
                            "new entity gets a new internal mod_id",
                        ],
                    }
                )
                for extra in manual_extras:
                    items.append(
                        {
                            "action": ACTION_MANUAL,
                            "finding_code": CODE_INFO_CONFLICT,
                            "reason": (
                                "Additional folder shares foreign Nexus slug; "
                                "do not auto-bind (no title/path guessing)"
                            ),
                            "before": {
                                "disk_info": extra,
                                "parent_hybrid_mod_id": entity.get("mod_id"),
                            },
                            "approved": False,
                        }
                    )
            continue

        items.append(
            {
                "action": ACTION_MANUAL,
                "finding_code": code or "unknown",
                "reason": finding.get("reason") or "unclassified",
                "before": before,
                "disk_evidence": disk,
                "approved": False,
            }
        )

    return {
        "phase": 2,
        "generated_at": _now(),
        "source_report": report.get("generated_at"),
        "db_path": report.get("db_path"),
        "library": report.get("library"),
        "actions_allowed": [ACTION_KEEP, ACTION_SPLIT, ACTION_MANUAL],
        "forbidden": ["AUTO_DELETE", "AUTO_MERGE", "MODIFY_UNAPPROVED"],
        "counts": {
            ACTION_KEEP: sum(1 for i in items if i["action"] == ACTION_KEEP),
            ACTION_SPLIT: sum(1 for i in items if i["action"] == ACTION_SPLIT),
            ACTION_MANUAL: sum(1 for i in items if i["action"] == ACTION_MANUAL),
        },
        "items": items,
    }


def _write_info_metadata(info_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Write ``.info/metadata.json``; filesystem proof is ``entity_key`` only."""
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
    payload, _ = normalize_info_entity_key_payload(payload)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def apply_plan(
    plan: dict[str, Any],
    *,
    db_path: Path,
    library: Path,
    apply: bool,
    confirm: bool,
    rollback_dir: Path,
) -> dict[str, Any]:
    """Execute approved SPLIT_ENTITY items only."""
    _ = library
    results: list[dict[str, Any]] = []
    if not apply:
        return {
            "phase": 3,
            "applied": False,
            "reason": "dry-run (pass --apply --confirm to mutate)",
            "items": [
                {
                    "action": i.get("action"),
                    "would_apply": bool(i.get("approved"))
                    and i.get("action") == ACTION_SPLIT,
                    "before": i.get("before"),
                }
                for i in plan.get("items") or []
            ],
        }
    if not confirm:
        raise SystemExit("refusing apply without --confirm")

    from core.db_manager import DatabaseManager
    from services.identity_service import identity_create_scope

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    rollback_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    case_dir = rollback_dir / f"hybrid_split_{stamp}"
    case_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, case_dir / "mod_manager.db.bak")

    for item in plan.get("items") or []:
        action = item.get("action")
        if action != ACTION_SPLIT:
            results.append(
                {
                    "action": action,
                    "applied": False,
                    "skipped": True,
                    "reason": "not SPLIT_ENTITY",
                    "before": item.get("before"),
                }
            )
            continue
        if not item.get("approved"):
            results.append(
                {
                    "action": action,
                    "applied": False,
                    "skipped": True,
                    "reason": "not approved (set approved=true on plan item)",
                    "before": item.get("before"),
                }
            )
            continue

        before = item.get("before") or {}
        keep = item.get("keep") or {}
        creates = list(item.get("create_from_info") or [])
        entity_before = before.get("db_entity") or {}
        mid = _text(keep.get("mod_id") or entity_before.get("mod_id"))
        if not mid.isdigit():
            results.append(
                {
                    "action": action,
                    "applied": False,
                    "error": "missing keep.mod_id",
                    "before": before,
                }
            )
            continue

        keep_path = Path(_text(keep.get("path") or entity_before.get("path")))
        if keep_path.is_dir():
            dest = case_dir / "keep_info" / keep_path.name
            dest.mkdir(parents=True, exist_ok=True)
            info_src = keep_path / ".info"
            if info_src.is_dir():
                shutil.copytree(info_src, dest / ".info", dirs_exist_ok=True)

        keep_title = _text(keep.get("title"))
        keep_url = _text(keep.get("source_url"))
        with db._lock:
            if keep_title:
                db._conn.execute(
                    "UPDATE mods SET title = ?, display_name = ? WHERE mod_id = ?",
                    (keep_title, keep_title, int(mid)),
                )
            if keep_url:
                db._conn.execute(
                    "UPDATE mods SET source_url = ? WHERE mod_id = ?",
                    (keep_url, int(mid)),
                )
            db._conn.commit()
        after_row = db.get_mod_display_info(mid)
        after_keep = {
            "mod_id": mid,
            "internal_id": _text(
                getattr(after_row, "internal_id", "") or entity_before.get("internal_id")
            ),
            "app_id": int(getattr(after_row, "app_id", 0) or 0),
            "platform": _text(getattr(after_row, "platform", "")),
            "external_id": _text(getattr(after_row, "external_id", "")),
            "workspace_id": _text(getattr(after_row, "workspace_id", "")),
            "title": _text(
                getattr(after_row, "steam_name", "")
                or getattr(after_row, "title", "")
                or keep_title
            ),
            "source_url": _text(getattr(after_row, "source_url", "") or keep_url),
            "path": _text(
                getattr(after_row, "last_known_path", "") or keep.get("path")
            ),
        }

        info_path = _text(keep.get("info_path"))
        if info_path:
            _write_info_metadata(
                Path(info_path),
                {
                    "title": keep_title or None,
                    "display_name": keep_title or None,
                    "url": keep_url or None,
                    "workspace_id": _text(
                        entity_before.get("workspace_id")
                        or entity_before.get("external_id")
                    ),
                    "external_id": _text(entity_before.get("external_id")),
                    "platform": "nexus",
                    "app_id": int(entity_before.get("app_id") or 0) or None,
                    "internal_id": _text(entity_before.get("internal_id")),
                },
            )

        after_created: list[dict[str, Any]] = []
        for target in creates:
            src_url = _text(target.get("source_url"))
            slug = nexus_url_game_slug(src_url)
            app_id = int(target.get("app_id") or app_id_for_nexus_slug(slug) or 0)
            ext = _text(target.get("external_id"))
            title = _text(target.get("title"))
            folder = Path(_text(target.get("path")))
            info_iid = _text(target.get("info_internal_id")) or str(uuid.uuid4())
            if app_id <= 0 or not ext or not src_url:
                after_created.append(
                    {"error": "insufficient URL evidence", "target": target}
                )
                continue
            existing = db.find_mod_by_external("nexus", ext, app_id=app_id)
            if existing is not None:
                after_created.append(
                    {
                        "skipped": True,
                        "reason": "entity already exists for scope",
                        "mod_id": str(existing.mod_id),
                        "target": target,
                    }
                )
                continue

            with identity_create_scope():
                new_mid = int(db.allocate_mod_id())
            with db._lock:
                db._conn.execute(
                    """
                    UPDATE mods SET
                        app_id = ?,
                        platform = ?,
                        external_id = ?,
                        workspace_id = ?,
                        internal_id = ?,
                        title = ?,
                        display_name = ?,
                        source_url = ?,
                        last_known_path = ?,
                        folder_present = 1,
                        source_type = ?
                    WHERE mod_id = ?
                    """,
                    (
                        app_id,
                        "nexus",
                        ext,
                        ext,
                        info_iid,
                        title or f"Nexus Mod {ext}",
                        title or f"Nexus Mod {ext}",
                        src_url,
                        str(folder.resolve()) if folder.exists() else str(folder),
                        "nexus",
                        new_mid,
                    ),
                )
                db._conn.commit()

            meta_path = folder / ".info" / "metadata.json"
            if folder.is_dir():
                snap = case_dir / "created_info" / f"{new_mid}_{folder.name}"
                snap.mkdir(parents=True, exist_ok=True)
                if (folder / ".info").is_dir():
                    shutil.copytree(
                        folder / ".info", snap / ".info", dirs_exist_ok=True
                    )
                _write_info_metadata(
                    meta_path,
                    {
                        "internal_id": info_iid,
                        "workspace_id": ext,
                        "external_id": ext,
                        "platform": "nexus",
                        "app_id": app_id,
                        "title": title or None,
                        "display_name": title or None,
                        "url": src_url,
                    },
                )

            new_info = db.get_mod_display_info(new_mid)
            after_created.append(
                {
                    "mod_id": str(new_mid),
                    "internal_id": info_iid,
                    "app_id": app_id,
                    "platform": "nexus",
                    "external_id": ext,
                    "workspace_id": ext,
                    "title": _text(getattr(new_info, "steam_name", "") or title),
                    "source_url": src_url,
                    "path": str(folder),
                }
            )

        results.append(
            {
                "action": ACTION_SPLIT,
                "applied": True,
                "before": before,
                "after": {
                    "kept_entity": after_keep,
                    "created_entities": after_created,
                },
                "rollback_dir": str(case_dir),
            }
        )

    DatabaseManager.reset_instance()
    return {
        "phase": 3,
        "applied": True,
        "generated_at": _now(),
        "rollback_dir": str(case_dir),
        "results": results,
    }


def _default_paths() -> tuple[Path, Path, Path]:
    db = ROOT / "data" / "mod_manager.db"
    library = ROOT / "mod"
    out = ROOT / "tools" / "_audit_out"
    return db, library, out


def main(argv: list[str] | None = None) -> int:
    db_default, lib_default, out_default = _default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="Phase 1 read-only report")
    p_report.add_argument("--db", type=Path, default=db_default)
    p_report.add_argument("--library", type=Path, default=lib_default)
    p_report.add_argument("--out", type=Path, default=out_default / "hybrid_report.json")

    p_plan = sub.add_parser("plan", help="Phase 2 recovery plan")
    p_plan.add_argument("--report", type=Path, default=out_default / "hybrid_report.json")
    p_plan.add_argument("--out", type=Path, default=out_default / "hybrid_plan.json")
    p_plan.add_argument(
        "--approve-splits",
        action="store_true",
        help="Mark all SPLIT_ENTITY items approved=true",
    )

    p_apply = sub.add_parser("apply", help="Phase 3 execute approved SPLIT")
    p_apply.add_argument("--plan", type=Path, default=out_default / "hybrid_plan.json")
    p_apply.add_argument("--db", type=Path, default=db_default)
    p_apply.add_argument("--library", type=Path, default=lib_default)
    p_apply.add_argument("--out", type=Path, default=out_default / "hybrid_apply.json")
    p_apply.add_argument("--apply", action="store_true")
    p_apply.add_argument("--confirm", action="store_true")
    p_apply.add_argument(
        "--rollback-dir",
        type=Path,
        default=ROOT / "tools" / "identity_recovery_rollback",
    )

    args = parser.parse_args(argv)

    if args.cmd == "report":
        report = build_report(db_path=args.db, library=args.library)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out} findings={report['counts']['findings']}")
        return 0

    if args.cmd == "plan":
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        plan = build_plan(report)
        if args.approve_splits:
            for item in plan["items"]:
                if item.get("action") == ACTION_SPLIT:
                    item["approved"] = True
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"wrote {args.out} "
            f"KEEP={plan['counts'][ACTION_KEEP]} "
            f"SPLIT={plan['counts'][ACTION_SPLIT]} "
            f"MANUAL={plan['counts'][ACTION_MANUAL]}"
        )
        return 0

    if args.cmd == "apply":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        result = apply_plan(
            plan,
            db_path=args.db,
            library=args.library,
            apply=bool(args.apply),
            confirm=bool(args.confirm),
            rollback_dir=args.rollback_dir,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out} applied={result.get('applied')}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
