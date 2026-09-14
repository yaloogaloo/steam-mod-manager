#!/usr/bin/env python3
"""Independent Backup census: live filesystem + validate_backup, not backup_status."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "_tmp" / "dumps" / "legacy_workspace_backup"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_backup_metadata_only(mid: str, dest, meta) -> dict:
    """Filesystem + validator metadata rules. Does not walk offline/assets."""
    from core.mod_platform import PLATFORM_NEXUS, PLATFORM_OTHER, normalize_platform
    from services.metadata_backup_validator import status_from_validation

    if not str(mid).isdigit():
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": ["invalid mod_id"],
        }
    if dest is None or meta is None or not meta.is_file():
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": ["metadata.json missing"],
        }
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": [f"metadata.json unreadable: {exc}"],
        }
    if not isinstance(data, dict):
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": ["metadata.json is not an object"],
        }
    issues: list[str] = []
    title = str(data.get("title") or data.get("display_name") or "").strip()
    if not title:
        issues.append("missing title/display_name")
    source_type = str(data.get("source_type") or data.get("platform") or "").strip()
    if not source_type:
        issues.append("missing source_type")
    source_url = str(data.get("source_url") or data.get("url") or "").strip()
    plat = normalize_platform(source_type) if source_type else ""
    require_url = bool(plat) and plat not in {PLATFORM_OTHER, PLATFORM_NEXUS}
    if require_url and not source_url:
        issues.append("missing source_url")
    workspace_id = str(data.get("workspace_id") or "").strip()
    external_id = str(
        data.get("external_id") or data.get("published_file_id") or ""
    ).strip()
    if not workspace_id and not external_id:
        issues.append("missing workspace_id/external_id")
    metadata_ok = bool(title and source_type and (workspace_id or external_id))
    if require_url:
        metadata_ok = bool(metadata_ok and source_url)
    cover_ok = True
    if str(data.get("cover_path") or "").strip():
        cover_ok = any(dest.glob("cover.*"))
        if not cover_ok:
            issues.append("declared cover missing on disk")
    idx = dest / "offline" / "index.html"
    offline_ok = True
    if idx.is_file() or str(data.get("offline_status") or "") in {
        "generated",
        "archived",
    }:
        offline_ok = idx.is_file()
        if not offline_ok:
            issues.append("offline index.html missing")
    result = {
        "metadata_ok": metadata_ok,
        "cover_ok": cover_ok,
        "offline_ok": offline_ok,
        "issues": issues,
    }
    result["_status"] = status_from_validation(result)
    return result


def census(*, db=None) -> dict:
    from core.db_manager import get_db
    from core.paths import data_dir, default_mod_library
    from services.legacy_workspace_backup import (
        backup_writer_locked_to_mod_id,
        classify_legacy_workspace_buckets,
        inventory_legacy_workspace_buckets,
    )
    from services.metadata_backup import BACKUP_DIR_NAME, backup_root
    from services.metadata_backup_validator import (
        status_from_validation,
        validate_backup,
    )

    database = db or get_db()
    rows = list(database.iter_mod_backup_key_rows())
    source_exists_invalid: list[dict] = []
    source_exists_missing: list[dict] = []
    source_exists_partial: list[dict] = []
    live_offline_invalid: list[dict] = []
    by_status: dict[str, int] = {}
    live = 0
    print("census entities", len(rows), flush=True)
    for i, row in enumerate(rows, 1):
        if i % 200 == 0:
            print("census scanned", i, "/", len(rows), flush=True)
        mid = str(row.get("mod_id") or "").strip()
        lkp = Path(str(row.get("last_known_path") or "").strip())
        try:
            source_exists = bool(lkp.is_dir())
        except OSError:
            source_exists = False
        dest = backup_root(mid) if mid.isdigit() else None
        meta = dest / "metadata.json" if dest is not None else None
        # Canonical metadata.json + validator rules. Skip walking offline/assets
        # trees (that hung a 2990-mod full validate_backup pass).
        result = _validate_backup_metadata_only(mid, dest, meta)
        status = result.get("_status") or (
            status_from_validation(result) if mid.isdigit() else "invalid"
        )
        by_status[status] = by_status.get(status, 0) + 1
        rec = {
            "mod_id": mid,
            "internal_id": str(row.get("internal_id") or ""),
            "workspace_id": str(row.get("workspace_id") or ""),
            "last_known_path": str(lkp),
            "source_exists": source_exists,
            "backup_dir": str(dest) if dest is not None else "",
            "metadata_exists": bool(meta is not None and meta.is_file()),
            "status": status,
            "issues": list(result.get("issues") or []),
            "metadata_ok": bool(result.get("metadata_ok")),
            "cover_ok": bool(result.get("cover_ok")),
            "offline_ok": bool(result.get("offline_ok")),
        }
        if source_exists:
            live += 1
            if not rec["metadata_exists"]:
                source_exists_missing.append(rec)
            elif not rec["metadata_ok"] or status == "invalid":
                source_exists_invalid.append(rec)
            elif status == "partial":
                source_exists_partial.append(rec)
            if not rec["offline_ok"] and any(
                "offline" in str(x).lower() or "index.html" in str(x).lower()
                for x in rec["issues"]
            ):
                live_offline_invalid.append(rec)

    inv = inventory_legacy_workspace_buckets(db=database)
    classified = classify_legacy_workspace_buckets(db=database)
    writer = backup_writer_locked_to_mod_id()
    backup_root_path = data_dir() / BACKUP_DIR_NAME
    named_like_workspace = []
    current_ids = {str(r.get("mod_id") or "") for r in rows}
    workspace_ids = {
        str(r.get("workspace_id") or "").strip()
        for r in rows
        if str(r.get("workspace_id") or "").strip()
    }
    if backup_root_path.is_dir():
        for child in backup_root_path.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            if child.name in current_ids:
                continue
            if child.name in workspace_ids:
                named_like_workspace.append(child.name)
    return {
        "ts": _now(),
        "entity_count": len(rows),
        "live_source_count": live,
        "by_status": by_status,
        "source_exists_invalid": len(source_exists_invalid),
        "source_exists_missing": len(source_exists_missing),
        "source_exists_partial": len(source_exists_partial),
        "live_offline_invalid": len(live_offline_invalid),
        "source_exists_invalid_rows": source_exists_invalid[:50],
        "source_exists_missing_rows": source_exists_missing[:50],
        "live_offline_invalid_rows": live_offline_invalid[:50],
        "legacy": {
            "count": inv.get("legacy_bucket_count"),
            "bytes": inv.get("legacy_bucket_total_bytes"),
            "counts": classified.get("counts"),
        },
        "writer": writer,
        "legacy_named_like_workspace": named_like_workspace,
        "library_root": str(default_mod_library()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    from core.db_manager import DatabaseManager
    from core.paths import database_path

    DatabaseManager.instance(database_path())
    payload = census()
    out = Path(args.out) if args.out else (
        OUT_DIR / f"census_{_now().replace(':', '')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if not str(k).endswith("_rows")}, indent=2))
    print("Wrote", out)
    return 0 if payload["source_exists_invalid"] == 0 and payload["source_exists_missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
