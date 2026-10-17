#!/usr/bin/env python3
"""Identity full rebuild — Phase 2/3 workspace clean + preview.

Rules
-----
- Same ``app_id``: ``workspace_id`` must be unique; keep one directory,
  mark others ``DELETE_CANDIDATE``.
- Cross-game: identical ``workspace_id`` is allowed (e.g. BG3 + Stardew).

Never uses folder name / external_id / published_file_id as entity identity.
Never mutates DB / .info (preview only).

Usage::

    python tools/identity_full_rebuild_preview.py
    python tools/identity_full_rebuild_preview.py --audit tools/_audit_out/rebuild_audit.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_full_rebuild_common import (  # noqa: E402
    ACTION_DELETE_CANDIDATE,
    ACTION_REBUILD,
    ACTION_SKIP,
    DEFAULT_DB,
    DEFAULT_LIBRARY,
    OUT_DIR,
    load_json,
    now_utc,
    plan_workspace_actions,
    scan_disk_entries,
    text,
    write_json,
)


def build_preview(
    *,
    library: Path,
    db_path: Path,
    audit_path: Path | None = None,
) -> dict:
    # Always re-scan disk for planning (audit is evidence, not authority).
    entries = scan_disk_entries(library, db_path=db_path)
    planned = plan_workspace_actions(entries)

    # Strip bulky info blobs from preview rows (keep metadata pointers).
    items = []
    for row in planned:
        item = dict(row)
        info = item.pop("info", None)
        item["has_info_payload"] = bool(info)
        items.append(item)

    counts = {
        "total": len(items),
        ACTION_REBUILD: sum(1 for i in items if i["action"] == ACTION_REBUILD),
        ACTION_DELETE_CANDIDATE: sum(
            1 for i in items if i["action"] == ACTION_DELETE_CANDIDATE
        ),
        ACTION_SKIP: sum(1 for i in items if i["action"] == ACTION_SKIP),
    }

    # Cross-game same workspace evidence (allowed).
    by_ws: dict[str, set[int]] = {}
    for item in items:
        if item["action"] != ACTION_REBUILD:
            continue
        ws = text(item.get("workspace_id"))
        app_id = int(item.get("app_id") or 0)
        if not ws or app_id <= 0:
            continue
        by_ws.setdefault(ws, set()).add(app_id)
    cross_game = [
        {"workspace_id": ws, "app_ids": sorted(apps)}
        for ws, apps in sorted(by_ws.items())
        if len(apps) > 1
    ]

    audit_ref = ""
    if audit_path and audit_path.is_file():
        audit_ref = str(audit_path.resolve())
        _ = load_json(audit_path)

    return {
        "generated_at": now_utc(),
        "tool": "identity_full_rebuild_preview",
        "library_root": str(library.resolve()),
        "db_path": str(db_path.resolve()) if db_path.is_file() else str(db_path),
        "audit_ref": audit_ref,
        "counts": counts,
        "cross_game_workspace_allowed": cross_game,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--audit",
        type=Path,
        default=OUT_DIR / "rebuild_audit.json",
        help="Optional Phase-1 audit path (reference only)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR / "rebuild_preview.json",
    )
    args = parser.parse_args()
    preview = build_preview(
        library=args.library,
        db_path=args.db,
        audit_path=args.audit,
    )
    out = write_json(args.out, preview)
    print(f"wrote={out}")
    print(f"counts={preview['counts']}")
    print(f"cross_game_workspace={len(preview['cross_game_workspace_allowed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
