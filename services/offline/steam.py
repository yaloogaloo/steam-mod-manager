"""Steam Workshop offline provider — wraps ``services.archive`` unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import PLATFORM_STEAM, normalize_platform
from core.models import ModMetadata
from core.paths import default_mod_library
from services.archive import OfflinePageArchiver, is_stub_offline_page
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OfflineProvider,
    OfflineUpdateResult,
    PROVIDER_STEAM_ARCHIVE,
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
    ) -> OfflineUpdateResult:
        mid = str(mod_id).strip()
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
                index = archiver.ensure_offline_page(
                    info_dir,
                    workshop_id,
                    metadata=meta if isinstance(meta, ModMetadata) else None,
                )
            if is_stub_offline_page(index):
                status = OFFLINE_STATUS_FAILED
                error = "Steam offline page is a stub (archive incomplete)"
            else:
                status = OFFLINE_STATUS_ARCHIVED
        except Exception as exc:  # noqa: BLE001
            status = OFFLINE_STATUS_FAILED
            error = str(exc)
            index = info_dir / "index.html"
            if not index.is_file():
                get_db().update_mod_offline_status(
                    mid,
                    status=status,
                    provider=self.get_provider_name(),
                )
                raise

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
        )
