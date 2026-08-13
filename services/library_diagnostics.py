"""Read-only Library diagnostics payload (Phase 7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.paths import data_dir, default_mod_library
from services.library_maintenance import scan_library_issues
from services.library_status import (
    CONTENT_BACKUP_INVALID,
    CONTENT_FOLDER_MISSING,
    CONTENT_IDENTITY_CONFLICT,
    CONTENT_METADATA_MISSING,
    row_content_status,
)
from services.metadata_backup import BACKUP_DIR_NAME


def build_library_diagnostics(
    library_root: str | Path | None = None,
    *,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Scan-only diagnostics dict for scripts / tests."""
    from core.db_manager import get_db

    root = Path(library_root) if library_root else Path(default_mod_library())
    data = Path(data_root) if data_root else Path(data_dir())
    report = scan_library_issues(root, data_root=data)
    db = get_db()

    missing_metadata: list[str] = []
    invalid_backup: list[str] = []
    identity_conflict: list[str] = []
    folder_missing_mods: list[str] = []

    try:
        with db._lock:
            rows = db._conn.execute(
                """
                SELECT mod_id, content_status, library_status, backup_status,
                       folder_present, last_known_path
                FROM mods
                """
            ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []

    for row in rows:
        mid = str(row["mod_id"])
        brow = {str(k): row[k] for k in row.keys()}
        cs = row_content_status(brow)
        bstatus = str(brow.get("backup_status") or "").strip()
        if cs == CONTENT_METADATA_MISSING:
            missing_metadata.append(mid)
        if cs == CONTENT_BACKUP_INVALID or bstatus == "invalid":
            invalid_backup.append(mid)
        if cs == CONTENT_IDENTITY_CONFLICT:
            identity_conflict.append(mid)
        if cs == CONTENT_FOLDER_MISSING or int(brow.get("folder_present") or 1) == 0:
            folder_missing_mods.append(mid)

    backup_base = data / BACKUP_DIR_NAME
    _sort = lambda xs: sorted(set(xs), key=lambda x: int(x) if str(x).isdigit() else x)

    return {
        "library_root": str(root),
        "data_root": str(data),
        "games": {
            "missing_folder": list(report.missing_folder_games),
            "orphan_games": list(report.orphan_games),
            "test_like": list(report.test_like_entries),
        },
        "mods": {
            "folder_missing": _sort(folder_missing_mods),
            "missing_metadata": _sort(missing_metadata),
            "invalid_backup": _sort(invalid_backup),
            "identity_conflict": _sort(identity_conflict),
            "orphan_mods": list(report.orphan_mods),
        },
        "backup": {
            "orphan_backup": list(report.orphan_backup),
            "backup_dir": str(backup_base),
        },
        "notes": list(report.notes),
    }
