#!/usr/bin/env python3
"""Identity governance CLI: audit / repair --dry-run / repair --apply / verify."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.db_manager import DatabaseManager  # noqa: E402
from core.paths import data_dir, default_mod_library  # noqa: E402
from services.mod_identity_repair import (  # noqa: E402
    audit_severity_counts,
    repair_mod_library_identity,
)
from services.mod_library_integrity_audit import audit_mod_library_integrity  # noqa: E402


def _db():
    DatabaseManager.reset_instance()
    return DatabaseManager.instance(data_dir() / "mod_manager.db")


def cmd_audit(library: Path, out: Path | None) -> int:
    db = _db()
    report = audit_mod_library_integrity(library, db=db)
    counts = audit_severity_counts(report)
    payload = {
        "library": str(library),
        "scanned_folders": report.scanned_folders,
        "scanned_db_rows": report.scanned_db_rows,
        "counts": counts,
        "global_findings": [
            {
                "code": f.code.value,
                "severity": f.severity.value,
                "message": f.message,
                "mod_id": f.mod_id,
                "source_url": f.source_url,
                "external_id": f.external_id,
                "expected": f.expected,
                "actual": f.actual,
            }
            for f in report.global_findings
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    DatabaseManager.reset_instance()
    return 0 if counts.get("CRITICAL", 0) == 0 and counts.get("HIGH", 0) == 0 else 2


def cmd_repair(library: Path, apply: bool, out: Path | None) -> int:
    if apply:
        os.environ.setdefault("SMM_IDENTITY_RECOVERY", "1")
    db = _db()
    plan = repair_mod_library_identity(library, db=db, apply=apply)
    text = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    DatabaseManager.reset_instance()
    if not plan.success:
        return 1
    if apply:
        after = plan.after
        if after.get("CRITICAL", 0) or after.get("HIGH", 0):
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_audit = sub.add_parser("audit", help="Read-only integrity audit")
    p_audit.add_argument("--out", type=Path, default=None)

    p_repair = sub.add_parser("repair", help="Build / apply identity repair plan")
    p_repair.add_argument(
        "--apply",
        action="store_true",
        help="Apply mutations (default: dry-run)",
    )
    p_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default)",
    )
    p_repair.add_argument("--out", type=Path, default=None)

    p_verify = sub.add_parser("verify", help="Alias of audit with exit code gate")
    p_verify.add_argument("--out", type=Path, default=None)

    p_id = sub.add_parser(
        "identity-repair",
        help="Historical identity pollution planner (dry-run by default)",
    )
    p_id.add_argument("--audit", action="store_true", help="Read-only plan (default)")
    p_id.add_argument("--apply", action="store_true", help="Apply mutations")
    p_id.add_argument("--yes", action="store_true", help="Required for --apply")
    p_id.add_argument("--out", type=Path, default=None)
    p_id.add_argument("--db", type=Path, default=None)

    args = parser.parse_args(argv)
    library = Path(args.library) if args.library else Path(default_mod_library())
    out = getattr(args, "out", None)

    if args.cmd in {"audit", "verify"}:
        return cmd_audit(library, out)
    if args.cmd == "repair":
        apply = bool(args.apply) and not bool(args.dry_run)
        return cmd_repair(library, apply=apply, out=out)
    if args.cmd == "identity-repair":
        from services.identity_repair import main as identity_repair_main

        argv2: list[str] = ["--library", str(library)]
        if args.db:
            argv2.extend(["--db", str(args.db)])
        if out:
            argv2.extend(["--out", str(out)])
        if args.apply:
            argv2.append("--apply")
            if args.yes:
                argv2.append("--yes")
        else:
            argv2.append("--audit")
        return identity_repair_main(argv2)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
