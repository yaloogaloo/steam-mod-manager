"""Identity Collision Recovery — Phase 2 preview (no mutation).

Builds a recovery plan from the audit report. Every item requires human
``manual_decision=APPROVE`` before apply. Never auto-guesses the true owner.

Usage::

    python tools/identity_collision_preview.py
    python tools/identity_collision_preview.py --report PATH --out PATH
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_collision_common import (  # noqa: E402
    CODE_EXTERNAL_COLLISION,
    CODE_INFO_DB_MISMATCH,
    CODE_INTERNAL_ID_COLLISION,
    CODE_WORKSPACE_COLLISION,
    default_paths,
    dump_json,
    load_json,
    nexus_mod_id_from_url,
    norm_path,
    now_utc,
    text,
)


def _proposed_split_for_internal_collision(finding: dict[str, Any]) -> dict[str, Any]:
    """Propose split options without auto-assigning keep ownership."""
    infos = list(finding.get("info_records") or [])
    db = dict(finding.get("db_record") or {})
    lkp = norm_path(db.get("last_known_path"))
    keep_candidate: dict[str, Any] | None = None
    split_candidates: list[dict[str, Any]] = []
    matches_lkp: list[dict[str, Any]] = []
    for info in infos:
        folder = text(info.get("folder"))
        if lkp and norm_path(folder) == lkp:
            matches_lkp.append(info)
    # Only propose keep when exactly one folder matches DB last_known_path.
    if len(matches_lkp) == 1:
        keep_candidate = {
            "folder": matches_lkp[0].get("folder"),
            "info_path": matches_lkp[0].get("info_path"),
            "title": matches_lkp[0].get("title"),
            "reason": "matches DB last_known_path (proposal only — requires confirm)",
            "evidence": matches_lkp[0],
        }
    for info in infos:
        folder = text(info.get("folder"))
        if keep_candidate and norm_path(folder) == norm_path(
            keep_candidate.get("folder")
        ):
            continue
        url = text(info.get("source_url"))
        split_candidates.append(
            {
                "folder": folder,
                "info_path": info.get("info_path"),
                "title": info.get("title"),
                "suggested_fields": {
                    "platform": text(info.get("platform")) or "nexus",
                    "app_id": int(info.get("app_id") or 0),
                    "external_id": text(info.get("external_id"))
                    or nexus_mod_id_from_url(url),
                    "workspace_id": text(info.get("workspace_id"))
                    or text(info.get("external_id"))
                    or nexus_mod_id_from_url(url),
                    "source_url": url,
                    "title": text(info.get("title")),
                },
                "note": (
                    "New internal_id will be allocated on APPROVE; "
                    "fields taken from .info evidence only"
                ),
            }
        )
    return {
        "strategy": "SPLIT_INTERNAL_ID",
        "keep_candidate": keep_candidate,
        "split_candidates": split_candidates,
        "human_must_set": {
            "keep_path": "absolute path of folder that keeps old_internal_id",
            "split_paths": "absolute paths that receive new internal_ids",
            "manual_decision": "APPROVE | REJECT",
        },
        "forbidden": [
            "auto_guess_owner",
            "auto_delete",
            "auto_merge",
            "folder_name_identity",
        ],
    }


def build_recovery_plan(report: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for finding in report.get("findings") or []:
        code = text(finding.get("code"))
        base = {
            "finding_code": code,
            "old_internal_id": text(finding.get("old_internal_id")),
            "affected_paths": list(finding.get("affected_paths") or []),
            "db_record": dict(finding.get("db_record") or {}),
            "info_records": list(finding.get("info_records") or []),
            "evidence": list(finding.get("evidence") or []),
            "risk": "HIGH",
            "requires_manual_confirm": True,
            "manual_decision": "",
            "keep_path": "",
            "split_paths": [],
            "proposed_split": None,
            "reason": text(finding.get("reason")),
        }

        if code == CODE_INTERNAL_ID_COLLISION:
            base["risk"] = "CRITICAL"
            base["proposed_split"] = _proposed_split_for_internal_collision(finding)
            # Prefill keep/split as empty suggestions only when unique LKP match.
            keep = (base["proposed_split"] or {}).get("keep_candidate") or {}
            if keep.get("folder"):
                base["keep_path_suggestion"] = text(keep.get("folder"))
            base["split_paths_suggestion"] = [
                text(c.get("folder"))
                for c in (base["proposed_split"] or {}).get("split_candidates") or []
            ]
            items.append(base)
            continue

        if code == CODE_INFO_DB_MISMATCH:
            base["risk"] = "HIGH"
            base["proposed_split"] = {
                "strategy": "REVIEW_MISMATCH",
                "note": (
                    "DB vs .info disagree. If this folder is a foreign merge victim, "
                    "promote to SPLIT via INTERNAL_ID_COLLISION / manual keep_path."
                ),
                "forbidden": ["auto_overwrite_db", "auto_guess"],
            }
            items.append(base)
            continue

        if code in (CODE_WORKSPACE_COLLISION, CODE_EXTERNAL_COLLISION):
            base["risk"] = "MEDIUM"
            base["proposed_split"] = {
                "strategy": "KEEP_SEPARATE_ENTITIES",
                "note": (
                    "Multiple DB entities already exist across games — "
                    "no merge/split of internal_id required. Display-only collision."
                ),
                "forbidden": ["auto_merge", "workspace_lookup"],
            }
            # Cross-game DB rows are already separate entities; no apply action.
            items.append(base)
            continue

        base["proposed_split"] = {
            "strategy": "MANUAL_REVIEW",
            "note": "Unclassified finding — human review only",
        }
        items.append(base)

    return {
        "phase": "collision_preview",
        "generated_at": now_utc(),
        "source_report": report.get("generated_at"),
        "db_path": report.get("db_path"),
        "library": report.get("library"),
        "production_mutation": "NONE",
        "instructions": [
            "Set manual_decision=APPROVE only after human review.",
            "For INTERNAL_ID_COLLISION: set keep_path + split_paths explicitly.",
            "Apply refuses empty / non-APPROVE decisions.",
            "Never auto-guess which Mod owns the old internal_id.",
        ],
        "counts": {
            "items": len(items),
            "by_code": {
                code: sum(1 for i in items if i.get("finding_code") == code)
                for code in (
                    CODE_INTERNAL_ID_COLLISION,
                    CODE_WORKSPACE_COLLISION,
                    CODE_EXTERNAL_COLLISION,
                    CODE_INFO_DB_MISMATCH,
                )
            },
            "approvable_splits": sum(
                1
                for i in items
                if i.get("finding_code") == CODE_INTERNAL_ID_COLLISION
            ),
        },
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    _db, _lib, out_dir = default_paths(ROOT)
    parser = argparse.ArgumentParser(description="Identity collision recovery preview")
    parser.add_argument(
        "--report",
        type=Path,
        default=out_dir / "identity_collision_report.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=out_dir / "identity_collision_recovery_plan.json",
    )
    args = parser.parse_args(argv)
    report = load_json(args.report)
    plan = build_recovery_plan(report)
    dump_json(args.out, plan)
    print(f"wrote {args.out}")
    print(f"items={plan['counts']['items']} by_code={plan['counts']['by_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
