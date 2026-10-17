#!/usr/bin/env python3
"""Inventory / dry-run / delete legacy ``data/mod_backup/<workspace_id>/`` buckets.

Current Backup storage key is ``mods.mod_id``. This tool never deletes a
directory whose name is a current ``mods.mod_id``.

Usage::

    python tools/archive/cleanup_legacy_workspace_backup.py --dry-run
    python tools/archive/cleanup_legacy_workspace_backup.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "_tmp" / "dumps" / "legacy_workspace_backup"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()
    dry_run = not args.apply
    if args.dry_run:
        dry_run = True

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from core.db_manager import DatabaseManager, get_db
    from core.paths import database_path
    from tools.archive.legacy_workspace_backup import (
        classify_legacy_workspace_buckets,
        delete_safe_legacy_workspace_buckets,
        inventory_legacy_workspace_buckets,
        repair_invalid_current_from_legacy,
        summarize_report,
    )

    DatabaseManager.instance(database_path())
    db = get_db()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    before_inv = inventory_legacy_workspace_buckets(db=db)
    classified = classify_legacy_workspace_buckets(db=db)
    stamp = _now().replace(":", "")
    before_path = OUT_DIR / f"dry_run_{stamp}.json"
    before_path.write_text(
        json.dumps(
            {
                "ts": _now(),
                "inventory": {
                    "legacy_bucket_count": before_inv["legacy_bucket_count"],
                    "legacy_bucket_total_bytes": before_inv["legacy_bucket_total_bytes"],
                    "current_entity_count": before_inv["current_entity_count"],
                },
                "counts": classified["counts"],
                "writer": classified["writer"],
                "safe_deletion_examples": classified["safe_deletion_examples"],
                "retained_by_category": classified["retained_by_category"],
                "no_current_entity_examples": [
                    {
                        "legacy_bucket": row["legacy_bucket"],
                        "category": row.get("category"),
                        "why": row.get("why"),
                    }
                    for row in (classified.get("no_current_entity") or [])[:30]
                ],
                "unique_data_examples": [
                    {
                        "legacy_bucket": row["legacy_bucket"],
                        "current_mod_id": row.get("current_mod_id"),
                        "why": row.get("why"),
                    }
                    for row in (classified.get("unique_data") or [])[:30]
                ],
                "current_backup_invalid_examples": [
                    {
                        "legacy_bucket": row["legacy_bucket"],
                        "current_mod_id": row.get("current_mod_id"),
                        "issues": row.get("issues"),
                    }
                    for row in (classified.get("current_backup_invalid") or [])[:30]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("=== DRY RUN ===" if dry_run else "=== APPLY ===")
    print(f"Total legacy buckets: {classified['counts']['total_legacy_buckets']}")
    print(f"Safe to delete: {classified['counts']['safe_to_delete']}")
    print(f"Blocked: {classified['counts']['blocked']}")
    print(f"Current backup missing: {classified['counts']['current_backup_missing']}")
    print(f"Current backup invalid: {classified['counts']['current_backup_invalid']}")
    print(f"Multiple Entity candidates: {classified['counts']['multiple_entity_candidates']}")
    print(f"Unique data: {classified['counts']['unique_data']}")
    print(f"Runtime reference risk: {classified['counts']['runtime_reference_risk']}")
    print(f"No current Entity: {classified['counts']['no_current_entity']}")
    print(f"Recovery valuable: {classified['counts'].get('recovery_valuable', 0)}")
    print(f"Manual decision: {classified['counts'].get('manual_decision', 0)}")
    print(f"Bytes reclaimable: {classified['counts']['bytes_reclaimable']}")
    print("Safe deletion examples:")
    for row in classified["safe_deletion_examples"]:
        print(f"  {row['legacy_bucket']}")
        print(f"  → current mod_id={row['current_mod_id']}")
        print(f"  → {row['why']}")
    print(f"Report: {before_path}")

    audit_path = OUT_DIR / f"delete_audit_{stamp}.jsonl"
    repair = repair_invalid_current_from_legacy(classified, dry_run=dry_run)
    repair_path = OUT_DIR / f"repair_{stamp}.json"
    repair_path.write_text(
        json.dumps(repair, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Repaired current backups: {repair['repaired_count']} "
        f"failed={repair['failed_count']} dry_run={dry_run}"
    )
    if not dry_run and int(repair.get("repaired_count") or 0) > 0:
        classified = classify_legacy_workspace_buckets(db=db)
        print(
            "Reclassified after repair: "
            f"invalid={classified['counts']['current_backup_invalid']} "
            f"safe={classified['counts']['safe_to_delete']}"
        )
    result = delete_safe_legacy_workspace_buckets(
        classified,
        dry_run=dry_run,
        audit_path=audit_path,
    )
    after = classify_legacy_workspace_buckets(db=db)
    summary = summarize_report(before=classified, delete_result=result, after=after)
    summary_path = OUT_DIR / f"summary_{stamp}.json"
    summary_path.write_text(
        json.dumps(
            {
                "ts": _now(),
                "dry_run": dry_run,
                "entity_count_before": classified["entity_count"],
                "entity_count_after": after["entity_count"],
                "identity_pairs_unchanged": classified["entity_identity_pairs"]
                == after["entity_identity_pairs"],
                "writer": after["writer"],
                "repair": {
                    "repaired": repair["repaired_count"],
                    "failed": repair["failed_count"],
                    "dry_run": repair["dry_run"],
                },
                "delete": {
                    "deleted": result["deleted"],
                    "bytes": result["bytes"],
                    "errors": result["errors"],
                    "refused": result.get("refused"),
                },
                "summary": summary,
                "after_counts": after["counts"],
                "retained_by_category": after["retained_by_category"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("=== AFTER ===")
    print(f"Deleted: {result['deleted']} / {result['bytes']} bytes (dry_run={dry_run})")
    print(f"After legacy buckets: {after['counts']['total_legacy_buckets']}")
    print(f"After safe remaining: {after['counts']['safe_to_delete']}")
    print(f"Summary: {summary_path}")
    return 0 if int(result.get("errors") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
