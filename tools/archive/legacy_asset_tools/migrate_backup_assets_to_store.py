#!/usr/bin/env python3
"""
Phase 3 CLI: Backup offline → Asset Store manifest migration.

Never deletes production Backup ``offline/assets``.

Usage:
  python tools/migrate_backup_assets_to_store.py --dry-run --mod-id 1
  python tools/migrate_backup_assets_to_store.py --mod-id 1
  python tools/migrate_backup_assets_to_store.py --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import asset_store_dir  # noqa: E402
from services.asset_store import AssetStore  # noqa: E402
from services.backup_asset_migration import (  # noqa: E402
    migrate_all_backup_offline_assets,
    migrate_backup_offline_for_mod_id,
    restore_backup_assets_from_store,
)
from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root  # noqa: E402
from services.offline.backup_closure import usable_backup_offline_index  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Backup offline assets to Asset Store refs"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--store-root", type=str, default="")
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument(
        "--verify-restore-isolated",
        action="store_true",
        help=(
            "After migrate, copy Backup offline to _tmp, strip assets, "
            "restore from Store (never touches production Backup assets)"
        ),
    )
    args = parser.parse_args(argv)

    store_root = Path(args.store_root) if args.store_root else asset_store_dir()
    store = AssetStore(root=store_root)

    if args.mod_id:
        result = migrate_backup_offline_for_mod_id(
            args.mod_id, store=store, dry_run=args.dry_run
        )
        payload: dict = {"mode": "single", "result": result.to_dict()}
        bak = backup_root(args.mod_id) / BACKUP_OFFLINE_DIR
        payload["backup_offline_usable"] = (
            usable_backup_offline_index(bak) is not None
        )
        assets = bak / "assets"
        payload["production_backup_assets_present"] = (
            assets.is_dir() and any(assets.iterdir())
        )
        if args.verify_restore_isolated and result.ok and not args.dry_run:
            import shutil

            iso = _REPO / "_tmp" / f"backup_restore_iso_{args.mod_id}"
            if iso.exists():
                shutil.rmtree(iso)
            shutil.copytree(bak, iso)
            from services.backup_asset_migration import (
                strip_backup_offline_assets_for_test,
            )

            removed = strip_backup_offline_assets_for_test(iso)
            restore = restore_backup_assets_from_store(iso, store=store)
            payload["isolated_restore"] = {
                "assets_stripped": removed,
                "restore": restore.to_dict(),
                "usable_after": usable_backup_offline_index(iso) is not None,
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        out = Path(args.json_out) if args.json_out else (
            _REPO / "_tmp" / f"backup_asset_migration_mod_{args.mod_id}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
        return 0 if result.ok else 1

    batch = migrate_all_backup_offline_assets(
        store=store, dry_run=args.dry_run, limit=args.limit or None
    )
    payload = {"mode": "batch", "result": batch.to_dict()}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    out = Path(args.json_out) if args.json_out else (
        _REPO / "_tmp" / "backup_asset_migration_batch.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out}", file=sys.stderr)
    return 0 if batch.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
