"""One-shot LIVE Offline Snapshot repair. Offline files only.

Never changes ``internal_id`` / ``workspace_id`` / ``mod_id`` / entity rows,
``last_known_path``, or Backup ``metadata.json``. Never creates a Mod folder
or ``.info``. Never clears a Backup snapshot because the source resolver
returned None.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def snapshot_live_offline_only(
    mod_id: int | str,
    managed_path: str | Path,
    *,
    db: Any | None = None,
) -> str:
    """Copy canonical live offline page + local closure into current storage key."""
    mid = str(mod_id or "").strip()
    root = Path(managed_path)
    if not mid.isdigit() or not root.is_dir():
        return ""
    from services.metadata_backup import BACKUP_OFFLINE_DIR, write_backup_root_for
    from services.backup_identity import BackupIdentityError
    from services.offline.backup_closure import (
        backup_offline_snapshot_valid,
        snapshot_offline_closure,
    )
    from services.offline.paths import resolve_offline_page

    src = resolve_offline_page(root)
    if src is None or not src.is_file():
        return ""
    try:
        dest_offline = write_backup_root_for(mid) / BACKUP_OFFLINE_DIR
    except BackupIdentityError:
        logger.warning(
            "offline snapshot repair refused: Invalid frozen internal_id mod_id=%s",
            mid,
        )
        return ""
    try:
        copied = snapshot_offline_closure(src, dest_offline)
    except OSError as exc:
        logger.warning("offline snapshot repair failed mod_id=%s: %s", mid, exc)
        return ""
    if not copied or not backup_offline_snapshot_valid(
        dest_offline, source_index=src
    ):
        return ""
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        database.update_mod_backup_offline_path(mid, copied)
    except Exception:  # noqa: BLE001
        logger.debug("persist repaired offline path failed mod_id=%s", mid, exc_info=True)
    return copied


def repair_live_offline_backups(
    library_root: str | Path | None = None,
    *,
    db: Any | None = None,
) -> dict[str, int]:
    """Repair usable Offline Snapshots for every current LIVE Mod.

    Storage key is ``mods.mod_id`` (current Backup bucket). Never uses
    ``workspace_id`` as a directory name.
    """
    from core.db_manager import get_db
    from services.metadata_backup import BACKUP_OFFLINE_DIR, readable_backup_root
    from services.offline.backup_closure import backup_offline_snapshot_valid
    from services.offline.paths import resolve_offline_page

    database = db if db is not None else get_db()
    lib = Path(library_root).resolve() if library_root else None
    stats = {
        "total_live_mods": 0,
        "source_offline_present": 0,
        "backup_valid": 0,
        "backup_repaired": 0,
        "backup_already_valid": 0,
        "backup_source_missing": 0,
        "backup_repair_failed": 0,
        "source_exists_backup_invalid": 0,
    }
    try:
        rows = database.iter_mod_backup_rows()
    except Exception:  # noqa: BLE001
        logger.exception("repair_live_offline_backups: failed to list mods")
        return stats

    for row in rows:
        mid = str(row.get("mod_id") or "").strip()
        if not mid.isdigit():
            continue
        lkp = str(row.get("last_known_path") or "").strip()
        if not lkp:
            continue
        path = Path(lkp)
        try:
            live = path.is_dir()
        except OSError:
            live = False
        if not live:
            continue
        if lib is not None:
            try:
                path.resolve().relative_to(lib)
            except (OSError, ValueError):
                continue
        stats["total_live_mods"] += 1
        src = None
        try:
            src = resolve_offline_page(path)
        except Exception:  # noqa: BLE001
            src = None
        dest_root = readable_backup_root(mid)
        dest_offline = (
            dest_root / BACKUP_OFFLINE_DIR if dest_root is not None else None
        )
        if src is None or not src.is_file():
            stats["backup_source_missing"] += 1
            continue
        stats["source_offline_present"] += 1
        if dest_offline is not None and backup_offline_snapshot_valid(
            dest_offline, source_index=src
        ):
            stats["backup_already_valid"] += 1
            stats["backup_valid"] += 1
            continue
        copied = snapshot_live_offline_only(mid, path, db=database)
        dest_root = readable_backup_root(mid)
        dest_offline = (
            dest_root / BACKUP_OFFLINE_DIR if dest_root is not None else None
        )
        if (
            copied
            and dest_offline is not None
            and backup_offline_snapshot_valid(dest_offline, source_index=src)
        ):
            stats["backup_repaired"] += 1
            stats["backup_valid"] += 1
            continue
        stats["backup_repair_failed"] += 1
        stats["source_exists_backup_invalid"] += 1

    logger.info(
        "offline backup integrity repair "
        "live=%s source=%s valid=%s repaired=%s already=%s "
        "source_missing=%s failed=%s still_invalid=%s",
        stats["total_live_mods"],
        stats["source_offline_present"],
        stats["backup_valid"],
        stats["backup_repaired"],
        stats["backup_already_valid"],
        stats["backup_source_missing"],
        stats["backup_repair_failed"],
        stats["source_exists_backup_invalid"],
    )
    return stats
