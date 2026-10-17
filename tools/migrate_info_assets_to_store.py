#!/usr/bin/env python3
"""
Phase 2 CLI: migrate ``.info/assets`` → Durable Asset Store + Manifest.

Never deletes ``.info/assets``. Does not modify Backup / asset_cache / DB.

Usage:
  python tools/migrate_info_assets_to_store.py --dry-run --mod-id 1
  python tools/migrate_info_assets_to_store.py --mod-id 1
  python tools/migrate_info_assets_to_store.py --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import asset_store_dir  # noqa: E402
from services.asset_store import AssetStore  # noqa: E402
from services.info_asset_migration import (  # noqa: E402
    migrate_all_info_assets,
    migrate_info_assets_for_mod_id,
    offline_page_still_resolvable,
    resolve_mod_managed_path,
)
from services.offline.backup_closure import collect_offline_closure  # noqa: E402
from services.offline.paths import resolve_offline_page  # noqa: E402


def _validate_offline(managed: Path) -> dict:
    index = resolve_offline_page(managed)
    if index is None:
        return {"ok": False, "reason": "no offline index"}
    closure = collect_offline_closure(index)
    return {
        "ok": True,
        "index": str(index),
        "closure_files": len(closure),
        "resolvable": offline_page_still_resolvable(managed),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate .info/assets to Asset Store")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mod-id", type=str, default="", help="Single Mod SQLite PK")
    parser.add_argument("--limit", type=int, default=0, help="Batch limit (0=all)")
    parser.add_argument(
        "--store-root",
        type=str,
        default="",
        help="Override Asset Store root (default: data/asset_store)",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default="",
        help="Write result JSON to this path (default: _tmp/...)",
    )
    args = parser.parse_args(argv)

    store_root = Path(args.store_root) if args.store_root else asset_store_dir()
    store = AssetStore(root=store_root)

    if args.mod_id:
        result = migrate_info_assets_for_mod_id(
            args.mod_id, store=store, dry_run=args.dry_run
        )
        payload: dict = {"mode": "single", "result": result.to_dict()}
        folder = resolve_mod_managed_path(args.mod_id)
        if folder is not None:
            payload["offline_validation"] = _validate_offline(folder)
            # Confirm assets still on disk
            from services.info_asset_migration import discover_info_asset_trees, iter_asset_files

            remaining = 0
            for tree in discover_info_asset_trees(folder):
                remaining += sum(1 for _ in iter_asset_files(tree.assets_dir))
            payload["info_assets_still_present"] = remaining
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        out = Path(args.json_out) if args.json_out else (
            _REPO / "_tmp" / f"info_asset_migration_mod_{args.mod_id}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
        return 0 if result.ok else 1

    batch = migrate_all_info_assets(
        store=store,
        dry_run=args.dry_run,
        limit=args.limit or None,
    )
    payload = {"mode": "batch", "result": batch.to_dict()}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    out = Path(args.json_out) if args.json_out else (
        _REPO / "_tmp" / "info_asset_migration_batch.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out}", file=sys.stderr)
    return 0 if batch.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
