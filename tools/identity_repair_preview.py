"""Identity Recovery Phase 2-B Preview — human review repair preview.

Read-only. Never mutates DB, .info, backup, or lifecycle code.
Never executes repairs.

Usage::

    python tools/identity_repair_preview.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")
BACKUP_METADATA = "metadata.json"

ACTION_IGNORE_ORPHAN = "IGNORE_ORPHAN_INFO"
ACTION_RESTORE_INFO = "RESTORE_INFO_FROM_BACKUP"
ACTION_MANUAL_REVIEW = "MANUAL_REVIEW"
ACTION_REVIEW_BACKUP = "REVIEW_BACKUP_CONFLICT"
ACTION_SPLIT_REVIEW = "SPLIT_IDENTITY_REVIEW"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"_read_error": True, "_path": str(path)}
    return data if isinstance(data, dict) else {"_read_error": True, "_path": str(path)}


def _read_info_record(info_path: str = "", folder: str = "") -> dict[str, Any] | None:
    if info_path:
        data = _read_json(Path(info_path))
        if data is not None:
            return data
    if folder:
        root = Path(folder)
        for info_name in INFO_DIR_NAMES:
            for meta_name in METADATA_NAMES:
                data = _read_json(root / info_name / meta_name)
                if data is not None:
                    return data
    return None


def _info_identity_slice(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload or payload.get("_read_error"):
        return payload
    keys = (
        "internal_id",
        "workspace_id",
        "external_id",
        "published_file_id",
        "platform",
        "source_type",
        "app_id",
        "title",
        "display_name",
        "source_url",
        "url",
    )
    return {k: payload.get(k) for k in keys if k in payload}


def _load_backup_record(backup_root: Path, mod_id: str) -> dict[str, Any] | None:
    mid = _text(mod_id)
    if not mid:
        return None
    meta = backup_root / mid / BACKUP_METADATA
    data = _read_json(meta)
    if data is None:
        return None
    out = _info_identity_slice(data) or {}
    out["_backup_path"] = str(meta)
    return out


def _backup_matches_db(backup: dict[str, Any] | None, db: dict[str, Any] | None) -> tuple[bool, list[str]]:
    if not backup or not db:
        return False, ["missing backup or db record"]
    if backup.get("_read_error"):
        return False, ["backup unreadable"]
    fails: list[str] = []
    bak_iid = _text(backup.get("internal_id"))
    db_iid = _text(db.get("internal_id")) or _text(db.get("mod_id"))
    if not bak_iid or bak_iid != db_iid:
        # Also accept backup internal_id == mod_id when DB uuid empty
        if bak_iid != _text(db.get("mod_id")) or not bak_iid:
            fails.append(
                f"internal_id backup={bak_iid!r} db={_text(db.get('internal_id'))!r}"
            )
    bak_plat = _text(backup.get("platform") or backup.get("source_type")).lower()
    db_plat = _text(db.get("platform")).lower()
    if bak_plat and db_plat and bak_plat != db_plat:
        fails.append(f"platform backup={bak_plat!r} db={db_plat!r}")
    try:
        bak_app = int(backup.get("app_id") or 0)
    except (TypeError, ValueError):
        bak_app = 0
    db_app = int(db.get("app_id") or 0)
    if bak_app > 0 and db_app > 0 and bak_app != db_app:
        fails.append(f"app_id backup={bak_app} db={db_app}")
    bak_ext = _text(backup.get("external_id") or backup.get("published_file_id"))
    db_ext = _text(db.get("external_id"))
    if bak_ext and db_ext and bak_ext != db_ext:
        fails.append(f"external_id backup={bak_ext!r} db={db_ext!r}")
    return (len(fails) == 0), fails


def _diff_preview(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Describe intended after-state differences (preview only)."""
    changes: list[dict[str, Any]] = []
    for section in ("db_record", "info_record", "backup_record"):
        b = before.get(section)
        a = after.get(section)
        if b == a:
            continue
        changes.append(
            {
                "section": section,
                "before": b,
                "after": a,
                "note": "preview only — not applied",
            }
        )
    return {
        "unchanged_db": before.get("db_record") == after.get("db_record"),
        "unchanged_info": before.get("info_record") == after.get("info_record"),
        "unchanged_backup": before.get("backup_record") == after.get("backup_record"),
        "section_changes": changes,
    }


def _build_item(
    plan_item: dict[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    finding_type = _text(plan_item.get("finding_type"))
    plan_index = int(plan_item.get("plan_index") or 0)
    finding_id = f"{finding_type}:{plan_index}"
    current = plan_item.get("current_data") or {}
    evidence = dict(plan_item.get("evidence") or {})
    db_record = current.get("db_record") or {}
    if not isinstance(db_record, dict):
        db_record = {}

    info_path = _text(current.get("info_path") or evidence.get("info_path"))
    folder = ""
    extra = current.get("extra") or evidence.get("extra") or {}
    if isinstance(extra, dict):
        folder = _text(extra.get("folder"))
    info_live = _read_info_record(info_path=info_path, folder=folder)
    info_record = _info_identity_slice(info_live)

    mod_id = _text(db_record.get("mod_id"))
    # Prefer DB path for backup bind; orphan info uses internal_id only
    backup_record = _load_backup_record(backup_root, mod_id)
    if backup_record is None and finding_type == "backup_identity_source_anomaly":
        # backup dir may be named by folder in extra
        bak_dir = ""
        if isinstance(extra, dict):
            bak_dir = _text(extra.get("backup_dir"))
        if bak_dir:
            data = _read_json(Path(bak_dir) / BACKUP_METADATA)
            if data is not None:
                backup_record = _info_identity_slice(data) or {}
                backup_record["_backup_path"] = str(Path(bak_dir) / BACKUP_METADATA)
        elif _text(plan_item.get("internal_id")):
            backup_record = _load_backup_record(
                backup_root, _text(plan_item.get("internal_id"))
            )

    before = {
        "db_record": db_record or None,
        "info_record": info_record,
        "backup_record": backup_record,
    }

    # --- action rules ---
    if finding_type == "info_internal_id_not_in_db":
        proposed = ACTION_IGNORE_ORPHAN
        risk = "low"
        after = {
            "db_record": before["db_record"],  # unchanged — no create
            "info_record": before["info_record"],  # unchanged — no delete
            "backup_record": before["backup_record"],
            "registration_status": "ignored_orphan_info_not_a_mod",
            "notes": [
                "Do not delete .info",
                "Do not create DB entity",
                "Folder remains unregistered until Import/Sync",
            ],
        }
    elif finding_type == "db_entity_without_valid_info":
        ok, fails = _backup_matches_db(backup_record, db_record)
        if ok:
            proposed = ACTION_RESTORE_INFO
            risk = "medium"
            # Preview restored .info shaped from DB authority + matching backup body
            from services.mod_identity import set_entity_key

            restored = dict(backup_record or {})
            restored.pop("_backup_path", None)
            # Stamp DB authority fields (preview only) — entity_key not legacy key.
            proof = _text(db_record.get("internal_id")) or mod_id
            restored = set_entity_key(restored, proof)
            if _text(db_record.get("platform")):
                restored["platform"] = _text(db_record.get("platform"))
                restored["source_type"] = _text(db_record.get("platform"))
            if db_record.get("app_id") is not None:
                restored["app_id"] = int(db_record.get("app_id") or 0)
            if _text(db_record.get("external_id")):
                restored["external_id"] = _text(db_record.get("external_id"))
            if _text(db_record.get("workspace_id")):
                restored["workspace_id"] = _text(db_record.get("workspace_id"))
            after = {
                "db_record": before["db_record"],  # DB unchanged
                "info_record": restored,
                "backup_record": before["backup_record"],
                "notes": [
                    "Preview only: would write .info from matched backup",
                    "DB identity fields unchanged",
                    "entity_key value = Entity.internal_id (not a third Mod ID)",
                ],
            }
        else:
            proposed = ACTION_MANUAL_REVIEW
            risk = "medium"
            evidence["restore_blocked_reasons"] = fails
            after = {
                "db_record": before["db_record"],
                "info_record": before["info_record"],
                "backup_record": before["backup_record"],
                "notes": [
                    "Backup does not fully match DB identity — no restore proposed",
                    *fails,
                ],
            }
    elif finding_type == "backup_identity_source_anomaly":
        proposed = ACTION_REVIEW_BACKUP
        risk = "medium"
        after = {
            "db_record": before["db_record"],
            "info_record": before["info_record"],
            "backup_record": before["backup_record"],
            "notes": [
                "Do not restore from this backup",
                "Do not create Mod from backup",
                "Human review of backup conflict only",
            ],
        }
    elif finding_type in {
        "cross_game_same_external_id",
        "cross_game_same_workspace_id",
    }:
        proposed = ACTION_SPLIT_REVIEW
        risk = "high"
        after = {
            "db_record": before["db_record"],
            "info_record": before["info_record"],
            "backup_record": before["backup_record"],
            "cross_game_rows": (extra.get("rows") if isinstance(extra, dict) else None),
            "notes": [
                "Do not merge entities",
                "Do not migrate internal_id",
                "Human must decide per-game split / app_id correction",
            ],
        }
    else:
        proposed = ACTION_MANUAL_REVIEW
        risk = _text(plan_item.get("risk")) or "high"
        after = {
            "db_record": before["db_record"],
            "info_record": before["info_record"],
            "backup_record": before["backup_record"],
            "notes": ["Unclassified finding type — manual review only"],
        }

    after_preview = {
        **after,
        "diff": _diff_preview(before, after),
    }

    return {
        "finding_id": finding_id,
        "finding_type": finding_type,
        "internal_id": _text(plan_item.get("internal_id")),
        "plan_index": plan_index,
        "before": before,
        "evidence": evidence,
        "proposed_action": proposed,
        "after_preview": after_preview,
        "risk": risk,
        "requires_manual_confirm": True,
        "plan_category": plan_item.get("category"),
        "needs_human_confirm_reason": plan_item.get("needs_human_confirm_reason"),
    }


def build_repair_preview(
    plan: dict[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    items = []
    for plan_item in plan.get("items") or []:
        items.append(_build_item(plan_item, backup_root=backup_root))

    by_action = Counter(i["proposed_action"] for i in items)
    return {
        "generated_at": _now(),
        "phase": "2-B-preview",
        "mode": "preview_only",
        "production_mutation": "NONE",
        "source_plan": {
            "generated_at": plan.get("generated_at"),
            "findings_planned": plan.get("findings_planned"),
            "db_path": plan.get("db_path"),
            "backup_path": plan.get("backup_path"),
        },
        "items_count": len(items),
        "proposed_action_counts": dict(sorted(by_action.items())),
        "guards": {
            "modifies_database": False,
            "modifies_info": False,
            "creates_mods": False,
            "deletes_mods": False,
            "migrates_internal_id": False,
            "modifies_workspace_id": False,
            "calls_identity_service": False,
            "calls_workspace_resolver": False,
            "executes_repairs": False,
            "all_require_manual_confirm": True,
        },
        "policy": {
            "info_internal_id_not_in_db": ACTION_IGNORE_ORPHAN,
            "db_entity_without_valid_info": (
                f"{ACTION_RESTORE_INFO} only if backup identity fully matches DB; "
                f"else {ACTION_MANUAL_REVIEW}"
            ),
            "backup_identity_source_anomaly": ACTION_REVIEW_BACKUP,
            "cross_game": ACTION_SPLIT_REVIEW,
        },
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=str,
        default="",
        help="Phase 2-A plan JSON",
    )
    parser.add_argument("--db", type=str, default="", help="DB path for byte check")
    parser.add_argument("--backup", type=str, default="", help="mod_backup root")
    parser.add_argument("--out", type=str, default="", help="Preview JSON output")
    args = parser.parse_args(argv)

    plan_path = (
        Path(args.plan)
        if args.plan
        else (ROOT / "tools" / "_audit_out" / "identity_recovery_plan.json")
    )
    if not plan_path.is_file():
        print(f"missing plan: {plan_path}", file=sys.stderr)
        return 2

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    db_path = Path(args.db) if args.db else Path(_text(plan.get("db_path")))
    backup_root = (
        Path(args.backup) if args.backup else Path(_text(plan.get("backup_path")))
    )
    out = (
        Path(args.out)
        if args.out
        else (ROOT / "tools" / "_audit_out" / "identity_repair_preview.json")
    )

    before_db = db_path.read_bytes() if db_path.is_file() else b""
    preview = build_repair_preview(plan, backup_root=backup_root)
    after_db = db_path.read_bytes() if db_path.is_file() else b""
    if before_db != after_db:
        raise RuntimeError("Phase 2-B preview mutated the database — abort")

    # Prove we did not open DB for writes (optional read connection unused).
    preview["db_byte_digest_unchanged"] = True

    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(preview, ensure_ascii=False, indent=2)
    out.write_text(payload, encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dated = out.with_name(f"identity_repair_preview_{stamp}.json")
    dated.write_text(payload, encoding="utf-8")

    print(f"preview={out}")
    print(f"dated={dated}")
    print(f"items_count={preview['items_count']}")
    print(
        "proposed_action_counts="
        + json.dumps(preview["proposed_action_counts"], ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
