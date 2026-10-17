"""Phase 3 — read-only migration report for retiring legacy identity fields.

Does not DROP columns. Does not mutate production data by default.

Usage::

    python tools/identity_cleanup_migration_report.py
    python tools/identity_cleanup_migration_report.py --apply-workspace-backfill
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "tools" / "_audit_out" / "identity_cleanup_migration_report.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        empty_ws = con.execute(
            """
            SELECT COUNT(*) AS c FROM mods
            WHERE TRIM(COALESCE(workspace_id,'')) = ''
              AND TRIM(COALESCE(external_id,'')) != ''
            """
        ).fetchone()["c"]
        mismatch = con.execute(
            """
            SELECT COUNT(*) AS c FROM mods
            WHERE TRIM(COALESCE(workspace_id,'')) != ''
              AND TRIM(COALESCE(external_id,'')) != ''
              AND TRIM(workspace_id) != TRIM(external_id)
            """
        ).fetchone()["c"]
        drop_ready = empty_ws == 0
        return {
            "generated_at": _now(),
            "phase": "identity_convergence_3",
            "db_path": str(db_path),
            "production_mutation": "NONE",
            "legacy_fields": {
                "external_id": {
                    "status": "RETIRE_AS_IDENTITY",
                    "empty_workspace_with_external": empty_ws,
                    "workspace_external_mismatch": mismatch,
                    "drop_column_ready": drop_ready,
                    "recommended_backfill_sql": (
                        "UPDATE mods SET workspace_id = TRIM(external_id) "
                        "WHERE TRIM(COALESCE(workspace_id,'')) = '' "
                        "AND TRIM(COALESCE(external_id,'')) != '';"
                    ),
                },
                "published_file_id": {
                    "status": "STOP_WRITING_TO_INFO",
                    "drop_column_ready": False,
                    "notes": "Not a mods column in all schemas; strip from .info writers",
                },
                "workshop_id": {
                    "status": "TEMP_PARSE_ONLY",
                    "drop_column_ready": True,
                    "notes": "Import/Sync kwargs only — not a DB identity column",
                },
            },
            "keep": ["internal_id", "workspace_id", "platform", "app_id"],
            "registration_api": "find_mod_for_registration(platform, app_id, workspace_id)",
            "forbidden_after_convergence": [
                "find_mod_by_external as separate axis",
                "Reconcile workspace/external match",
                "Backup folder-name identity",
                "path identity",
            ],
        }
    finally:
        con.close()


def apply_workspace_backfill(db_path: Path) -> int:
    """Copy external_id → workspace_id only when workspace is empty."""
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            """
            UPDATE mods
            SET workspace_id = TRIM(external_id)
            WHERE TRIM(COALESCE(workspace_id,'')) = ''
              AND TRIM(COALESCE(external_id,'')) != ''
            """
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--apply-workspace-backfill",
        action="store_true",
        help="Copy external_id into empty workspace_id (safe migration bridge)",
    )
    args = parser.parse_args(argv)
    report = build_report(args.db)
    if args.apply_workspace_backfill:
        n = apply_workspace_backfill(args.db)
        report["production_mutation"] = "WORKSPACE_BACKFILL_ONLY"
        report["workspace_backfill_rows"] = n
        report = {**report, **build_report(args.db)}
        report["workspace_backfill_rows"] = n
        report["production_mutation"] = "WORKSPACE_BACKFILL_ONLY"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), **{k: report[k] for k in ("phase", "production_mutation") if k in report}}, indent=2))
    print(json.dumps(report["legacy_fields"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
