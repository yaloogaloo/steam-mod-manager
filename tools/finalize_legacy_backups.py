#!/usr/bin/env python3
"""Finalize leftover workspace Backup buckets after current-Backup repair."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    from services.legacy_backup_finalize import finalize_leftover_legacy_buckets
    from services.legacy_workspace_backup import inventory_legacy_workspace_buckets

    DatabaseManager.instance(database_path())
    db = get_db()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now().replace(":", "")
    before = inventory_legacy_workspace_buckets(db=db)
    result = finalize_leftover_legacy_buckets(db=db, dry_run=dry_run)
    after = result["after"]
    payload = {
        "ts": _now(),
        "dry_run": dry_run,
        "before_inventory": {
            "legacy_bucket_count": before["legacy_bucket_count"],
            "legacy_bucket_total_bytes": before["legacy_bucket_total_bytes"],
        },
        "repair": {
            "repaired": result["repair"]["repaired_count"],
            "failed": result["repair"]["failed_count"],
        },
        "migrated": result["migrated"],
        "delete": {
            "deleted": result["delete"].get("deleted"),
            "bytes": result["delete"].get("bytes"),
            "errors": result["delete"].get("errors"),
        },
        "counts": result["counts"],
        "after_counts": after.get("counts"),
        "manual_decision": [
            {
                "legacy_bucket": row.get("legacy_bucket"),
                "size": row.get("size"),
                "final_why": row.get("final_why"),
                "historical_internal_id": row.get("historical_internal_id"),
                "workspace_id": row.get("workspace_id"),
                "title": row.get("title"),
            }
            for row in result["manual_decision"]
        ],
        "manifests": result["manifests"],
    }
    path = OUT_DIR / f"finalize_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== FINALIZE", "DRY RUN" if dry_run else "APPLY", "===")
    print("Before leftover:", before["legacy_bucket_count"], before["legacy_bucket_total_bytes"])
    print("Repaired:", result["repair"]["repaired_count"], "failed", result["repair"]["failed_count"])
    print("Migrated:", len(result["migrated"]))
    print("Deleted:", result["delete"].get("deleted"), result["delete"].get("bytes"))
    print("After leftover:", result["counts"]["after_legacy"])
    print("After invalid:", result["counts"]["after_invalid"])
    print("Manual decision:", len(result["manual_decision"]))
    print("Report:", path)
    return 0 if int(result["delete"].get("errors") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
