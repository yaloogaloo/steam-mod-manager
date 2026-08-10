"""GitHub offline provider — Playwright page.content() only."""

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
from services.offline.github_browser_snapshot import (
    GitHubBrowserSnapshot,
    run_github_browser_snapshot,
)

OFFLINE_SUBDIR = "offline"


class GithubOfflineProvider(OfflineProvider):
    """
    Build ``.info/offline/`` for GitHub repositories.

    Sole path::

        Playwright Chromium → page.content() → index.html

    No requests HTML download, no asset crawler, no summary / fallback pages.
    """

    def __init__(
        self,
        *,
        browser_snapshot: GitHubBrowserSnapshot | None = None,
        capture_func: Any | None = None,
        # Test injectables: duck-typed ``.snapshot(url, output_dir)``.
        layout_provider: Any | None = None,
        snapshot_provider: Any | None = None,
        downloader: Any | None = None,
        readable_provider: Any | None = None,
    ) -> None:
        del downloader, readable_provider
        self._browser_snapshot = browser_snapshot
        self._capture_func = capture_func
        self._injected = layout_provider or snapshot_provider

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
        del metadata
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
        title = (info.display_name or info.steam_name or "").strip()

        if self._injected is not None:
            snap = self._injected.snapshot(source_url, output_dir)
            html_path = Path(getattr(snap, "html_path", output_dir / "index.html"))
            success = bool(getattr(snap, "success", True)) and html_path.is_file()
            used_fallback = bool(getattr(snap, "used_fallback", False))
            error = getattr(snap, "error", None) or getattr(snap, "failure_reason", None)
            # Injected path must not invent fallback pages either.
            if success and not used_fallback:
                status = OFFLINE_STATUS_ARCHIVED
            else:
                status = OFFLINE_STATUS_FAILED
            get_db().update_mod_offline_status(
                mid, status=status, provider=self.get_provider_name()
            )
            if not html_path.is_file():
                raise RuntimeError(str(error or "GitHub Playwright snapshot failed"))
            if status == OFFLINE_STATUS_FAILED and used_fallback:
                raise RuntimeError(
                    str(error or "GitHub snapshot produced a forbidden fallback page")
                )
            return OfflineUpdateResult(
                mod_id=mid,
                index_path=html_path,
                status=status,
                provider=self.get_provider_name(),
                error=str(error or "") if status == OFFLINE_STATUS_FAILED else "",
            )

        browser = self._browser_snapshot
        if browser is None:
            browser = GitHubBrowserSnapshot(
                capture_func=self._capture_func,
                title=title,
            )

        result, status = run_github_browser_snapshot(
            source_url=source_url,
            output_dir=output_dir,
            snapshot=browser,
            title=title,
        )

        get_db().update_mod_offline_status(
            mid,
            status=status,
            provider=self.get_provider_name(),
        )

        if not result.success or not result.html_path.is_file():
            raise RuntimeError(result.error or "GitHub Playwright snapshot failed")

        return OfflineUpdateResult(
            mod_id=mid,
            index_path=result.html_path,
            status=status,
            provider=self.get_provider_name(),
            error=result.error or "",
        )
