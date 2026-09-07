#!/usr/bin/env python3
"""Identity full rebuild — Phase 1 read-only disk audit.

Scans ``mod/<game>/<mod>/.info`` and writes ``rebuild_audit.json``.

Never mutates DB / .info / lifecycle contracts.

Usage::

    python tools/identity_full_rebuild_audit.py
    python tools/identity_full_rebuild_audit.py --out tools/_audit_out/rebuild_audit.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_full_rebuild_common import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_LIBRARY,
    OUT_DIR,
    mark_duplicate_workspace,
    now_utc,
    scan_disk_entries,
    write_json,
)


def build_audit(*, library: Path, db_path: Path) -> dict:
    entries = scan_disk_entries(library, db_path=db_path)
    mark_duplicate_workspace(entries)
    rows = []
    for entry in entries:
        rows.append(
            {
                "game": entry["game"],
                "app_id": entry["app_id"],
                "workspace_id": entry["workspace_id"],
                "title": entry["title"],
                "current_internal_id": entry["current_internal_id"],
                "path": entry["path"],
                "duplicate_workspace": bool(entry.get("duplicate_workspace")),
            }
        )
    dup_groups = sum(1 for r in rows if r["duplicate_workspace"])
    return {
        "generated_at": now_utc(),
        "tool": "identity_full_rebuild_audit",
        "library_root": str(library.resolve()),
        "db_path": str(db_path.resolve()) if db_path.is_file() else str(db_path),
        "counts": {
            "entries": len(rows),
            "duplicate_workspace_entries": dup_groups,
            "missing_workspace": sum(1 for r in rows if not r["workspace_id"]),
            "missing_app_id": sum(1 for r in rows if int(r["app_id"] or 0) <= 0),
        },
        "entries": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR / "rebuild_audit.json",
    )
    args = parser.parse_args()
    audit = build_audit(library=args.library, db_path=args.db)
    out = write_json(args.out, audit)
    print(f"wrote={out}")
    print(f"counts={audit['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
