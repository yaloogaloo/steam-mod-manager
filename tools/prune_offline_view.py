#!/usr/bin/env python3
"""
Prune ephemeral ``cache/offline_view/`` trees.

These directories are OPEN materializations only — safe to delete anytime.
They are recreated on next OPEN. Prefer ``tools/cleanup_cache.py`` for
LRU / full cache wipe.

Does NOT touch:
  .info/, Asset Store, Backup, asset_cache, DB, Identity

Usage:
  python tools/prune_offline_view.py --dry-run
  python tools/prune_offline_view.py --execute --confirm
  python tools/prune_offline_view.py --execute --confirm --older-than-hours 24
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import offline_view_cache_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune ephemeral cache/offline_view materializations"
    )
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --execute",
    )
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=0.0,
        help="Only prune dirs with mtime older than N hours (0 = all)",
    )
    parser.add_argument("--root", type=str, default="")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else offline_view_cache_dir()
    if not root.is_dir():
        print(f"offline_view missing: {root}")
        return 0

    text = str(root.resolve()).replace("\\", "/").lower()
    for bad in ("/asset_store", "/mod_backup", "/deploy_backup"):
        if bad in text and "/cache/" not in text:
            print(f"refusing unsafe root: {root}")
            return 2

    cutoff = 0.0
    if float(args.older_than_hours) > 0:
        cutoff = time.time() - float(args.older_than_hours) * 3600.0

    candidates: list[Path] = []
    total_bytes = 0
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if cutoff and mtime > cutoff:
            continue
        candidates.append(path)
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    total_bytes += int(f.stat().st_size)
        except OSError:
            pass

    print(
        f"offline_view={root} candidates={len(candidates)} "
        f"approx_bytes={total_bytes}"
    )
    if not args.execute:
        print("dry-run only (pass --execute --confirm to delete)")
        for p in candidates[:20]:
            print(f"  would remove {p.name}")
        if len(candidates) > 20:
            print(f"  ... +{len(candidates) - 20} more")
        return 0

    if not args.confirm:
        print("refusing: --execute requires --confirm")
        return 2

    removed = 0
    for path in candidates:
        try:
            shutil.rmtree(path, ignore_errors=False)
            removed += 1
        except OSError as exc:
            print(f"failed {path}: {exc}")
    print(f"removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
