#!/usr/bin/env python3
"""
Phase 8.1: Audit Mods missing LIVE ``.info`` asset manifests.

Usage:
  python tools/audit_legacy_info_assets_manifest.py
  python tools/audit_legacy_info_assets_manifest.py --limit 100
  python tools/audit_legacy_info_assets_manifest.py --mod-id 171
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

from tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild import (  # noqa: E402
    AUDIT_JSON,
    AUDIT_MD,
    ManifestRebuildAuditResult,
    audit_all_manifest_rebuild,
    audit_mod_manifest_rebuild,
    write_rebuild_audit_reports,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit missing .info manifests for Phase 8 rebuild"
    )
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument("--md-out", type=str, default="")
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    if args.mod_id:
        mod = audit_mod_manifest_rebuild(args.mod_id)
        audit = ManifestRebuildAuditResult()
        audit.mods_scanned = 1
        audit.mods = [mod]
        audit.reason_breakdown[mod.classification.value] = 1
        if mod.can_migrate:
            audit.candidate_mods = 1
            audit.files = mod.asset_files
            audit.bytes_total = mod.asset_bytes
        else:
            audit.blocked_mods = 1
            audit.blocked_count = 1
    else:
        audit = audit_all_manifest_rebuild(limit=int(args.limit or 0))

    json_out = Path(args.json_out) if args.json_out else _REPO / AUDIT_JSON
    md_out = Path(args.md_out) if args.md_out else _REPO / AUDIT_MD
    write_rebuild_audit_reports(audit, json_path=json_out, md_path=md_out)
    print(
        "\n".join(
            [
                f"elapsed_s: {time.monotonic() - t0:.2f}",
                f"mods_scanned: {audit.mods_scanned}",
                f"candidate_mods: {audit.candidate_mods}",
                f"blocked_mods: {audit.blocked_mods}",
                f"already_ok_mods: {audit.already_ok_mods}",
                f"files: {audit.files}",
                f"bytes: {audit.bytes_total}",
                f"blocked_count: {audit.blocked_count}",
                f"reason_breakdown: {json.dumps(audit.reason_breakdown)}",
                f"json: {json_out}",
                f"md: {md_out}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
