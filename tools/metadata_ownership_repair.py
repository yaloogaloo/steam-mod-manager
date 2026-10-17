#!/usr/bin/env python3
"""Metadata Ownership Isolation — repair (Audit → Preview → APPROVE → Apply).

Never auto-applies. Never mutates identity (mod_id / internal_id / workspace_id /
external_id / app_id entity keys).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.metadata_ownership_audit import run_audit  # noqa: E402
from tools.metadata_ownership_common import (  # noqa: E402
    BACKUP_DIR_NAME,
    CODE_BACKUP_FOREIGN_OWNER,
    CODE_BACKUP_LEGACY_KEY,
    CODE_INFO_METADATA_POLLUTED,
    CODE_METADATA_FOREIGN_OWNER,
    DECISION_APPROVE,
    dump_json,
    entity_summary,
    load_db_rows,
    load_json,
    now_utc,
    read_info,
    text,
    write_info_patch,
)

CASE_A = "FOREIGN_METADATA"
CASE_B = "KEEP_MATCHING_DELETE_FOREIGN"
CASE_C = "MANUAL_REVIEW"

CLEAR_TEXT_FIELDS = (
    "description",
    "custom_description",
)


def build_preview(
    *,
    db_path: Path,
    mod_root: Path,
    data_root: Path,
    audit_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = audit_report or run_audit(
        db_path=db_path, mod_root=mod_root, data_root=data_root
    )
    rows = load_db_rows(db_path)
    by_mid = {text(r.get("mod_id")): r for r in rows}

    items: dict[str, dict[str, Any]] = {}

    def _item(mid: str) -> dict[str, Any] | None:
        row = by_mid.get(mid)
        if not row:
            return None
        ent = entity_summary(row)
        key = ent["mod_id"]
        if key not in items:
            items[key] = {
                "internal_id": ent["internal_id"],
                "mod_id": ent["mod_id"],
                "app_id": ent["app_id"],
                "workspace_id": ent["workspace_id"],
                "title": ent["title"],
                "case": CASE_A,
                "manual_decision": "",
                "conflict_reasons": [],
                "actions": [],
            }
        return items[key]

    for finding in report.get("findings") or []:
        code = text(finding.get("code"))
        mid = text(finding.get("mod_id"))
        if code == CODE_BACKUP_LEGACY_KEY:
            # Legacy folder keyed by workspace digits — MANUAL_REVIEW only.
            items[f"legacy:{finding.get('workspace_id')}"] = {
                "internal_id": "",
                "mod_id": "",
                "app_id": 0,
                "workspace_id": text(finding.get("workspace_id")),
                "title": "",
                "case": CASE_C,
                "manual_decision": "",
                "conflict_reasons": [text(finding.get("conflict_reason"))],
                "actions": [
                    {
                        "op": "manual_review_legacy_backup",
                        "path": text(finding.get("metadata_source")),
                        "peer_mod_ids": finding.get("peer_mod_ids") or [],
                    }
                ],
                "finding_code": code,
            }
            continue

        if not mid:
            continue
        item = _item(mid)
        if item is None:
            continue
        reason = text(finding.get("conflict_reason"))
        if reason and reason not in item["conflict_reasons"]:
            item["conflict_reasons"].append(reason)
        item["finding_code"] = code

        meta_app = int(finding.get("metadata_app_id") or 0)
        entity_app = int(item["app_id"] or 0)

        if code in (
            CODE_METADATA_FOREIGN_OWNER,
            CODE_INFO_METADATA_POLLUTED,
            CODE_BACKUP_FOREIGN_OWNER,
        ):
            if meta_app > 0 and entity_app > 0 and meta_app != entity_app:
                item["case"] = CASE_A
                item["actions"].append(
                    {
                        "op": "clear_foreign_text_fields",
                        "targets": ["db", "info", "backup"],
                        "fields": list(CLEAR_TEXT_FIELDS),
                        "metadata_app_id": meta_app,
                    }
                )
                if "source_url" in reason or "url" in reason.lower():
                    item["actions"].append(
                        {
                            "op": "clear_foreign_url_fields",
                            "targets": ["info", "backup"],
                            "fields": ["url", "source_url", "website"],
                        }
                    )
            elif "identical" in reason or "matches peer" in reason:
                peer_mid = text(finding.get("peer_mod_id"))
                foreign_marker = int(finding.get("foreign_marker_app_id") or 0)
                meta_from_markers = int(finding.get("metadata_app_id") or 0)
                if foreign_marker or (
                    meta_from_markers > 0 and meta_from_markers != entity_app
                ):
                    item["case"] = CASE_A
                    item["actions"].append(
                        {
                            "op": "clear_foreign_text_fields",
                            "targets": ["db", "info", "backup"],
                            "fields": list(CLEAR_TEXT_FIELDS),
                            "metadata_app_id": foreign_marker or meta_from_markers,
                            "note": "cross_app_peer_description_foreign_markers",
                            "peer_mod_id": peer_mid,
                        }
                    )
                else:
                    peer_row = by_mid.get(peer_mid) if peer_mid else None
                    peer_title = text(
                        (peer_row or {}).get("title")
                        or (peer_row or {}).get("display_name")
                    )
                    self_title = text(item.get("title"))
                    desc = text(by_mid[mid].get("description")) or text(
                        by_mid[mid].get("custom_description")
                    )
                    peer_named = bool(
                        peer_title
                        and len(peer_title) >= 4
                        and peer_title.casefold() in desc.casefold()
                    )
                    self_named = bool(
                        self_title
                        and len(self_title) >= 4
                        and self_title.casefold() in desc.casefold()
                    )
                    if peer_named and not self_named:
                        item["case"] = CASE_A
                        item["actions"].append(
                            {
                                "op": "clear_foreign_text_fields",
                                "targets": ["db", "info", "backup"],
                                "fields": list(CLEAR_TEXT_FIELDS),
                                "note": "cross_app_description_names_peer_title",
                                "peer_mod_id": peer_mid,
                            }
                        )
                    else:
                        item["case"] = CASE_C
                        item["actions"].append(
                            {
                                "op": "manual_review",
                                "reason": reason,
                                "peer_mod_id": peer_mid,
                                "metadata_source": text(finding.get("metadata_source")),
                            }
                        )
            else:
                item["case"] = CASE_C
                item["actions"].append(
                    {
                        "op": "manual_review",
                        "reason": reason,
                        "metadata_source": text(finding.get("metadata_source")),
                    }
                )

    # Deduplicate actions; Case C wins if any manual_review remains without clears.
    for item in items.values():
        seen: set[str] = set()
        unique_actions = []
        for act in item["actions"]:
            key = json.dumps(act, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            unique_actions.append(act)
        item["actions"] = unique_actions
        has_clear = any(
            a.get("op") in ("clear_foreign_text_fields", "clear_foreign_url_fields")
            for a in item["actions"]
        )
        has_manual = any(a.get("op") == "manual_review" for a in item["actions"])
        if has_clear and has_manual:
            item["actions"] = [
                a for a in item["actions"] if a.get("op") != "manual_review"
            ]
            has_manual = False
        if has_clear:
            item["case"] = (
                CASE_B
                if any(a.get("metadata_app_id") for a in item["actions"])
                else CASE_A
            )
        elif has_manual or item.get("case") == CASE_C:
            item["case"] = CASE_C

    return {
        "generated_at": now_utc(),
        "db_path": str(db_path),
        "mod_root": str(mod_root),
        "data_root": str(data_root),
        "audit_finding_count": report.get("finding_count"),
        "item_count": len(items),
        "items": list(items.values()),
        "instructions": (
            "Set manual_decision=APPROVE on each item to apply. "
            "Case C (MANUAL_REVIEW) is never applied. Identity fields are never changed."
        ),
    }


def _clear_db_text(db_path: Path, mid: str, fields: list[str]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        sets = []
        params: list[Any] = []
        for f in fields:
            if f in cols:
                sets.append(f"{f} = ?")
                params.append("")
        if not sets:
            return
        params.append(mid)
        con.execute(f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?", params)
        con.commit()
    finally:
        con.close()


def _clear_info_fields(folder: Path, fields: list[str]) -> bool:
    payload, info_path = read_info(folder)
    if not payload or not info_path or payload.get("_read_error"):
        return False
    updates = {f: "" for f in fields if f in payload or f in CLEAR_TEXT_FIELDS}
    if not updates:
        return False
    write_info_patch(Path(info_path), updates)
    return True


def _clear_backup_fields(data_root: Path, mid: str, fields: list[str]) -> bool:
    meta = data_root / BACKUP_DIR_NAME / mid / "metadata.json"
    if not meta.is_file():
        return False
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    changed = False
    for f in fields:
        if f in payload and text(payload.get(f)):
            payload[f] = ""
            changed = True
    if changed:
        meta.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return changed


def _folder_for_mid(row: dict[str, Any], mod_root: Path) -> Path | None:
    lkp = text(row.get("last_known_path"))
    if lkp and Path(lkp).is_dir():
        return Path(lkp)
    return None


def regenerate_binding(
    *,
    mid: str,
    folder: Path | None,
) -> None:
    """Re-bind backup from cleaned .info for *mid* without creating entities."""
    if folder is None or not folder.is_dir():
        return
    try:
        from services.metadata_backup_sync import sync_after_metadata_change

        sync_after_metadata_change(mid, folder, reason="repair", wait=True)
    except Exception:
        pass


def apply_plan(
    plan: dict[str, Any],
    *,
    db_path: Path,
    mod_root: Path,
    data_root: Path,
    dry_run: bool = True,
) -> dict[str, Any]:
    rows = {text(r.get("mod_id")): r for r in load_db_rows(db_path)}
    results: list[dict[str, Any]] = []

    for item in plan.get("items") or []:
        decision = text(item.get("manual_decision")).upper()
        case = text(item.get("case"))
        mid = text(item.get("mod_id"))
        entry = {
            "mod_id": mid,
            "internal_id": text(item.get("internal_id")),
            "case": case,
            "decision": decision,
            "applied": False,
            "skipped_reason": "",
            "ops": [],
        }
        if case == CASE_C:
            entry["skipped_reason"] = "MANUAL_REVIEW"
            results.append(entry)
            continue
        if decision != DECISION_APPROVE:
            entry["skipped_reason"] = "not APPROVE"
            results.append(entry)
            continue
        if not mid or mid not in rows:
            entry["skipped_reason"] = "missing entity"
            results.append(entry)
            continue

        row = rows[mid]
        folder = _folder_for_mid(row, mod_root)
        for act in item.get("actions") or []:
            op = text(act.get("op"))
            fields = list(act.get("fields") or CLEAR_TEXT_FIELDS)
            targets = list(act.get("targets") or ["db", "info", "backup"])
            if op not in ("clear_foreign_text_fields", "clear_foreign_url_fields"):
                entry["ops"].append({"op": op, "status": "ignored"})
                continue
            if dry_run:
                entry["ops"].append(
                    {"op": op, "status": "dry_run", "fields": fields, "targets": targets}
                )
                continue
            if "db" in targets and op == "clear_foreign_text_fields":
                _clear_db_text(db_path, mid, fields)
                entry["ops"].append({"op": "clear_db", "fields": fields})
            if "info" in targets and folder is not None:
                if _clear_info_fields(folder, fields):
                    entry["ops"].append({"op": "clear_info", "fields": fields})
            if "backup" in targets:
                if _clear_backup_fields(data_root, mid, fields):
                    entry["ops"].append({"op": "clear_backup", "fields": fields})

        if not dry_run:
            regenerate_binding(mid=mid, folder=folder)
            entry["ops"].append({"op": "regenerate_backup_binding", "status": "ok"})
            entry["applied"] = True
        else:
            entry["applied"] = False
            entry["skipped_reason"] = "dry_run"

        results.append(entry)

    return {
        "generated_at": now_utc(),
        "dry_run": dry_run,
        "results": results,
        "applied_count": sum(1 for r in results if r.get("applied")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata ownership repair")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prev = sub.add_parser("preview", help="Build repair plan from audit")
    p_prev.add_argument("--db", type=Path, default=None)
    p_prev.add_argument("--mod-root", type=Path, default=None)
    p_prev.add_argument("--data-root", type=Path, default=None)
    p_prev.add_argument("--out", type=Path, default=None)

    p_apply = sub.add_parser("apply", help="Apply APPROVE items only")
    p_apply.add_argument("--plan", type=Path, required=True)
    p_apply.add_argument("--db", type=Path, default=None)
    p_apply.add_argument("--mod-root", type=Path, default=None)
    p_apply.add_argument("--data-root", type=Path, default=None)
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--apply", action="store_true")
    p_apply.add_argument("--confirm", action="store_true")
    p_apply.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)
    db = args.db or (ROOT / "data" / "mod_manager.db")
    mod_root = args.mod_root or (ROOT / "mod")
    data_root = args.data_root or (ROOT / "data")
    out_dir = ROOT / "tools" / "_audit_out"

    if args.cmd == "preview":
        plan = build_preview(db_path=db, mod_root=mod_root, data_root=data_root)
        out = args.out or (out_dir / "metadata_ownership_repair_plan.json")
        dump_json(out, plan)
        print(f"Wrote {out} items={plan['item_count']}")
        return 0

    if args.cmd == "apply":
        if not args.apply and not args.dry_run:
            print("Specify --dry-run or --apply --confirm", file=sys.stderr)
            return 2
        if args.apply and not args.confirm:
            print("--apply requires --confirm", file=sys.stderr)
            return 2
        plan = load_json(args.plan)
        result = apply_plan(
            plan,
            db_path=db,
            mod_root=mod_root,
            data_root=data_root,
            dry_run=not args.apply,
        )
        out = args.out or (out_dir / "metadata_ownership_repair_result.json")
        dump_json(out, result)
        print(f"Wrote {out} applied={result['applied_count']} dry_run={result['dry_run']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
