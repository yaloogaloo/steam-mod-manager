"""Nexus offline pages via user-imported browser HTML / MHTML (no remote scrape)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_MANUAL_IMPORT,
    normalize_platform,
)
from core.paths import default_mod_library
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.manual_import import (
    HTML_SUFFIXES,
    SUPPORTED_OFFLINE_SUFFIXES,
    import_offline_snapshot,
    validate_offline_path,
)

logger = logging.getLogger(__name__)

OFFLINE_SUBDIR = "offline"
DEFAULT_INDEX_NAME = "index.html"

_IMPORT_HINT = (
    "Nexus 离线页面需手动导入：在浏览器中打开 Mod 页 → 另存为网页（HTML 或 MHTML）→ "
    "在详情页或导入对话框选择离线页面文件。"
)


def validate_html_path(html_path: str | Path) -> Path:
    """Validate a manual offline snapshot path (HTML or MHTML)."""
    return validate_offline_path(html_path)


def store_snapshot(
    html_path: Path,
    output_dir: Path | str,
    *,
    source_url: str = "",
    title: str = "",
    clean: bool = True,
) -> tuple[Path, int]:
    """
    Import *html_path* (HTML or MHTML) into ``output_dir/index.html`` (+ assets).

    *clean* enables Nexus MHTML layout cleaning (default True).

    Returns ``(index_path, asset_count)``.
    """
    index, asset_count, _fmt = import_offline_snapshot(
        html_path,
        output_dir,
        source_url=source_url,
        title=title,
        clean=clean,
    )
    return index, asset_count


class NexusManualOfflineProvider(OfflineProvider):
    """
    Nexus offline pages from user-saved browser HTML / MHTML.

    Does **not** fetch Nexus remotely (no requests / Playwright / CDP).
    """

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_NEXUS

    def get_provider_name(self) -> str:
        return PROVIDER_NEXUS_MANUAL_IMPORT

    def update_offline_page(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        metadata: Any | None = None,
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        """Auto-download is disabled — callers must use ``import_offline_page``."""
        del managed_path, library_root, metadata, force_refresh
        mid = str(mod_id).strip()
        get_db().update_mod_offline_status(
            mid,
            status=OFFLINE_STATUS_FAILED,
            provider=self.get_provider_name(),
        )
        raise RuntimeError(_IMPORT_HINT)

    def import_offline_page(
        self,
        mod_id: int | str,
        html_path: str | Path,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        clean: bool = True,
    ) -> OfflineUpdateResult:
        """Import a user-saved Nexus HTML/MHTML snapshot into ``.info/offline/``."""
        mid = str(mod_id).strip()
        root = Path(library_root) if library_root else default_mod_library()
        path = Path(managed_path) if managed_path else find_managed_mod_path(root, mid)
        if path is None:
            raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")

        info = get_db().get_mod_display_info(mid)
        if info is None:
            raise ValueError(f"Mod not found in database: {mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        output_dir = info_dir / OFFLINE_SUBDIR
        title = (info.display_name or info.steam_name or "").strip()
        source_url = (info.source_url or "").strip()

        try:
            index, _assets = store_snapshot(
                Path(html_path),
                output_dir,
                source_url=source_url,
                title=title,
                clean=clean,
            )
        except Exception:
            get_db().update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise

        get_db().update_mod_offline_status(
            mid,
            status=OFFLINE_STATUS_ARCHIVED,
            provider=self.get_provider_name(),
        )
        logger.info(
            "[NEXUS_OFFLINE] stage=manual_import status=success mod_id=%s path=%s",
            mid,
            index,
        )
        return OfflineUpdateResult(
            mod_id=mid,
            index_path=index,
            status=OFFLINE_STATUS_ARCHIVED,
            provider=self.get_provider_name(),
            error="",
        )


# Backward-compatible alias used by OfflineManager / older imports.
NexusOfflineProvider = NexusManualOfflineProvider

__all__ = [
    "DEFAULT_INDEX_NAME",
    "HTML_SUFFIXES",
    "OFFLINE_SUBDIR",
    "SUPPORTED_OFFLINE_SUFFIXES",
    "NexusManualOfflineProvider",
    "NexusOfflineProvider",
    "store_snapshot",
    "validate_html_path",
]
