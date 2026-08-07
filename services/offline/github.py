"""GitHub offline provider — real webpage snapshot (not API / local generator)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_GITHUB,
    PROVIDER_GITHUB_SNAPSHOT,
    normalize_platform,
)
from core.paths import default_mod_library
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.snapshot import WebSnapshotDownloader

OFFLINE_SUBDIR = "offline"


class GithubOfflineProvider(OfflineProvider):
    """Snapshot repository ``source_url`` into ``.info/offline/``."""

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_GITHUB

    def get_provider_name(self) -> str:
        return PROVIDER_GITHUB_SNAPSHOT

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

        info = get_db().get_mod_display_info(mid)
        if info is None:
            raise ValueError(f"Mod not found in database: {mid}")

        source_url = (info.source_url or "").strip()
        if not source_url:
            # Fall back to canonical GitHub URL from external_id (owner/repo).
            repo = (info.external_id or "").strip().strip("/")
            if repo.count("/") == 1:
                source_url = f"https://github.com/{repo}"

        if not source_url:
            get_db().update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise ValueError(f"GitHub Mod has no source_url: mod_id={mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        output_dir = info_dir / OFFLINE_SUBDIR

        with WebSnapshotDownloader() as downloader:
            snap = downloader.download(source_url, output_dir)

        if not snap.success or not snap.html_path.is_file():
            error = snap.error or "Snapshot download failed"
            get_db().update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise RuntimeError(error)

        get_db().update_mod_offline_status(
            mid,
            status=OFFLINE_STATUS_ARCHIVED,
            provider=self.get_provider_name(),
        )
        return OfflineUpdateResult(
            mod_id=mid,
            index_path=snap.html_path,
            status=OFFLINE_STATUS_ARCHIVED,
            provider=self.get_provider_name(),
            error="",
        )
