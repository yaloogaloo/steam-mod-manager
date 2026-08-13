"""Mod Detail refresh — local reconcile first, optional one-shot official sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_STEAM,
    normalize_platform,
    silent_correct_nexus_workspace_id,
)
from services.library_reconcile import _folder_has_metadata, _folder_missing_content
from services.library_status import (
    compute_content_status,
    content_status_to_library_status,
    row_content_status,
)
from services.metadata_ownership import (
    FIELD_COVER,
    FIELD_DESCRIPTION,
    FIELD_DISPLAY_NAME,
    should_apply_official_field,
)
from services.metadata_refresh import MetadataRefreshResult

logger = logging.getLogger(__name__)

__all__ = [
    "LocalReconcileResult",
    "ModRefreshResult",
    "reconcile_local_state",
    "refresh_mod",
]


@dataclass
class LocalReconcileResult:
    mod_id: str
    folder_present: bool
    content_status: str = "healthy"
    missing_content: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ModRefreshResult:
    mod_id: str
    success: bool
    local: LocalReconcileResult | None = None
    official_attempted: bool = False
    official_success: bool = False
    official_synced: bool = False
    provider: MetadataRefreshResult | None = None
    message: str = ""
    error: str = ""

    def to_metadata_refresh_result(self) -> MetadataRefreshResult:
        """Compat shim for existing Detail panel worker handlers."""
        prov = self.provider
        path = prov.managed_path if prov and prov.managed_path else None
        return MetadataRefreshResult(
            mod_id=self.mod_id,
            success=self.success,
            skipped=not self.official_attempted and self.success,
            renamed=bool(prov and prov.renamed),
            managed_path=path,
            old_path=prov.old_path if prov else path,
            title=prov.title if prov else "",
            error=self.error,
            cover_path=prov.cover_path if prov else "",
            message=self.message,
        )


def reconcile_local_state(
    mod_id: int | str,
    managed_path: str | Path | None,
    *,
    library_root: str | Path | None = None,
    db=None,
) -> LocalReconcileResult:
    """
    Recompute folder/content/backup-related state from disk — no network I/O.
    """
    from core.db_manager import get_db
    from services.file_ops import (
        apply_missing_content_marker,
        clear_missing_content_if_present,
    )
    from services.metadata_backup import mark_missing
    from services.metadata_backup_sync import sync_after_metadata_change

    database = db if db is not None else get_db()
    mid = str(mod_id or "").strip()
    notes: list[str] = []

    folder: Path | None = None
    if managed_path is not None:
        candidate = Path(managed_path)
        if candidate.is_dir():
            folder = candidate

    if folder is None:
        row = database.get_mod_backup_row(mid) or {}
        lkp = str(row.get("last_known_path") or "").strip()
        if lkp and Path(lkp).is_dir():
            folder = Path(lkp)

    logger.info("[refresh] mod_id=%s local reconcile started", mid)

    if folder is None or not folder.is_dir():
        mark_missing(mid)
        row = database.get_mod_backup_row(mid) or {}
        bstatus = str(row.get("backup_status") or "").strip()
        cs = compute_content_status(folder_present=False, backup_status=bstatus)
        try:
            database.update_mod_identity_fields(
                mid,
                content_status=cs,
                library_status=content_status_to_library_status(cs),
                folder_present=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("update folder_missing failed for %s: %s", mid, exc)
        logger.info(
            "[refresh] mod_id=%s folder missing content_status=%s",
            mid,
            cs,
        )
        return LocalReconcileResult(
            mod_id=mid,
            folder_present=False,
            content_status=cs,
            missing_content=False,
            notes=["folder_missing"],
        )

    # Heal missing-content marker from live payload
    if clear_missing_content_if_present(folder):
        notes.append("cleared_stale_missing_flag")
    elif apply_missing_content_marker(folder, sync_backup=False):
        notes.append("marked_missing_content")

    missing = _folder_missing_content(folder)
    meta_missing = not _folder_has_metadata(folder)
    row = database.get_mod_backup_row(mid) or {}
    backup_status = str(row.get("backup_status") or "").strip()
    cs = compute_content_status(
        folder_present=True,
        missing_content=missing,
        metadata_missing=meta_missing,
        backup_status=backup_status,
    )
    try:
        database.update_mod_identity_fields(
            mid,
            content_status=cs,
            library_status=content_status_to_library_status(cs),
            folder_present=True,
            last_known_path=str(folder.resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("update local content_status failed for %s: %s", mid, exc)

    try:
        if sync_after_metadata_change(mid, folder, "refresh"):
            notes.append("backup_synced")
    except Exception as exc:  # noqa: BLE001
        logger.debug("backup sync during refresh failed for %s: %s", mid, exc)

    # Local archive / sidecar rescan (no remote providers)
    try:
        from services.info_sidecar import rescan_mod_folder

        plat = normalize_platform(str(row.get("platform") or ""))
        if plat == "nexus":
            silent_correct_nexus_workspace_id(mid)
        rescan_mod_folder(folder, mod_id=mid, db=database)
        if plat == "nexus":
            silent_correct_nexus_workspace_id(mid)
        notes.append("local_rescan")
    except Exception as exc:  # noqa: BLE001
        logger.debug("local rescan failed for %s: %s", mid, exc)

    logger.info(
        "[refresh] mod_id=%s local reconcile done content_status=%s folder_present=1",
        mid,
        cs,
    )
    return LocalReconcileResult(
        mod_id=mid,
        folder_present=True,
        content_status=cs,
        missing_content=missing,
        notes=notes,
    )


def _should_attempt_official_sync(db, mod_id: str) -> bool:
    return not db.is_official_metadata_synced(mod_id)


def refresh_mod(
    mod_id: int | str,
    managed_path: str | Path,
    *,
    platform: str = "",
    library_root: str | Path | None = None,
    source_url: str = "",
    db=None,
) -> ModRefreshResult:
    """
    Detail refresh entry: local reconcile always; official provider at most once
    per mod lifetime (until first successful sync).
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    mid = str(mod_id or "").strip()
    folder = Path(managed_path)
    plat = normalize_platform(platform)

    local = reconcile_local_state(
        mid, folder, library_root=library_root, db=database
    )

    if not _should_attempt_official_sync(database, mid):
        logger.info(
            "[refresh] mod_id=%s official metadata already synced; network refresh skipped",
            mid,
        )
        msg = "已刷新本地状态"
        if not local.folder_present:
            msg = "已刷新本地状态（Mod 目录缺失）"
        return ModRefreshResult(
            mod_id=mid,
            success=True,
            local=local,
            official_attempted=False,
            official_synced=True,
            message=msg,
        )

    logger.info(
        "[refresh] mod_id=%s official metadata not synced; attempting initial provider sync",
        mid,
    )

    provider_result: MetadataRefreshResult | None = None
    try:
        if plat == PLATFORM_STEAM:
            from services.metadata_refresh import refresh_steam_mod_metadata

            provider_result = refresh_steam_mod_metadata(
                mid,
                folder,
                library_root=library_root,
                force=True,
                allow_official_sync=True,
                db=database,
            )
        elif plat == PLATFORM_MODIO:
            from services.modio_metadata_refresh import refresh_modio_mod_metadata

            provider_result = refresh_modio_mod_metadata(
                mid,
                folder,
                library_root=library_root,
                source_url=source_url,
                allow_official_sync=True,
                db=database,
            )
        else:
            # Nexus / GitHub / other — local only; mark synced if folder exists
            if local.folder_present:
                database.set_official_metadata_synced(mid, True)
            return ModRefreshResult(
                mod_id=mid,
                success=True,
                local=local,
                official_attempted=False,
                official_synced=database.is_official_metadata_synced(mid),
                message="已刷新本地状态",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[refresh] mod_id=%s official metadata sync failed; remains unsynced",
            mid,
        )
        database.set_official_metadata_synced(mid, False)
        return ModRefreshResult(
            mod_id=mid,
            success=True,
            local=local,
            official_attempted=True,
            official_success=False,
            official_synced=False,
            error=str(exc),
            message="本地状态已刷新，官方信息同步失败",
        )

    assert provider_result is not None
    if provider_result.success and not provider_result.skipped:
        database.set_official_metadata_synced(mid, True)
        logger.info(
            "[refresh] mod_id=%s official metadata sync succeeded; marked synced",
            mid,
        )
        # Reconcile again after official writes (.info / rename / cover)
        final_path = provider_result.managed_path or folder
        local = reconcile_local_state(
            mid, final_path, library_root=library_root, db=database
        )
        return ModRefreshResult(
            mod_id=mid,
            success=True,
            local=local,
            official_attempted=True,
            official_success=True,
            official_synced=True,
            provider=provider_result,
            message="已刷新本地状态，并同步官方信息",
        )

    database.set_official_metadata_synced(mid, False)
    logger.warning(
        "[refresh] mod_id=%s official metadata sync failed; remains unsynced error=%s",
        mid,
        provider_result.error,
    )
    return ModRefreshResult(
        mod_id=mid,
        success=True,
        local=local,
        official_attempted=True,
        official_success=False,
        official_synced=False,
        provider=provider_result,
        error=provider_result.error,
        message="本地状态已刷新，官方信息同步失败",
    )
