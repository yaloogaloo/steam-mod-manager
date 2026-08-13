"""Route offline page updates to the correct platform provider."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
)
from core.paths import default_mod_library
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.github import GithubOfflineProvider
from services.offline.modio import ModioOfflineProvider
from services.offline.nexus_manual import NexusManualOfflineProvider
from services.offline.steam import SteamOfflineProvider


class OfflineManager:
    """
    Single entry point for offline page refresh.

    UI must call this manager — never instantiate platform providers directly.
    """

    def __init__(
        self,
        *,
        db: DatabaseManager | None = None,
        library_root: str | Path | None = None,
        providers: Sequence[OfflineProvider] | None = None,
    ) -> None:
        self._db = db
        self.library_root = Path(library_root) if library_root else default_mod_library()
        self._providers: list[OfflineProvider] = list(
            providers
            if providers is not None
            else (
                SteamOfflineProvider(),
                NexusManualOfflineProvider(),
                GithubOfflineProvider(),
                ModioOfflineProvider(),
            )
        )

    @property
    def db(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def providers(self) -> Iterable[OfflineProvider]:
        return tuple(self._providers)

    def get_provider_for_platform(self, platform: str) -> OfflineProvider:
        plat = normalize_platform(platform)
        probe = type("Probe", (), {"platform": plat})()
        for provider in self._providers:
            if provider.can_handle(probe):
                return provider
        raise ValueError(f"No offline provider for platform={plat!r}")

    def get_provider_for_mod(self, mod_id: str | int) -> OfflineProvider:
        info = self.db.get_mod_display_info(mod_id)
        if info is None:
            raise ValueError(f"Mod not found: {mod_id}")
        return self.get_provider_for_platform(info.platform)

    def import_mod_offline_html(
        self,
        mod_id: str | int,
        html_path: str | Path,
        *,
        managed_path: str | Path | None = None,
        metadata=None,
        platform: str | None = None,
        clean: bool = True,
    ) -> OfflineUpdateResult:
        """Import a local HTML file as the Nexus offline snapshot."""
        del metadata
        mid = str(mod_id).strip()
        info = self.db.get_mod_display_info(mid)
        plat = normalize_platform(
            platform
            if platform is not None
            else (info.platform if info is not None else PLATFORM_NEXUS)
        )
        provider = self.get_provider_for_platform(plat)
        import_fn = getattr(provider, "import_offline_page", None)
        if not callable(import_fn):
            raise TypeError(
                f"Provider {provider.get_provider_name()!r} does not support HTML import"
            )
        return import_fn(
            mid,
            html_path,
            managed_path=managed_path,
            library_root=self.library_root,
            clean=clean,
        )

    def update_mod_offline(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        metadata=None,
        platform: str | None = None,
    ) -> OfflineUpdateResult:
        """
        Refresh ``.info/index.html`` using the provider for this Mod's platform.

        Does not run on library open / scan — call only from user actions
        (detail panel refresh) or explicit sync flows.
        """
        mid = str(mod_id).strip()
        info = self.db.get_mod_display_info(mid)
        plat = normalize_platform(
            platform
            if platform is not None
            else (info.platform if info is not None else PLATFORM_STEAM)
        )
        if info is None and plat != PLATFORM_STEAM:
            # Steam can still archive from filesystem metadata alone.
            if plat in (PLATFORM_NEXUS, PLATFORM_GITHUB, PLATFORM_MODIO):
                raise ValueError(f"Mod not found: {mid}")

        provider = self.get_provider_for_platform(plat)
        result = provider.update_offline_page(
            mid,
            managed_path=managed_path,
            library_root=self.library_root,
            metadata=metadata,
        )
        try:
            from services.metadata_backup_sync import sync_after_metadata_change

            dest = Path(managed_path) if managed_path else None
            if dest is None:
                index = getattr(result, "index_path", None)
                if index is not None:
                    idx = Path(index)
                    # .info/offline/index.html → mod root; .info/index.html → mod root
                    if idx.parent.name == "offline":
                        dest = idx.parent.parent.parent
                    else:
                        dest = idx.parent.parent
            if dest is not None:
                sync_after_metadata_change(mid, dest, "offline_change")
        except Exception:  # noqa: BLE001
            pass
        return result


def attach_nexus_offline_page(
    mod_id: str | int,
    page_path: str | Path,
    *,
    managed_path: str | Path | None = None,
    library_root: str | Path | None = None,
    clean: bool = True,
) -> OfflineUpdateResult:
    """
    Shared entry for Nexus offline page attach (HTML or MHTML).

    Used by Mod Import (optional offline page) and Detail Panel「导入离线页面」.
    Routes through :class:`NexusManualOfflineProvider` only.

    *clean* (default True) runs Nexus MHTML Offline Snapshot Cleaner when the
    source is ``.mhtml`` / ``.mht``.
    """
    result = OfflineManager(library_root=library_root).import_mod_offline_html(
        mod_id,
        page_path,
        managed_path=managed_path,
        platform=PLATFORM_NEXUS,
        clean=clean,
    )
    try:
        from services.metadata_backup_sync import sync_after_metadata_change

        dest = Path(managed_path) if managed_path else None
        if dest is None:
            index = getattr(result, "index_path", None)
            if index is not None:
                idx = Path(index)
                dest = idx.parent.parent.parent if idx.parent.name == "offline" else idx.parent.parent
        if dest is not None:
            sync_after_metadata_change(mod_id, dest, "offline_change")
    except Exception:  # noqa: BLE001
        pass
    return result


# Backward-compatible alias.
attach_nexus_offline_html = attach_nexus_offline_page
