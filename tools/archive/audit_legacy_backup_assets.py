#!/usr/bin/env python3
"""
Phase 4 / 4.1: Audit / migrate / cleanup legacy Backup offline/assets.

Usage:
  python tools/audit_legacy_backup_assets.py --fast-audit
  python tools/audit_legacy_backup_assets.py --deep-audit --limit 20
  python tools/audit_legacy_backup_assets.py --classify-manifest-debt
  python tools/audit_legacy_backup_assets.py --migrate-manifests --dry-run
  python tools/audit_legacy_backup_assets.py --migrate-manifests --execute
  python tools/audit_legacy_backup_assets.py --batch --execute --resume
  python tools/audit_legacy_backup_assets.py --mod-id 3 --execute --verify-restore
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
from tools.archive.legacy_asset_tools.backup_manifest_debt import (  # noqa: E402
    audit_manifest_debt,
    migrate_manifest_debt,
)
from tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup import (  # noqa: E402
    DEFAULT_CHECKPOINT,
    AuditMode,
    audit_legacy_backup_assets,
    cleanup_all_legacy_backup_assets,
    cleanup_mod_legacy_backup_assets,
)

OUT_DEFAULT = _REPO / "_tmp" / "legacy_backup_asset_cleanup_audit.json"
PHASE41_JSON = _REPO / "_tmp" / "phase41_legacy_backup_audit.json"
PHASE41_MD = _REPO / "_tmp" / "phase41_legacy_backup_audit.md"


def _print_summary(audit: dict) -> None:
    print(
        "\n".join(
            [
                f"Total backup asset files: {audit.get('backup_asset_files', 0)}",
                f"Total backup asset bytes: {audit.get('backup_asset_bytes', 0)}",
                "",
                f"Safe:    {audit.get('safe_files', 0)}",
                f"Unsafe:  {audit.get('unsafe_files', 0)}",
                f"Unknown: {audit.get('unknown_files', 0)}",
                "",
                f"Safe bytes reclaimable: {audit.get('safe_bytes', 0)}",
                f"Unsafe bytes:           {audit.get('unsafe_bytes', 0)}",
                f"Unknown bytes:          {audit.get('unknown_bytes', 0)}",
                "",
                (
                    f"Manifests: valid={audit.get('valid_manifests', 0)} "
                    f"invalid={audit.get('invalid_manifests', 0)} "
                    f"missing={audit.get('missing_manifests', 0)}"
                ),
                f"Missing CAS objects: {audit.get('missing_cas_objects', 0)}",
                f"Corrupt CAS objects: {audit.get('corrupt_cas_objects', 0)}",
            ]
        )
    )


def _write_phase41_md(path: Path, audit: dict, debt: dict | None, elapsed_s: float) -> None:
    lines = [
        "# Phase 4.1 Legacy Backup Audit",
        "",
        f"- elapsed_s: {elapsed_s:.2f}",
        f"- mods_scanned: {audit.get('mods_scanned')}",
        f"- backup_asset_files: {audit.get('backup_asset_files')}",
        f"- backup_asset_bytes: {audit.get('backup_asset_bytes')}",
        f"- SAFE: {audit.get('safe_files')} / {audit.get('safe_bytes')} bytes",
        f"- Unsafe: {audit.get('unsafe_files')} / {audit.get('unsafe_bytes')} bytes",
        f"- Unknown: {audit.get('unknown_files')} / {audit.get('unknown_bytes')} bytes",
        f"- valid_manifests: {audit.get('valid_manifests')}",
        f"- missing_manifests: {audit.get('missing_manifests')}",
        f"- reason_counts: `{json.dumps(audit.get('reason_counts') or {})}`",
        "",
    ]
    if debt:
        lines.extend(
            [
                "## Manifest debt",
                "",
                f"- migratable_mods: {debt.get('migratable_mods')}",
                f"- blocked_mods: {debt.get('blocked_mods')}",
                f"- by_class: `{json.dumps(debt.get('by_class') or {})}`",
                f"- by_class_bytes: `{json.dumps(debt.get('by_class_bytes') or {})}`",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit / migrate / cleanup legacy Backup offline/assets"
    )
    parser.add_argument("--dry-run", action="store_true", help="Classify only")
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    parser.add_argument("--mod-id", type=str, default="")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--store-root", type=str, default="")
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument(
        "--fast-audit",
        action="store_true",
        help="Fast metadata audit (default for dry-run)",
    )
    parser.add_argument(
        "--deep-audit",
        action="store_true",
        help="Deep audit with materialize verification",
    )
    parser.add_argument(
        "--standard-audit",
        action="store_true",
        help="File hash + CAS verify (no materialize)",
    )
    parser.add_argument(
        "--classify-manifest-debt",
        action="store_true",
        help="Classify MISSING_MANIFEST debt only",
    )
    parser.add_argument(
        "--migrate-manifests",
        action="store_true",
        help="Run Phase 3 Backup manifest migration for debt Mods",
    )
    parser.add_argument(
        "--verify-restore",
        action="store_true",
        help="After delete, temp-restore from CAS (expensive)",
    )
    parser.add_argument(
        "--skip-restore-verify",
        action="store_true",
        help="Deprecated alias: restore verify is off by default",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=str(DEFAULT_CHECKPOINT),
        help="Checkpoint path under _tmp",
    )
    parser.add_argument(
        "--phase41-report",
        action="store_true",
        help="Write _tmp/phase41_legacy_backup_audit.{json,md}",
    )
    args = parser.parse_args(argv)

    if args.execute and args.dry_run:
        print("Pass either --dry-run or --execute, not both", file=sys.stderr)
        return 2

    dry_run = not args.execute
    store_root = Path(args.store_root) if args.store_root else asset_store_dir()
    store = AssetStore(root=store_root)
    out_path = Path(args.json_out) if args.json_out else OUT_DEFAULT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path = Path(args.checkpoint)

    if args.deep_audit:
        mode = AuditMode.DEEP
    elif args.standard_audit:
        mode = AuditMode.STANDARD
    else:
        mode = AuditMode.FAST  # default / --fast-audit

    t0 = time.perf_counter()
    payload: dict = {"mode": "audit", "dry_run": dry_run, "audit_mode": mode.value}

    if args.classify_manifest_debt or args.phase41_report or args.migrate_manifests:
        debt = audit_manifest_debt(
            mod_ids=[args.mod_id] if args.mod_id else None,
            limit=args.limit or None,
            only_missing=True,
        )
        payload["manifest_debt"] = debt.to_dict()
        print("Manifest debt by_class:", json.dumps(debt.by_class, indent=2))
        print(
            f"migratable={debt.migratable_mods} blocked={debt.blocked_mods}",
            file=sys.stderr,
        )
        if args.classify_manifest_debt and not args.migrate_manifests and not args.phase41_report:
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"Wrote {out_path}", file=sys.stderr)
            return 0

    if args.migrate_manifests:
        mig = migrate_manifest_debt(
            store=store,
            dry_run=dry_run,
            mod_ids=[args.mod_id] if args.mod_id else None,
            limit=args.limit or None,
        )
        payload["mode"] = "migrate_manifests"
        payload["migration"] = mig.to_dict()
        print(json.dumps(mig.to_dict(), indent=2, ensure_ascii=False))
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
        return 0 if mig.ok else 1

    # Audit (unless execute-only with skip — always audit for reports)
    audit = audit_legacy_backup_assets(
        store=store,
        mod_ids=[args.mod_id] if args.mod_id else None,
        limit=args.limit or None,
        dry_run=True,
        mode=mode,
        include_file_details=bool(args.mod_id) and not args.batch,
    )
    audit_payload = audit.to_dict()
    audit_payload["elapsed_s"] = time.perf_counter() - t0
    _print_summary(audit_payload)
    payload["audit"] = audit_payload

    if args.phase41_report:
        debt_payload = payload.get("manifest_debt")
        if debt_payload is None:
            debt_payload = audit_manifest_debt(only_missing=True).to_dict()
            payload["manifest_debt"] = debt_payload
        PHASE41_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _write_phase41_md(
            PHASE41_MD, audit_payload, debt_payload, audit_payload["elapsed_s"]
        )
        print(f"Wrote {PHASE41_JSON} and {PHASE41_MD}", file=sys.stderr)

    verify_restore = bool(args.verify_restore) and not args.skip_restore_verify

    if args.execute:
        t1 = time.perf_counter()
        if args.mod_id and not args.batch:
            # Single-mod: restore verify ON unless --skip-restore-verify
            do_restore = not args.skip_restore_verify
            if args.verify_restore:
                do_restore = True
            result = cleanup_mod_legacy_backup_assets(
                args.mod_id,
                store=store,
                dry_run=False,
                verify_restore_after=do_restore,
                mode=AuditMode.STANDARD,
            )
            payload["mode"] = "single_execute"
            payload["cleanup"] = result.to_dict()
            payload["cleanup_elapsed_s"] = time.perf_counter() - t1
            post = audit_legacy_backup_assets(
                store=store,
                mod_ids=[args.mod_id],
                dry_run=True,
                mode=AuditMode.FAST,
            )
            payload["post_audit"] = post.to_dict()
            print(json.dumps(payload["cleanup"], indent=2, ensure_ascii=False))
            ok = result.ok
        else:
            batch = cleanup_all_legacy_backup_assets(
                store=store,
                dry_run=False,
                mod_ids=[args.mod_id] if args.mod_id else None,
                limit=args.limit or None,
                verify_restore_after=verify_restore,
                mode=AuditMode.STANDARD,
                checkpoint_path=ckpt_path,
                resume=args.resume,
            )
            payload["mode"] = "batch_execute"
            payload["cleanup"] = batch.to_dict()
            payload["cleanup_elapsed_s"] = time.perf_counter() - t1
            print(json.dumps(payload["cleanup"], indent=2, ensure_ascii=False))
            ok = batch.ok
    else:
        payload["mode"] = "dry_run"
        ok = True

    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
