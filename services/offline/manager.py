"""Route offline page updates to the correct platform provider."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
)
from core.paths import default_mod_library
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.github import GithubOfflineProvider
from services.offline.nexus import NexusOfflineProvider
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
                NexusOfflineProvider(),
                GithubOfflineProvider(),
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
            if plat in (PLATFORM_NEXUS, PLATFORM_GITHUB):
                raise ValueError(f"Mod not found: {mid}")

        provider = self.get_provider_for_platform(plat)
        return provider.update_offline_page(
            mid,
            managed_path=managed_path,
            library_root=self.library_root,
            metadata=metadata,
        )
