"""Generate Identity Pollution Repair Report (read-only by default)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply safe repairs (infer app_id / uniquify workspace). Never deletes.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="",
        help="Optional SQLite path (defaults to production data/mod_manager.db)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Optional report JSON path",
    )
    args = parser.parse_args()

    from core.db_manager import DatabaseManager
    from core.paths import database_path
    from services.identity_pollution import (
        apply_identity_pollution_repair,
        scan_identity_pollution,
        write_pollution_report,
    )

    DatabaseManager.reset_instance()
    db_path = Path(args.db) if args.db else database_path()
    db = DatabaseManager(db_path)
    report = scan_identity_pollution(db)
    out = write_pollution_report(
        report, path=Path(args.out) if args.out else None
    )
    print(f"report={out}")
    print(f"counts={report.to_dict()['counts']}")
    if args.apply:
        result = apply_identity_pollution_repair(db, report, apply=True)
        print(f"applied={len(result['applied'])} skipped={len(result['skipped'])}")
        again = scan_identity_pollution(db)
        print(f"after_counts={again.to_dict()['counts']}")
        write_pollution_report(again, path=out.with_name(out.stem + "_after.json"))
    db.close()
    DatabaseManager.reset_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
