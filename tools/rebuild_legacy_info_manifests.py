#!/usr/bin/env python3
"""
Phase 8: Rebuild LIVE ``.info`` manifests from legacy assets → Asset Store.

Does NOT delete ``.info/assets``. Deletion remains a later Phase 7 round.

Usage:
  python tools/rebuild_legacy_info_manifests.py --audit
  python tools/rebuild_legacy_info_manifests.py --mod-id 171
  python tools/rebuild_legacy_info_manifests.py --mod-id 171 --execute
  python tools/rebuild_legacy_info_manifests.py --batch --execute --resume
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import asset_store_dir, database_path  # noqa: E402
from services.asset_store import AssetStore  # noqa: E402
from tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild import (  # noqa: E402
    AUDIT_JSON,
    AUDIT_MD,
    DEFAULT_CHECKPOINT,
    audit_all_manifest_rebuild,
    rebuild_all_info_manifests,
    rebuild_mod_info_manifest,
    write_rebuild_audit_reports,
)


def _db_fingerprint() -> dict:
    path = database_path()
    try:
        st = path.stat()
        size, mtime = int(st.st_size), int(st.st_mtime_ns)
    except OSError:
        return {"ok": False}
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        n = int(conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
        conn.close()
    except sqlite3.Error:
        n = -1
    return {"size": size, "mtime_ns": mtime, "mods": n, "ok": True}


def _store_count(store: AssetStore) -> int:
    try:
        return sum(1 for _ in store.iter_objects())
    except Exception:  # noqa: BLE001
        return -1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 8 rebuild missing .info manifests (no asset delete)"
    )
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--verify-runtime",
        action="store_true",
        help="After each Mod: OPEN/MISS/Repair checks (slow; default on for --mod-id)",
    )
    parser.add_argument(
        "--no-verify-runtime",
        action="store_true",
        help="Skip OPEN/MISS/Repair (batch default)",
    )
    parser.add_argument("--store-root", type=str, default="")
    parser.add_argument("--checkpoint", type=str, default="")
    parser.add_argument("--json-out", type=str, default="")
    args = parser.parse_args(argv)

    store = AssetStore(
        root=Path(args.store_root) if args.store_root else asset_store_dir()
    )
    dry_run = not args.execute
    checkpoint = (
        Path(args.checkpoint) if args.checkpoint else _REPO / DEFAULT_CHECKPOINT
    )

    if args.audit or (not args.mod_id and not args.batch):
        t0 = time.monotonic()
        audit = audit_all_manifest_rebuild(
            store=store,
            limit=int(args.limit or 0),
            mod_ids=[args.mod_id] if args.mod_id else None,
        )
        json_out = Path(args.json_out) if args.json_out else _REPO / AUDIT_JSON
        write_rebuild_audit_reports(
            audit, json_path=json_out, md_path=_REPO / AUDIT_MD
        )
        print(
            f"audit elapsed={time.monotonic()-t0:.1f}s "
            f"candidates={audit.candidate_mods} blocked={audit.blocked_mods} "
            f"files={audit.files} bytes={audit.bytes_total}"
        )
        print(f"wrote {json_out}")
        if args.audit and not args.execute:
            return 0
        if not args.mod_id and not args.batch:
            return 0

    db_before = _db_fingerprint()
    store_before = _store_count(store)

    if args.mod_id and not args.batch:
        verify = not args.no_verify_runtime  # default ON for single
        if args.verify_runtime:
            verify = True
        result = rebuild_mod_info_manifest(
            args.mod_id,
            store=store,
            dry_run=dry_run,
            verify_runtime=verify and not dry_run,
        )
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        print(
            json.dumps(
                {
                    "db_unchanged": db_before == _db_fingerprint(),
                    "store_before": store_before,
                    "store_after": _store_count(store),
                },
                indent=2,
            )
        )
        return 0 if result.ok else 1

    if args.batch:
        # Batch: runtime verify off by default (manifest+CAS still verified)
        verify = bool(args.verify_runtime) and not args.no_verify_runtime
        batch = rebuild_all_info_manifests(
            store=store,
            dry_run=dry_run,
            resume=bool(args.resume),
            checkpoint_path=checkpoint,
            limit=int(args.limit or 0),
            verify_runtime=verify,
            stop_on_failure=False,
        )
        out_path = (
            Path(args.json_out)
            if args.json_out
            else _REPO / "_tmp" / "info_manifest_rebuild_batch.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = batch.to_dict()
        payload["db_before"] = db_before
        payload["db_after"] = _db_fingerprint()
        payload["db_unchanged"] = payload["db_before"] == payload["db_after"]
        payload["store_before"] = store_before
        payload["store_after"] = _store_count(store)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"batch ok={batch.ok} migrated={batch.mods_migrated} "
            f"skipped={batch.mods_skipped} failed={batch.mods_failed} "
            f"files={batch.files} bytes={batch.bytes_total} "
            f"new={batch.new_objects} reused={batch.reused_objects}"
        )
        print(f"wrote {out_path}")
        return 0 if batch.ok or batch.mods_migrated > 0 else 1

    parser.error("specify --audit, --mod-id, and/or --batch")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
