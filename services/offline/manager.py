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
        if info is not None:
            plat = normalize_platform(info.platform)
        else:
            plat = normalize_platform(
                platform if platform is not None else PLATFORM_STEAM
            )
        if info is None and plat != PLATFORM_STEAM:
            # Steam can still archive from filesystem metadata alone.
            if plat in (PLATFORM_NEXUS, PLATFORM_GITHUB, PLATFORM_MODIO):
                raise ValueError(f"Mod not found: {mid}")

        if managed_path is not None and mid.isdigit():
            from services.path_lifecycle import resolve_managed_folder

            resolved = resolve_managed_folder(
                mid,
                hint_path=managed_path,
                db=self.db,
            )
            if resolved.path is not None and resolved.path.is_dir():
                managed_path = resolved.path
            else:
                healed = resolve_managed_folder(mid, db=self.db)
                if healed.path is not None and healed.path.is_dir():
                    managed_path = healed.path

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

    After the HTML is saved, invokes the Nexus offline metadata scraper to fill
    any missing metadata fields.  This is the ONLY legitimate call site for the
    scraper — refresh_mod / reconcile / show_mod MUST NOT call it.
    """
    result = OfflineManager(library_root=library_root).import_mod_offline_html(
        mod_id,
        page_path,
        managed_path=managed_path,
        platform=PLATFORM_NEXUS,
        clean=clean,
    )

    # Resolve the managed Mod folder path.
    dest: Path | None = Path(managed_path) if managed_path else None
    if dest is None:
        index = getattr(result, "index_path", None)
        if index is not None:
            idx = Path(index)
            dest = (
                idx.parent.parent.parent
                if idx.parent.name == "offline"
                else idx.parent.parent
            )

    # ------------------------------------------------------------------
    # Nexus Offline Metadata Scraper
    # Trigger boundary: OFFLINE_HTML_IMPORT only.
    # MUST NOT be moved to refresh_mod / reconcile / sync / show_mod.
    # ------------------------------------------------------------------
    from core.mod_platform import OFFLINE_STATUS_ARCHIVED

    if getattr(result, "status", None) == OFFLINE_STATUS_ARCHIVED and dest is not None:
        _apply_nexus_offline_metadata(mod_id, dest)

    try:
        from services.metadata_backup_sync import sync_after_metadata_change

        if dest is not None:
            sync_after_metadata_change(mod_id, dest, "offline_change")
    except Exception:  # noqa: BLE001
        pass
    return result


def _apply_nexus_offline_metadata(
    mod_id: str | int,
    managed_path: Path,
) -> None:
    """Parse the saved offline HTML and fill any missing Mod metadata.

    Failures are caught and logged as warnings — the offline HTML import must
    succeed regardless of scraper errors.

    This helper is intentionally private and called ONLY from
    ``attach_nexus_offline_page``.  No other code path may call it.
    """
    import logging as _logging

    _logger = _logging.getLogger(__name__)

    from services.offline.nexus_html_parser import (
        NexusOfflineCandidates,
        apply_nexus_offline_candidates,
        parse_nexus_offline_html,
    )

    index_path = managed_path / ".info" / "offline" / "index.html"
    if not index_path.is_file():
        return

    try:
        candidates: NexusOfflineCandidates = parse_nexus_offline_html(index_path)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[NEXUS_SCRAPER] parse_nexus_offline_html failed: %s", exc)
        return

    if not candidates.any_useful():
        return

    try:
        apply_nexus_offline_candidates(mod_id, managed_path, candidates)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "[NEXUS_SCRAPER] apply_nexus_offline_candidates failed: %s", exc
        )


# Backward-compatible alias.
attach_nexus_offline_html = attach_nexus_offline_page
