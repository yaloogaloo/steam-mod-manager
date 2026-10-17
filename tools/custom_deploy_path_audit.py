#!/usr/bin/env python3
"""Audit stale mods.custom_deploy_path (read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db_manager import DatabaseManager  # noqa: E402
from services.custom_deploy_path_stale import (  # noqa: E402
    audit_stale_custom_deploy_paths,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit stale custom_deploy_path")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "tools" / "_audit_out" / "custom_deploy_path_stale_report.json",
    )
    args = parser.parse_args(argv)

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(args.db)
    findings = audit_stale_custom_deploy_paths(db)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_path": str(args.db),
        "finding_count": len(findings),
        "findings": [f.to_dict() for f in findings],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.out} findings={len(findings)}")
    for f in findings:
        print(
            f"  mod_id={f.mod_id} app_id={f.app_id} code={f.code} "
            f"action={f.recommended_action} path={f.custom_deploy_path!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
