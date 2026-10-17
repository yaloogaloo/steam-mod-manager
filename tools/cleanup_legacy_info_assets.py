#!/usr/bin/env python3
"""
Phase 7: Cleanup (retire) legacy LIVE ``.info/assets``.

Default execute verify is LIGHTWEIGHT — delete + manifest + Asset Store only.
No offline_view / OPEN materialize unless ``--deep-verify`` / ``--sample-verify``.

Usage:
  python tools/cleanup_legacy_info_assets.py --audit
  python tools/cleanup_legacy_info_assets.py --mod-id 3 --execute
  python tools/cleanup_legacy_info_assets.py --batch --execute --from-audit AUDIT.json --resume
  python tools/cleanup_legacy_info_assets.py --batch --execute --resume --sample-verify 5
  python tools/cleanup_legacy_info_assets.py --batch --execute --deep-verify  # slow gate
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
from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import (  # noqa: E402
    AUDIT_JSON,
    AUDIT_MD,
    DEFAULT_CHECKPOINT,
    OpenCheckMode,
    audit_all_info_assets,
    cleanup_all_safe_info_assets,
    cleanup_mod_info_assets,
    write_audit_reports,
)


def _db_fingerprint() -> dict:
    path = database_path()
    try:
        st = path.stat()
        size = int(st.st_size)
        mtime = int(st.st_mtime_ns)
    except OSError:
        return {"path": str(path), "ok": False}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        n = int(conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
        conn.close()
    except sqlite3.Error:
        n = -1
    return {"path": str(path), "size": size, "mtime_ns": mtime, "mods": n, "ok": True}


def _store_count(store: AssetStore) -> int:
    try:
        return sum(1 for _ in store.iter_objects())
    except Exception:  # noqa: BLE001
        return -1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 7 cleanup legacy .info/assets (SAFE_DELETE only)"
    )
    parser.add_argument("--audit", action="store_true", help="Audit only")
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--skip-failures",
        action="store_true",
        help="On Mod failure, record and continue (default: stop batch)",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--open-check",
        choices=["manifest", "materialize"],
        default="manifest",
        help="Audit OPEN check mode (batch post-delete is light unless --deep-verify)",
    )
    parser.add_argument(
        "--deep-verify",
        action="store_true",
        help=(
            "GATE/manual only: after delete, materialize offline_view + "
            "OPEN/MISS/Repair (SLOW — never use for bulk cleanup)"
        ),
    )
    parser.add_argument(
        "--light-verify",
        action="store_true",
        help=argparse.SUPPRESS,  # legacy alias; light is already the default
    )
    parser.add_argument(
        "--sample-verify",
        type=int,
        default=-1,
        metavar="N",
        help=(
            "After batch light cleanup: deep-verify N random cleaned Mods "
            "plus largest-by-files and largest-by-bytes (default: disabled)"
        ),
    )
    parser.add_argument(
        "--from-audit",
        type=str,
        default="",
        help="JSON audit file; batch only SAFE_DELETE mod_ids (skip re-scan + trust audit)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=2000,
        help="Unlink batch size before pause (default 2000)",
    )
    parser.add_argument(
        "--batch-pause-ms",
        type=int,
        default=40,
        help="Pause between unlink batches in ms (default 40)",
    )
    parser.add_argument(
        "--checkpoint-every-files",
        type=int,
        default=2000,
        help="Checkpoint after this many deleted files (default 2000)",
    )
    parser.add_argument("--store-root", type=str, default="")
    parser.add_argument("--checkpoint", type=str, default="")
    parser.add_argument("--json-out", type=str, default="")
    args = parser.parse_args(argv)

    store = AssetStore(
        root=Path(args.store_root) if args.store_root else asset_store_dir()
    )
    dry_run = not args.execute
    open_check = OpenCheckMode(args.open_check)
    checkpoint = (
        Path(args.checkpoint) if args.checkpoint else _REPO / DEFAULT_CHECKPOINT
    )

    if args.audit or (not args.mod_id and not args.batch):
        t0 = time.monotonic()
        audit = audit_all_info_assets(
            store=store,
            open_check=open_check,
            limit=int(args.limit or 0),
            mod_ids=[args.mod_id] if args.mod_id else None,
        )
        json_out = Path(args.json_out) if args.json_out else _REPO / AUDIT_JSON
        write_audit_reports(
            audit, json_path=json_out, md_path=_REPO / AUDIT_MD
        )
        print(
            f"audit elapsed={time.monotonic()-t0:.1f}s "
            f"safe={audit.safe_mods} keep={audit.keep_mods} "
            f"reclaimable_files={audit.files_reclaimable} "
            f"reclaimable_bytes={audit.bytes_reclaimable}"
        )
        print(f"wrote {json_out}")
        if not args.execute and not args.batch and not args.mod_id:
            return 0
        if args.audit and not args.execute:
            return 0

    db_before = _db_fingerprint()
    store_before = _store_count(store)

    if args.mod_id and not args.batch:
        # Default LIGHT (no offline_view). Opt in with --deep-verify.
        deep = bool(args.deep_verify) and not bool(args.light_verify)
        result = cleanup_mod_info_assets(
            args.mod_id,
            store=store,
            dry_run=dry_run,
            open_check=OpenCheckMode.MANIFEST,
            deep_verify=deep,
        )
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        db_after = _db_fingerprint()
        store_after = _store_count(store)
        print(
            json.dumps(
                {
                    "db_unchanged": db_before == db_after,
                    "asset_store_objects_before": store_before,
                    "asset_store_objects_after": store_after,
                    "asset_store_objects_deleted": max(0, store_before - store_after)
                    if store_before >= 0 and store_after >= 0
                    else None,
                },
                indent=2,
            )
        )
        return 0 if result.ok else 1

    if args.batch:
        mod_ids = None
        audit_by_mod = None
        trust_audit = False
        if args.from_audit:
            audit_path = Path(args.from_audit)
            from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import load_audit_snapshots

            audit_by_mod = load_audit_snapshots(audit_path)
            mod_ids = list(audit_by_mod.keys())
            trust_audit = True
            print(
                f"from-audit SAFE_DELETE mods: {len(mod_ids)} "
                f"(trust-audit, no re-scan)"
            )
        deep = bool(args.deep_verify)
        sample_n = int(args.sample_verify)
        if deep and sample_n >= 0:
            print(
                "note: --deep-verify already deep-checks every Mod; "
                "sample-verify skipped"
            )
            sample_n = -1
        print(
            f"batch verify_mode={'deep' if deep else 'light'}"
            + (f" sample_verify={sample_n}" if sample_n >= 0 else "")
        )
        batch = cleanup_all_safe_info_assets(
            store=store,
            dry_run=dry_run,
            resume=bool(args.resume),
            checkpoint_path=checkpoint,
            limit=int(args.limit or 0),
            mod_ids=mod_ids,
            open_check=OpenCheckMode.MANIFEST,
            stop_on_failure=not bool(args.skip_failures),
            trust_audit=trust_audit,
            audit_by_mod=audit_by_mod,
            batch_size=int(args.batch_size),
            batch_pause_ms=int(args.batch_pause_ms),
            checkpoint_every_files=int(args.checkpoint_every_files),
            deep_verify=deep,
            sample_verify=sample_n,
        )
        out_path = (
            Path(args.json_out)
            if args.json_out
            else _REPO / "_tmp" / "info_asset_cleanup_final_batch.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = batch.to_dict()
        payload["db_before"] = db_before
        payload["db_after"] = _db_fingerprint()
        payload["store_before"] = store_before
        payload["store_after"] = _store_count(store)
        payload["db_unchanged"] = payload["db_before"] == payload["db_after"]
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"batch ok={batch.ok} cleaned={batch.mods_cleaned} "
            f"skipped={batch.mods_skipped} failed={batch.mods_failed} "
            f"files={batch.files_deleted} bytes={batch.bytes_reclaimed} "
            f"verify={batch.verify_mode}"
        )
        if batch.sample_verify_mod_ids:
            sample_ok = all(r.ok for r in batch.sample_verify_results)
            print(
                f"sample_verify ids={batch.sample_verify_mod_ids} "
                f"ok={sample_ok}"
            )
        print(f"wrote {out_path}")
        return 0 if batch.ok else 1

    parser.error("specify --audit, --mod-id, and/or --batch")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
