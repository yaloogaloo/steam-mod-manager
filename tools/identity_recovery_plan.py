"""Identity Recovery Phase 2-A — generate repair PLAN from Phase 1 report.

Read-only. Never mutates DB / filesystem / lifecycle code.

Rules
-----
- Never create / merge / delete Mods
- Never migrate internal_id
- Never call IdentityService / workspace reverse lookup
- AUTO_SAFE only for ``info_internal_id_not_in_db`` when DB/deploy/backup
  have no references; otherwise escalate to MANUAL_REVIEW

Usage::

    python tools/identity_recovery_plan.py
    python tools/identity_recovery_plan.py --report path/to/report.json --out plan.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CATEGORY_AUTO_SAFE = "AUTO_SAFE"
CATEGORY_MANUAL_REVIEW = "MANUAL_REVIEW"
CATEGORY_FORBIDDEN_AUTO_FIX = "FORBIDDEN_AUTO_FIX"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"

CODE_INFO_NOT_IN_DB = "info_internal_id_not_in_db"
CODE_DB_WITHOUT_INFO = "db_entity_without_valid_info"
CODE_BACKUP_ANOMALY = "backup_identity_source_anomaly"
CODE_CROSS_EXT = "cross_game_same_external_id"
CODE_CROSS_WS = "cross_game_same_workspace_id"
CODE_DUP_INTERNAL = "duplicate_internal_id"
CODE_INFO_MISMATCH = "db_info_identity_mismatch"
CODE_IDENTITY_COLLISION = "identity_collision"
CODE_EXTERNAL_CONFLICT = "external_id_conflict"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _collect_db_mod_ids(con: sqlite3.Connection) -> set[str]:
    if not _table_exists(con, "mods"):
        return set()
    ids: set[str] = set()
    for row in con.execute(
        "SELECT mod_id, TRIM(COALESCE(internal_id,'')) AS iid FROM mods"
    ):
        mid = _text(row[0])
        iid = _text(row[1])
        if mid:
            ids.add(mid)
        if iid:
            ids.add(iid)
    return ids


def _collect_deploy_mod_ids(con: sqlite3.Connection) -> set[str]:
    """Read-only scan of deployment tables for mod_id references."""
    refs: set[str] = set()
    # deployment_record_items typically stores mod_id
    if _table_exists(con, "deployment_record_items"):
        cols = {r[1] for r in con.execute("PRAGMA table_info(deployment_record_items)")}
        if "mod_id" in cols:
            for (mid,) in con.execute(
                "SELECT DISTINCT TRIM(CAST(mod_id AS TEXT)) FROM deployment_record_items"
            ):
                if _text(mid):
                    refs.add(_text(mid))
    # Some schemas keep JSON blobs on deployment_records — inspect columns carefully
    if _table_exists(con, "deployment_records"):
        cols = {r[1] for r in con.execute("PRAGMA table_info(deployment_records)")}
        for col in ("mod_id", "primary_mod_id"):
            if col in cols:
                for (mid,) in con.execute(
                    f"SELECT DISTINCT TRIM(CAST({col} AS TEXT)) FROM deployment_records"
                ):
                    if _text(mid):
                        refs.add(_text(mid))
    return refs


def _backup_dirs(backup_root: Path) -> set[str]:
    if not backup_root.is_dir():
        return set()
    return {p.name for p in backup_root.iterdir() if p.is_dir()}


def _backup_payload_internal_ids(backup_root: Path) -> set[str]:
    """internal_id values appearing inside backup metadata.json (dependency signal)."""
    found: set[str] = set()
    if not backup_root.is_dir():
        return found
    for child in backup_root.iterdir():
        if not child.is_dir():
            continue
        meta = child / "metadata.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            iid = _text(data.get("internal_id"))
            if iid:
                found.add(iid)
    return found


def _classify_finding(
    finding: dict[str, Any],
    *,
    db_ids: set[str],
    deploy_ids: set[str],
    backup_dir_names: set[str],
    backup_payload_ids: set[str],
) -> dict[str, Any]:
    code = _text(finding.get("code"))
    internal_id = _text(finding.get("internal_id"))
    db_record = finding.get("db_record") or {}
    mod_id = _text(db_record.get("mod_id")) if isinstance(db_record, dict) else ""
    evidence: dict[str, Any] = {
        "phase1_code": code,
        "conflict_reason": _text(finding.get("conflict_reason")),
        "info_path": _text(finding.get("info_path")),
        "extra": finding.get("extra") or {},
    }

    # --- FORBIDDEN_AUTO_FIX ---
    if code in {
        CODE_DUP_INTERNAL,
        CODE_IDENTITY_COLLISION,
        CODE_EXTERNAL_CONFLICT,
        CODE_INFO_MISMATCH,
    }:
        return {
            "internal_id": internal_id,
            "category": CATEGORY_FORBIDDEN_AUTO_FIX,
            "risk": RISK_CRITICAL if code == CODE_DUP_INTERNAL else RISK_HIGH,
            "current_data": {
                "finding_type": code,
                "app_id": finding.get("app_id"),
                "platform": finding.get("platform"),
                "external_id": finding.get("external_id"),
                "db_record": db_record,
                "phase1_recommended_action": finding.get("recommended_action"),
            },
            "evidence": evidence,
            "recommended_action": (
                "FORBIDDEN_AUTO — human forensic plan only; "
                "do not auto-merge / migrate internal_id"
            ),
            "auto_fix_allowed": False,
            "needs_human_confirm_reason": (
                f"{code} can destroy distinct entities if automated"
            ),
        }

    # --- AUTO_SAFE candidate: orphan .info only ---
    if code == CODE_INFO_NOT_IN_DB:
        refs = {
            "db_has_internal_or_mod_id": internal_id in db_ids,
            "deploy_references_id": internal_id in deploy_ids
            or (mod_id and mod_id in deploy_ids),
            "backup_dir_named_as_id": internal_id in backup_dir_names,
            "backup_payload_contains_id": internal_id in backup_payload_ids,
        }
        evidence["reference_checks"] = refs
        clean = not any(refs.values())
        if clean:
            return {
                "internal_id": internal_id,
                "category": CATEGORY_AUTO_SAFE,
                "risk": RISK_LOW,
                "current_data": {
                    "finding_type": code,
                    "app_id": finding.get("app_id"),
                    "platform": finding.get("platform"),
                    "external_id": finding.get("external_id"),
                    "db_record": db_record,
                    "info_path": finding.get("info_path"),
                    "folder": (finding.get("extra") or {}).get("folder"),
                    "phase1_recommended_action": finding.get("recommended_action"),
                },
                "evidence": evidence,
                "recommended_action": (
                    "IGNORE_ORPHAN_INFO — leave folder unregistered; "
                    "optional quarantine of forged .info after confirm"
                ),
                "auto_fix_allowed": True,
                "needs_human_confirm_reason": (
                    "AUTO_SAFE for ignore-only; still confirm before any "
                    "filesystem quarantine in a later phase"
                ),
            }
        return {
            "internal_id": internal_id,
            "category": CATEGORY_MANUAL_REVIEW,
            "risk": RISK_MEDIUM,
            "current_data": {
                "finding_type": code,
                "app_id": finding.get("app_id"),
                "platform": finding.get("platform"),
                "external_id": finding.get("external_id"),
                "db_record": db_record,
                "info_path": finding.get("info_path"),
                "folder": (finding.get("extra") or {}).get("folder"),
                "phase1_recommended_action": finding.get("recommended_action"),
            },
            "evidence": evidence,
            "recommended_action": (
                "MANUAL_REVIEW — orphan .info has DB/deploy/backup references; "
                "do not auto-ignore or create entity"
            ),
            "auto_fix_allowed": False,
            "needs_human_confirm_reason": (
                "info_internal_id_not_in_db but references exist: "
                + ", ".join(k for k, v in refs.items() if v)
            ),
        }

    # --- MANUAL_REVIEW defaults ---
    if code in {
        CODE_DB_WITHOUT_INFO,
        CODE_BACKUP_ANOMALY,
        CODE_CROSS_EXT,
        CODE_CROSS_WS,
    }:
        risk = RISK_HIGH if code in {CODE_CROSS_EXT, CODE_CROSS_WS} else RISK_MEDIUM
        action_map = {
            CODE_DB_WITHOUT_INFO: (
                "RESTORE_INFO_FROM_BACKUP_AFTER_CONFIRM — only if backup "
                "identity fully matches existing DB entity"
            ),
            CODE_BACKUP_ANOMALY: (
                "QUARANTINE_BACKUP_AFTER_CONFIRM — never create Mod from backup"
            ),
            CODE_CROSS_EXT: (
                "SPLIT_CROSS_GAME_ENTITIES_AFTER_CONFIRM — keep both entities; "
                "fix app_id scoping manually"
            ),
            CODE_CROSS_WS: (
                "REVIEW_MANUAL — workspace_id display collision across games; "
                "not an entity key"
            ),
        }
        return {
            "internal_id": internal_id,
            "category": CATEGORY_MANUAL_REVIEW,
            "risk": risk,
            "current_data": {
                "finding_type": code,
                "app_id": finding.get("app_id"),
                "platform": finding.get("platform"),
                "external_id": finding.get("external_id"),
                "db_record": db_record,
                "info_path": finding.get("info_path"),
                "phase1_recommended_action": finding.get("recommended_action"),
                "extra": finding.get("extra") or {},
            },
            "evidence": evidence,
            "recommended_action": action_map.get(code, "REVIEW_MANUAL"),
            "auto_fix_allowed": False,
            "needs_human_confirm_reason": (
                f"{code} requires human judgment; auto-fix forbidden by Phase 2-A policy"
            ),
        }

    # Unknown / future codes → forbid auto
    return {
        "internal_id": internal_id,
        "category": CATEGORY_FORBIDDEN_AUTO_FIX,
        "risk": RISK_HIGH,
        "current_data": {
            "finding_type": code or "unknown",
            "app_id": finding.get("app_id"),
            "platform": finding.get("platform"),
            "external_id": finding.get("external_id"),
            "db_record": db_record,
            "raw_finding": finding,
        },
        "evidence": evidence,
        "recommended_action": "FORBIDDEN_AUTO — unknown finding type",
        "auto_fix_allowed": False,
        "needs_human_confirm_reason": f"unclassified finding code={code!r}",
    }


def build_recovery_plan(
    report: dict[str, Any],
    *,
    db_path: Path,
    backup_root: Path,
) -> dict[str, Any]:
    findings = list(report.get("findings") or [])
    con = sqlite3.connect(str(db_path))
    try:
        db_ids = _collect_db_mod_ids(con)
        deploy_ids = _collect_deploy_mod_ids(con)
    finally:
        con.close()
    backup_dirs = _backup_dirs(backup_root)
    backup_payload_ids = _backup_payload_internal_ids(backup_root)

    items: list[dict[str, Any]] = []
    for idx, finding in enumerate(findings):
        item = _classify_finding(
            finding,
            db_ids=db_ids,
            deploy_ids=deploy_ids,
            backup_dir_names=backup_dirs,
            backup_payload_ids=backup_payload_ids,
        )
        item["plan_index"] = idx
        item["finding_type"] = _text(finding.get("code"))
        item["current_state"] = _text(finding.get("conflict_reason"))
        items.append(item)

    by_cat = Counter(i["category"] for i in items)
    by_type = Counter(i["finding_type"] for i in items)
    auto_allowed = sum(1 for i in items if i.get("auto_fix_allowed"))

    return {
        "generated_at": _now(),
        "phase": "2-A",
        "mode": "plan_only",
        "production_mutation": "NONE",
        "source_report": {
            "generated_at": report.get("generated_at"),
            "findings_count": report.get("findings_count"),
            "db_path": report.get("db_path"),
            "library_path": report.get("library_path"),
            "backup_path": report.get("backup_path"),
        },
        "db_path": str(db_path),
        "backup_path": str(backup_root),
        "findings_planned": len(items),
        "category_counts": dict(sorted(by_cat.items())),
        "finding_type_counts": dict(sorted(by_type.items())),
        "auto_fix_allowed_count": auto_allowed,
        "policy": {
            "AUTO_SAFE": (
                "Only info_internal_id_not_in_db with no DB/deploy/backup references; "
                "action is ignore-orphan (no create)"
            ),
            "MANUAL_REVIEW": (
                "db_entity_without_valid_info, backup_identity_source_anomaly, "
                "cross_game_same_external_id, cross_game_same_workspace_id, "
                "and orphan-info with references"
            ),
            "FORBIDDEN_AUTO_FIX": (
                "duplicate_internal_id, identity_collision, external_id conflict, "
                "db_info_identity_mismatch"
            ),
            "never": [
                "modify database",
                "delete data",
                "create Mod",
                "merge Mod",
                "migrate internal_id",
                "call IdentityService",
                "workspace reverse lookup",
            ],
        },
        "guards": {
            "modifies_database": False,
            "creates_mods": False,
            "merges_entities": False,
            "calls_identity_service": False,
            "calls_workspace_resolver": False,
            "changes_lifecycle_code": False,
            "auto_executes_repairs": False,
        },
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=str,
        default="",
        help="Phase 1 report JSON (default: tools/_audit_out/identity_recovery_report.json)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="",
        help="SQLite path for reference checks (default: report.db_path)",
    )
    parser.add_argument(
        "--backup",
        type=str,
        default="",
        help="mod_backup root (default: report.backup_path)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output plan JSON (default: tools/_audit_out/identity_recovery_plan.json)",
    )
    args = parser.parse_args(argv)

    report_path = (
        Path(args.report)
        if args.report
        else (ROOT / "tools" / "_audit_out" / "identity_recovery_report.json")
    )
    if not report_path.is_file():
        print(f"missing report: {report_path}", file=sys.stderr)
        return 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    db_path = Path(args.db) if args.db else Path(_text(report.get("db_path")))
    backup_root = (
        Path(args.backup) if args.backup else Path(_text(report.get("backup_path")))
    )
    out = (
        Path(args.out)
        if args.out
        else (ROOT / "tools" / "_audit_out" / "identity_recovery_plan.json")
    )

    # Snapshot DB bytes before/after to prove no mutation.
    before = db_path.read_bytes() if db_path.is_file() else b""
    plan = build_recovery_plan(report, db_path=db_path, backup_root=backup_root)
    after = db_path.read_bytes() if db_path.is_file() else b""
    if before != after:
        raise RuntimeError("Phase 2-A plan builder mutated the database — abort")

    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    out.write_text(payload, encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dated = out.with_name(f"identity_recovery_plan_{stamp}.json")
    dated.write_text(payload, encoding="utf-8")

    print(f"plan={out}")
    print(f"dated={dated}")
    print(f"findings_planned={plan['findings_planned']}")
    print(f"category_counts={json.dumps(plan['category_counts'], ensure_ascii=False)}")
    print(f"auto_fix_allowed_count={plan['auto_fix_allowed_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
