#!/usr/bin/env python3
"""Fast GC for leftover LIVE ``.info/assets``.

Directory walk + manifest path set + unlink. No hash, no OPEN, no copy.

Usage:
  python tools/purge_legacy_info_assets.py --dry-run
  python tools/purge_legacy_info_assets.py --execute --batch-size 25 --resume
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.archive.legacy_asset_tools.fast_info_asset_purge import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    default_checkpoint_path,
    execute,
    format_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fast purge leftover .info/assets (GC, not migration)"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip cleaned_mod_ids from checkpoint",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default="",
        help="Override checkpoint path",
    )
    args = parser.parse_args(argv)

    checkpoint = Path(args.checkpoint) if args.checkpoint else default_checkpoint_path()
    result = execute(
        dry_run=not args.execute,
        batch_size=args.batch_size,
        resume=args.resume,
        checkpoint_path=checkpoint,
    )
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
