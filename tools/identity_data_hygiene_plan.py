"""Identity Data Hygiene — Phase 2 manual repair plan (never auto-executes).

Reads the Phase 1 audit report and emits candidate actions with
``requires_manual_confirm=true``.

Forbidden::

    - create / delete / merge Mods
    - modify internal_id / app_id
    - apply any UPDATE without human approval

Usage::

    python tools/identity_data_hygiene_plan.py
    python tools/identity_data_hygiene_plan.py --audit tools/_audit_out/identity_data_hygiene_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_AUDIT = ROOT / "tools" / "_audit_out" / "identity_data_hygiene_report.json"
DEFAULT_OUT = ROOT / "tools" / "_audit_out" / "identity_data_hygiene_plan.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _url_suggests_workspace(source_url: str, workspace_id: str, external_id: str) -> str:
    """Return which id (if any) appears verifiably in source_url path."""
    url = _text(source_url).lower()
    if not url:
        return ""
    try:
        path = urlparse(url).path or ""
    except Exception:  # noqa: BLE001
        path = url
    ws = _text(workspace_id)
    ext = _text(external_id)
    if ws and (f"/{ws}" in path or f"id={ws}" in url or path.endswith(ws)):
        return "workspace_id"
    if ext and (f"/{ext}" in path or f"id={ext}" in url or path.endswith(ext)):
        return "external_id"
    return ""


def _plan_item(
    *,
    case_id: str,
    before: dict[str, Any],
    candidate_action: str,
    risk: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "before": before,
        "candidate_action": candidate_action,
        "risk": risk,
        "rationale": rationale,
        "requires_manual_confirm": True,
        "forbidden": [
            "modify_internal_id",
            "modify_app_id",
            "merge_mods",
            "create_from_external_id",
            "delete_by_workspace_id",
            "auto_move_by_app_id",
        ],
        "allowed_field_if_approved": "workspace_id",
    }


def build_plan(audit: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    mismatch = audit.get("workspace_external_mismatch") or {}
    for row in mismatch.get("A_empty_workspace_has_external") or []:
        ext = _text(row.get("external_id"))
        url_hit = _url_suggests_workspace(
            str(row.get("source_url") or ""), "", ext
        )
        action = (
            f"SET workspace_id={ext!r} FROM external_id (empty workspace backfill)"
            if ext
            else "NO_SAFE_ACTION"
        )
        risk = "LOW" if url_hit == "external_id" or not _text(row.get("source_url")) else "MEDIUM"
        items.append(
            _plan_item(
                case_id=f"A-{row.get('mod_id')}",
                before=dict(row),
                candidate_action=action,
                risk=risk,
                rationale=(
                    "workspace_id empty; external_id present (legacy). "
                    "Safe only if platform digits match intended registration number."
                ),
            )
        )

    for row in mismatch.get("B_workspace_ne_external") or []:
        ws = _text(row.get("workspace_id"))
        ext = _text(row.get("external_id"))
        url_hit = _url_suggests_workspace(
            str(row.get("source_url") or ""), ws, ext
        )
        if url_hit == "external_id" and ext:
            action = (
                f"SET workspace_id={ext!r} TO MATCH verified source_url / external_id "
                f"(current workspace_id={ws!r} may be historical uniquify pollution)"
            )
            risk = "MEDIUM"
        elif url_hit == "workspace_id" and ws:
            action = (
                f"KEEP workspace_id={ws!r}; leave external_id={ext!r} as legacy audit only"
            )
            risk = "LOW"
        else:
            action = (
                "MANUAL_REVIEW — cannot decide between workspace_id and external_id "
                "without source_url proof"
            )
            risk = "HIGH"
        items.append(
            _plan_item(
                case_id=f"B-{row.get('mod_id')}",
                before=dict(row),
                candidate_action=action,
                risk=risk,
                rationale=(
                    "workspace_id != external_id. Only workspace_id may change after "
                    "manual confirm; never rewrite internal_id/app_id."
                ),
            )
        )

    for row in mismatch.get("C_empty_external") or []:
        items.append(
            _plan_item(
                case_id=f"C-{row.get('mod_id')}",
                before=dict(row),
                candidate_action="NO_ACTION — empty external_id is expected under minimal model",
                risk="NONE",
                rationale="external_id is legacy audit-only; empty is fine when workspace_id set.",
            )
        )

    cross = audit.get("cross_game_workspace_report") or {}
    for row in cross.get("cross_game_allowed") or []:
        items.append(
            _plan_item(
                case_id=f"XOK-{row.get('platform')}-{row.get('workspace_id')}",
                before=dict(row),
                candidate_action="NO_ACTION — cross-game duplicate workspace_id is ALLOWED",
                risk="NONE",
                rationale="Registration scope is (platform, app_id, workspace_id).",
            )
        )
    for row in cross.get("same_app_conflict") or []:
        items.append(
            _plan_item(
                case_id=f"XBAD-{row.get('platform')}-{row.get('app_id')}-{row.get('workspace_id')}",
                before=dict(row),
                candidate_action=(
                    "MANUAL_REVIEW — same (platform, app_id, workspace_id) maps to "
                    "multiple entities; do NOT auto-merge"
                ),
                risk="CRITICAL",
                rationale="Same-app registration collision. Human must decide which entity survives.",
            )
        )

    integrity = audit.get("internal_id_integrity") or {}
    for row in integrity.get("db_internal_id_empty") or []:
        items.append(
            _plan_item(
                case_id=f"IID-EMPTY-{row.get('mod_id')}",
                before=dict(row),
                candidate_action="MANUAL_REVIEW — DB internal_id empty; do NOT invent UUID here",
                risk="CRITICAL",
                rationale="Hygiene phase must not mint identity. Escalate to lifecycle owners.",
            )
        )
    for row in integrity.get("db_present_info_missing") or []:
        items.append(
            _plan_item(
                case_id=f"INFO-MISS-{row.get('mod_id')}",
                before=dict(row),
                candidate_action=(
                    "MANUAL_REVIEW — restore .info from backup only if "
                    "backup.internal_id == DB.internal_id"
                ),
                risk="HIGH",
                rationale="Do not rewrite internal_id. Backup restore is bind-only.",
            )
        )
    for row in integrity.get("info_present_db_missing") or []:
        items.append(
            _plan_item(
                case_id=f"INFO-ORPHAN-{_text(row.get('info_internal_id'))[:12]}",
                before=dict(row),
                candidate_action="NO_CREATE — .info without DB entity; leave for Sync/Import only",
                risk="HIGH",
                rationale="Hygiene must never create Mods from .info.",
            )
        )
    for row in integrity.get("duplicate_internal_id") or []:
        items.append(
            _plan_item(
                case_id=f"IID-DUP-{_text(row.get('internal_id'))[:12]}",
                before=dict(row),
                candidate_action="MANUAL_REVIEW — duplicate internal_id; do NOT auto-merge",
                risk="CRITICAL",
                rationale="Entity uniqueness broken. Architecture escalation required.",
            )
        )

    by_risk: dict[str, int] = {}
    for item in items:
        r = str(item.get("risk") or "UNKNOWN")
        by_risk[r] = by_risk.get(r, 0) + 1

    return {
        "generated_at": _now(),
        "phase": "identity_data_hygiene_2",
        "mode": "plan_only",
        "production_mutation": "NONE",
        "auto_execute": False,
        "requires_manual_confirm_all": True,
        "allowed_future_mutation": {
            "field": "workspace_id",
            "conditions": [
                "internal_id unchanged",
                "app_id unchanged",
                ".info/entity_key matches DB Entity.internal_id",
                "source_url verifiable when deciding fill/overwrite",
            ],
        },
        "forbidden": [
            "modify_internal_id",
            "merge_two_mods",
            "create_entity_from_external_id",
            "delete_entity_by_workspace_id",
            "move_entity_by_app_id",
            "auto_apply",
        ],
        "source_audit": str(audit.get("db_path") or ""),
        "counts": {"items": len(items), "by_risk": by_risk},
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identity hygiene manual plan (no apply)")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if not args.audit.is_file():
        print(
            json.dumps(
                {
                    "error": "audit_missing",
                    "hint": "Run python tools/identity_data_hygiene_audit.py first",
                    "expected": str(args.audit),
                },
                indent=2,
            )
        )
        return 1
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    plan = build_plan(audit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "items": plan["counts"]["items"],
                "by_risk": plan["counts"]["by_risk"],
                "auto_execute": False,
                "production_mutation": "NONE",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
