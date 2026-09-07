#!/usr/bin/env python3
"""Custom Deploy Path Stale Repair — Audit → Preview → APPROVE → Apply.

Default recommended action: CLEAR_TO_INHERIT (empty custom_deploy_path →
inherit game.mod_path). Optional REBIND_UNDER_INSTALL only when explicitly
APPROVE'd with a usable remapped candidate.

Never auto-applies. Never mutates identity fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db_manager import DatabaseManager  # noqa: E402
from services.custom_deploy_path_stale import (  # noqa: E402
    ACTION_CLEAR_TO_INHERIT,
    ACTION_REBIND_UNDER_INSTALL,
    DECISION_APPROVE,
    apply_clear_custom_deploy_path,
    apply_rebind_custom_deploy_path,
    audit_stale_custom_deploy_paths,
)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_preview(*, db_path: Path) -> dict[str, Any]:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    findings = audit_stale_custom_deploy_paths(db)
    items = []
    for f in findings:
        actions = [
            {
                "op": ACTION_CLEAR_TO_INHERIT,
                "description": (
                    "Set custom_deploy_path='' so deploy inherits game.mod_path"
                ),
            }
        ]
        if f.remapped_candidate and f.remapped_usable:
            actions.append(
                {
                    "op": ACTION_REBIND_UNDER_INSTALL,
                    "new_path": f.remapped_candidate,
                    "description": (
                        "Optional: rewrite onto current install root "
                        "(only when explicitly APPROVE'd)"
                    ),
                }
            )
        items.append(
            {
                **f.to_dict(),
                "manual_decision": "",
                "apply_action": f.recommended_action,
                "actions": actions,
            }
        )
    return {
        "generated_at": now_utc(),
        "db_path": str(db_path),
        "item_count": len(items),
        "items": items,
        "instructions": (
            "Set manual_decision=APPROVE and apply_action="
            f"{ACTION_CLEAR_TO_INHERIT}|{ACTION_REBIND_UNDER_INSTALL}. "
            f"Default recommendation is {ACTION_CLEAR_TO_INHERIT}."
        ),
    }


def apply_plan(
    plan: dict[str, Any],
    *,
    db_path: Path,
    dry_run: bool = True,
    only_mod_ids: set[str] | None = None,
) -> dict[str, Any]:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    results: list[dict[str, Any]] = []
    for item in plan.get("items") or []:
        mid = str(item.get("mod_id") or "").strip()
        if only_mod_ids is not None and mid not in only_mod_ids:
            results.append(
                {
                    "mod_id": mid,
                    "applied": False,
                    "skipped_reason": "filtered",
                }
            )
            continue
        decision = str(item.get("manual_decision") or "").strip().upper()
        action = str(item.get("apply_action") or item.get("recommended_action") or "")
        entry: dict[str, Any] = {
            "mod_id": mid,
            "internal_id": item.get("internal_id"),
            "decision": decision,
            "apply_action": action,
            "applied": False,
            "before": item.get("custom_deploy_path"),
            "after": None,
        }
        if decision != DECISION_APPROVE:
            entry["skipped_reason"] = "not APPROVE"
            results.append(entry)
            continue
        if dry_run:
            entry["skipped_reason"] = "dry_run"
            if action == ACTION_CLEAR_TO_INHERIT:
                entry["after"] = ""
            elif action == ACTION_REBIND_UNDER_INSTALL:
                entry["after"] = item.get("remapped_candidate") or ""
            results.append(entry)
            continue
        if action == ACTION_CLEAR_TO_INHERIT:
            ok = apply_clear_custom_deploy_path(db, mid)
            entry["applied"] = ok
            entry["after"] = "" if ok else None
            if not ok:
                entry["skipped_reason"] = "clear_failed"
        elif action == ACTION_REBIND_UNDER_INSTALL:
            new_path = str(item.get("remapped_candidate") or "").strip()
            ok = apply_rebind_custom_deploy_path(db, mid, new_path)
            entry["applied"] = ok
            entry["after"] = new_path if ok else None
            if not ok:
                entry["skipped_reason"] = "rebind_failed_or_unusable"
        else:
            entry["skipped_reason"] = f"unknown_action:{action}"
        results.append(entry)
    return {
        "generated_at": now_utc(),
        "dry_run": dry_run,
        "applied_count": sum(1 for r in results if r.get("applied")),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair stale custom_deploy_path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prev = sub.add_parser("preview", help="Build repair plan from audit")
    p_prev.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    p_prev.add_argument(
        "--out",
        type=Path,
        default=ROOT
        / "tools"
        / "_audit_out"
        / "custom_deploy_path_repair_plan.json",
    )

    p_apply = sub.add_parser("apply", help="Apply APPROVE items")
    p_apply.add_argument("--plan", type=Path, required=True)
    p_apply.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--apply", action="store_true")
    p_apply.add_argument("--confirm", action="store_true")
    p_apply.add_argument(
        "--only-mod-id",
        action="append",
        default=[],
        help="Limit apply to these internal mod_id PK values (repeatable)",
    )
    p_apply.add_argument(
        "--out",
        type=Path,
        default=ROOT
        / "tools"
        / "_audit_out"
        / "custom_deploy_path_repair_result.json",
    )

    p_clear = sub.add_parser(
        "clear-one",
        help="APPROVE clear one mod_id (confirmed data repair)",
    )
    p_clear.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    p_clear.add_argument("--mod-id", required=True)
    p_clear.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "preview":
        plan = build_preview(db_path=args.db)
        dump_json(args.out, plan)
        print(f"Wrote {args.out} items={plan['item_count']}")
        return 0

    if args.cmd == "apply":
        if not args.apply and not args.dry_run:
            print("Specify --dry-run or --apply --confirm", file=sys.stderr)
            return 2
        if args.apply and not args.confirm:
            print("--apply requires --confirm", file=sys.stderr)
            return 2
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        only = {str(x) for x in (args.only_mod_id or [])} or None
        result = apply_plan(
            plan,
            db_path=args.db,
            dry_run=not args.apply,
            only_mod_ids=only,
        )
        dump_json(args.out, result)
        print(
            f"Wrote {args.out} applied={result['applied_count']} "
            f"dry_run={result['dry_run']}"
        )
        return 0

    if args.cmd == "clear-one":
        if not args.confirm:
            print("clear-one requires --confirm", file=sys.stderr)
            return 2
        DatabaseManager.reset_instance()
        db = DatabaseManager.instance(args.db)
        mid = str(args.mod_id).strip()
        before = ""
        info = db.get_mod_display_info(mid)
        if info is not None:
            before = str(info.custom_deploy_path or "")
        ok = apply_clear_custom_deploy_path(db, mid)
        after = ""
        info2 = db.get_mod_display_info(mid)
        if info2 is not None:
            after = str(info2.custom_deploy_path or "")
        print(f"clear-one mod_id={mid} ok={ok} before={before!r} after={after!r}")
        return 0 if ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
