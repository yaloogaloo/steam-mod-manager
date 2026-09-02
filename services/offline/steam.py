"""Steam Workshop offline provider — wraps ``services.archive`` unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import PLATFORM_STEAM, normalize_platform
from core.models import ModMetadata
from core.paths import default_mod_library
from services.archive import (
    ARCHIVE_OUTCOME_FAILED,
    ARCHIVE_OUTCOME_RATE_LIMITED,
    ARCHIVE_OUTCOME_SKIPPED,
    ARCHIVE_OUTCOME_SUCCESS,
    ArchiveEnsureResult,
    OfflinePageArchiver,
    is_stub_offline_page,
    is_valid_steam_workshop_page,
)
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import (
    OFFLINE_OUTCOME_FAILED,
    OFFLINE_OUTCOME_RATE_LIMITED,
    OFFLINE_OUTCOME_SKIPPED,
    OFFLINE_OUTCOME_SUCCESS,
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OfflineProvider,
    OfflineUpdateResult,
    PROVIDER_STEAM_ARCHIVE,
)


def _coerce_ensure(value: Any) -> ArchiveEnsureResult:
    if isinstance(value, ArchiveEnsureResult):
        return value
    path = Path(value)
    if is_valid_steam_workshop_page(path):
        return ArchiveEnsureResult(
            path=path,
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )
    if is_stub_offline_page(path):
        return ArchiveEnsureResult(
            path=path,
            outcome=ARCHIVE_OUTCOME_FAILED,
            http_performed=True,
            write_performed=True,
            error="Steam offline page is a stub (archive incomplete)",
        )
    return ArchiveEnsureResult(
        path=path,
        outcome=ARCHIVE_OUTCOME_FAILED,
        http_performed=True,
        write_performed=True,
        error="Steam offline page invalid",
    )


class SteamOfflineProvider(OfflineProvider):
    """Delegates to ``OfflinePageArchiver.ensure_offline_page``."""

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_STEAM

    def get_provider_name(self) -> str:
        return PROVIDER_STEAM_ARCHIVE

    def update_offline_page(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        metadata: Any | None = None,
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        mid = str(mod_id).strip()
        force = bool(force_refresh)
        root = Path(library_root) if library_root else default_mod_library()
        path = Path(managed_path) if managed_path else find_managed_mod_path(root, mid)
        if path is None:
            raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        meta = metadata
        if meta is None:
            meta = mgr.load_metadata(path)
        if meta is None:
            info = get_db().get_mod_display_info(mid)
            meta = ModMetadata(
                published_file_id=mid,
                title=(info.display_name if info else "") or mid,
                app_id=int(info.app_id) if info else 0,
            )

        workshop_id = mid
        if isinstance(meta, ModMetadata) and meta.published_file_id:
            workshop_id = str(meta.published_file_id).strip() or mid

        error = ""
        try:
            with OfflinePageArchiver() as archiver:
                ensured = _coerce_ensure(
                    archiver.ensure_offline_page(
                        info_dir,
                        workshop_id,
                        metadata=meta if isinstance(meta, ModMetadata) else None,
                        force_refresh=force,
                    )
                )
            index = ensured.path
            if ensured.outcome == ARCHIVE_OUTCOME_SKIPPED:
                # Keep prior archived status; do not bump offline_updated_at.
                return OfflineUpdateResult(
                    mod_id=mid,
                    index_path=Path(index),
                    status=OFFLINE_STATUS_ARCHIVED,
                    provider=self.get_provider_name(),
                    error="",
                    outcome=OFFLINE_OUTCOME_SKIPPED,
                    skip_reason=ensured.skip_reason or "cache_hit",
                    force_refresh=force,
                    http_performed=False,
                    write_performed=False,
                )

            if ensured.outcome == ARCHIVE_OUTCOME_RATE_LIMITED:
                status = (
                    OFFLINE_STATUS_ARCHIVED
                    if is_valid_steam_workshop_page(index)
                    else OFFLINE_STATUS_FAILED
                )
                error = ensured.error or "Steam rate limited"
                get_db().update_mod_offline_status(
                    mid,
                    status=status,
                    provider=self.get_provider_name(),
                )
                return OfflineUpdateResult(
                    mod_id=mid,
                    index_path=Path(index),
                    status=status,
                    provider=self.get_provider_name(),
                    error=error,
                    outcome=OFFLINE_OUTCOME_RATE_LIMITED,
                    force_refresh=force,
                    http_performed=ensured.http_performed,
                    write_performed=ensured.write_performed,
                )

            if ensured.outcome == ARCHIVE_OUTCOME_FAILED or is_stub_offline_page(index):
                status = OFFLINE_STATUS_FAILED
                error = (
                    ensured.error
                    or "Steam offline page is a stub (archive incomplete)"
                )
            elif ensured.outcome == ARCHIVE_OUTCOME_SUCCESS and (
                is_valid_steam_workshop_page(index)
            ):
                status = OFFLINE_STATUS_ARCHIVED
            else:
                status = OFFLINE_STATUS_FAILED
                error = ensured.error or "Steam offline archive did not succeed"
        except Exception as exc:  # noqa: BLE001
            status = OFFLINE_STATUS_FAILED
            error = str(exc)
            index = info_dir / "index.html"
            get_db().update_mod_offline_status(
                mid,
                status=status,
                provider=self.get_provider_name(),
            )
            if not index.is_file():
                raise
            return OfflineUpdateResult(
                mod_id=mid,
                index_path=Path(index),
                status=status,
                provider=self.get_provider_name(),
                error=error,
                outcome=OFFLINE_OUTCOME_FAILED,
                force_refresh=force,
                http_performed=False,
                write_performed=False,
            )

        get_db().update_mod_offline_status(
            mid,
            status=status,
            provider=self.get_provider_name(),
        )
        outcome = (
            OFFLINE_OUTCOME_SUCCESS
            if status == OFFLINE_STATUS_ARCHIVED
            else OFFLINE_OUTCOME_FAILED
        )
        return OfflineUpdateResult(
            mod_id=mid,
            index_path=Path(index),
            status=status,
            provider=self.get_provider_name(),
            error=error,
            outcome=outcome,
            force_refresh=force,
            http_performed=ensured.http_performed,
            write_performed=ensured.write_performed,
        )
