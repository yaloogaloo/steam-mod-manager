"""Validate ``data/mod_backup/<mod_id>`` integrity."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_GENERATED,
    normalize_offline_status,
)
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_METADATA_NAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    backup_root,
)

logger = logging.getLogger(__name__)

BACKUP_STATUS_COMPLETE = "complete"
BACKUP_STATUS_PARTIAL = "partial"
BACKUP_STATUS_MISSING = "missing"
BACKUP_STATUS_INVALID = "invalid"


def validate_backup(mod_id: int | str) -> dict[str, Any]:
    """
    Check backup snapshot integrity.

    Returns::

        {
            "metadata_ok": bool,
            "cover_ok": bool,
            "offline_ok": bool,
            "issues": list[str],
        }
    """
    mid = str(mod_id).strip()
    issues: list[str] = []
    cover_ok = True
    offline_ok = True

    if not mid.isdigit():
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": ["invalid mod_id"],
        }

    dest = backup_root(mid)
    meta_file = dest / BACKUP_METADATA_NAME
    if not meta_file.is_file():
        return {
            "metadata_ok": False,
            "cover_ok": False,
            "offline_ok": False,
            "issues": ["metadata.json missing"],
        }

    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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

    title = str(data.get("title") or data.get("display_name") or "").strip()
    if not title:
        issues.append("missing title/display_name")

    source_type = str(
        data.get("source_type") or data.get("platform") or ""
    ).strip()
    if not source_type:
        issues.append("missing source_type")

    source_url = str(data.get("source_url") or data.get("url") or "").strip()
    from core.mod_platform import PLATFORM_NEXUS, PLATFORM_OTHER, normalize_platform

    plat = normalize_platform(source_type) if source_type else ""
    # 「其它」and Nexus batch import may legally omit a URL. Steam / GitHub / mod.io
    # still require one for a complete Backup.
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

    db_cover = ""
    db_offline = ""
    db_offline_status = ""
    db_lkp = ""
    try:
        from core.db_manager import get_db

        row = get_db().get_mod_backup_row(mid)
        if row:
            db_cover = str(row.get("backup_cover_path") or "").strip()
            db_offline = str(row.get("backup_offline_path") or "").strip()
            db_offline_status = str(row.get("offline_status") or "").strip()
            db_lkp = str(row.get("last_known_path") or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("validate_backup DB peek failed for %s", mid, exc_info=True)

    cover_declared = bool(
        str(data.get("cover_path") or "").strip() or db_cover
    )
    if cover_declared:
        cover_path = Path(db_cover) if db_cover else None
        on_disk = cover_path is not None and cover_path.is_file()
        if not on_disk:
            on_disk = any(
                c.is_file()
                for c in dest.glob(f"{BACKUP_COVER_BASENAME}.*")
            )
        if not on_disk:
            cover_ok = False
            issues.append("declared cover missing on disk")

    offline_status = normalize_offline_status(
        str(data.get("offline_status") or db_offline_status or "")
    )
    live_source = None
    if db_lkp:
        try:
            from services.offline.paths import resolve_offline_page

            live_source = resolve_offline_page(db_lkp)
        except Exception:  # noqa: BLE001
            live_source = None
    dest_offline = dest / BACKUP_OFFLINE_DIR
    idx = dest_offline / BACKUP_OFFLINE_INDEX
    require_snapshot = bool(live_source is not None and live_source.is_file())
    if not require_snapshot:
        require_snapshot = offline_status in {
            OFFLINE_STATUS_GENERATED,
            OFFLINE_STATUS_ARCHIVED,
        }
    try:
        index_present = idx.is_file() or (
            bool(db_offline) and Path(db_offline).is_file()
        )
    except OSError:
        index_present = False
    if require_snapshot or index_present:
        from services.offline.backup_closure import validate_offline_snapshot

        closure_issues = validate_offline_snapshot(
            dest_offline, source_index=live_source
        )
        if closure_issues:
            offline_ok = False
            issues.extend(closure_issues)

    return {
        "metadata_ok": metadata_ok,
        "cover_ok": cover_ok,
        "offline_ok": offline_ok,
        "issues": issues,
    }


def status_from_validation(result: dict[str, Any]) -> str:
    """Map :func:`validate_backup` result to SQLite ``backup_status``."""
    issues = [str(i) for i in (result.get("issues") or [])]
    if any("metadata.json missing" in i for i in issues):
        return BACKUP_STATUS_MISSING
    if not result.get("metadata_ok"):
        return BACKUP_STATUS_INVALID
    if result.get("cover_ok") and result.get("offline_ok"):
        return BACKUP_STATUS_COMPLETE
    return BACKUP_STATUS_PARTIAL
