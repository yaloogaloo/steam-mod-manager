#!/usr/bin/env python3
"""
Phase 7: Audit legacy LIVE ``.info/assets`` for SAFE_DELETE retirement.

Usage:
  python tools/audit_legacy_info_assets.py
  python tools/audit_legacy_info_assets.py --limit 50
  python tools/audit_legacy_info_assets.py --mod-id 3
  python tools/audit_legacy_info_assets.py --open-check materialize --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import asset_store_dir  # noqa: E402
from services.asset_store import AssetStore  # noqa: E402
from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import (  # noqa: E402
    AUDIT_JSON,
    AUDIT_MD,
    OpenCheckMode,
    audit_all_info_assets,
    audit_mod_info_assets,
    write_audit_reports,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit legacy .info/assets for Phase 7 SAFE_DELETE"
    )
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--open-check",
        choices=["manifest", "materialize"],
        default="manifest",
        help="OPEN readiness: manifest+CAS (fast) or full materialize",
    )
    parser.add_argument("--store-root", type=str, default="")
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument("--md-out", type=str, default="")
    args = parser.parse_args(argv)

    store = AssetStore(
        root=Path(args.store_root) if args.store_root else asset_store_dir()
    )
    open_check = OpenCheckMode(args.open_check)
    t0 = time.monotonic()

    if args.mod_id:
        mod = audit_mod_info_assets(
            args.mod_id, store=store, open_check=open_check
        )
        from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import InfoAssetCleanupAuditResult

        audit = InfoAssetCleanupAuditResult(open_check=open_check.value)
        audit.mods_scanned = 1
        audit.mods = [mod]
        audit.reason_breakdown[mod.classification.value] = 1
        if mod.classification.value == "NO_ASSETS":
            audit.no_assets_mods = 1
        elif mod.is_safe_delete:
            audit.safe_mods = 1
            audit.mods_with_info_assets = 1
            audit.files = mod.asset_files
            audit.bytes_total = mod.asset_bytes
            audit.files_reclaimable = mod.safe_files
            audit.bytes_reclaimable = mod.safe_bytes
        else:
            audit.keep_mods = 1
            if mod.asset_files:
                audit.mods_with_info_assets = 1
                audit.files = mod.asset_files
                audit.bytes_total = mod.asset_bytes
    else:
        audit = audit_all_info_assets(
            store=store,
            open_check=open_check,
            limit=int(args.limit or 0),
        )

    elapsed = time.monotonic() - t0
    json_out = Path(args.json_out) if args.json_out else _REPO / AUDIT_JSON
    md_out = Path(args.md_out) if args.md_out else _REPO / AUDIT_MD
    write_audit_reports(audit, json_path=json_out, md_path=md_out)

    print(
        "\n".join(
            [
                f"elapsed_s: {elapsed:.2f}",
                f"mods_scanned: {audit.mods_scanned}",
                f"mods_with_info_assets: {audit.mods_with_info_assets}",
                f"safe_mods: {audit.safe_mods}",
                f"keep_mods: {audit.keep_mods}",
                f"no_assets_mods: {audit.no_assets_mods}",
                f"files: {audit.files}",
                f"bytes_total: {audit.bytes_total}",
                f"files_reclaimable: {audit.files_reclaimable}",
                f"bytes_reclaimable: {audit.bytes_reclaimable}",
                f"reason_breakdown: {json.dumps(audit.reason_breakdown)}",
                f"json: {json_out}",
                f"md: {md_out}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
