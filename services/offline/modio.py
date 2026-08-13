"""mod.io offline provider — Playwright SPA render + asset localization."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.db_manager import get_db
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_MODIO,
    PROVIDER_MODIO_ARCHIVE,
    normalize_platform,
)
from core.paths import default_mod_library
from services.archive import OfflinePageArchiver, normalize_page_url
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.modio_browser_snapshot import (
    CaptureFunc,
    ModioSnapshotError,
    capture_modio_page_content,
    strip_modio_cookie_banner,
)

OFFLINE_SUBDIR = "offline"


def _map_modio_error(exc: BaseException) -> str:
    """User-facing Chinese error for mod.io archive failures."""
    text = str(exc or "").strip()
    low = text.lower()
    if "网络连接失败" in text or "network" in low or "timed out" in low or "timeout" in low:
        return "网络连接失败"
    if "资源下载失败" in text:
        return "资源下载失败"
    if (
        "页面访问失败" in text
        or "unrendered" in low
        or "empty_html" in low
        or "http" in low
        or isinstance(exc, ModioSnapshotError)
    ):
        return "mod.io 页面访问失败"
    if text:
        if "访问" in text or "fetch" in low or "get" in low or "playwright" in low:
            return "mod.io 页面访问失败"
        return text
    return "mod.io 页面访问失败"


class ModioOfflineProvider(OfflineProvider):
    """
    Build ``.info/offline/`` for mod.io Mods.

    Path::

        source_url → normalize (strip #fragment)
        → Playwright Chromium (networkidle → page.content())
        → OfflinePageArchiver.archive_rendered_html
        → index.html + assets/ (shared rewrite / asset_cache)
    """

    def __init__(self, *, capture_func: CaptureFunc | Callable[[str], str] | None = None) -> None:
        self._capture_func = capture_func

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_MODIO

    def get_provider_name(self) -> str:
        return PROVIDER_MODIO_ARCHIVE

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

        source_url = normalize_page_url((info.source_url or "").strip())
        if not source_url:
            get_db().update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise ValueError(f"mod.io Mod has no source_url: mod_id={mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        output_dir = info_dir / OFFLINE_SUBDIR

        error = ""
        try:
            if self._capture_func is not None:
                html_text = str(self._capture_func(source_url) or "")
            else:
                html_text = capture_modio_page_content(source_url)

            # Strip cookie consent before asset rewrite / save (mod.io only).
            html_text = strip_modio_cookie_banner(html_text)

            # Localize CSS / images via shared Steam asset stack (cache + rewrite).
            with OfflinePageArchiver(steam_cookie="") as archiver:
                index = archiver.archive_rendered_html(html_text, source_url, output_dir)
            status = OFFLINE_STATUS_ARCHIVED

            # Persist preferred open path so UI does not fall back to Steam layout.
            try:
                meta = mgr.load_metadata(path)
                if meta is not None:
                    meta.offline_page_path = str(Path(index))
                    mgr.save_metadata(meta, path, sync_backup=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            status = OFFLINE_STATUS_FAILED
            error = _map_modio_error(exc)
            get_db().update_mod_offline_status(
                mid,
                status=status,
                provider=self.get_provider_name(),
            )
            raise RuntimeError(error) from exc

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
