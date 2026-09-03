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
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        """
        Refresh ``.info/index.html`` using the provider for this Mod's platform.

        Does not run on library open / scan — call only from user actions
        (detail panel refresh) or explicit sync flows.

        *force_refresh*: when True (manual UI save), providers must not treat an
        existing page as a successful refresh (Steam: no cache-hit skip).
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
            force_refresh=bool(force_refresh),
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
        parsed_title = _apply_nexus_offline_metadata(mod_id, dest)
        dest = _maybe_rename_empty_mod_folder_to_parsed_title(
            mod_id, dest, parsed_title
        )

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
) -> str:
    """Parse the saved offline HTML and fill any missing Mod metadata.

    Failures are caught and logged as warnings — the offline HTML import must
    succeed regardless of scraper errors.

    This helper is intentionally private and called ONLY from
    ``attach_nexus_offline_page``.  No other code path may call it.

    Returns the parsed Mod title (empty when parse/apply produced none).
    Directory naming is *not* done here: a valid title is applied to DB /
    sidecar first, then ``_maybe_rename_empty_mod_folder_to_parsed_title``
    uses path_lifecycle so filesystem / DB / sidecar / identity stay aligned.
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
        return ""

    try:
        candidates: NexusOfflineCandidates = parse_nexus_offline_html(index_path)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[NEXUS_SCRAPER] parse_nexus_offline_html failed: %s", exc)
        return ""

    if not candidates.any_useful():
        return ""

    try:
        apply_nexus_offline_candidates(mod_id, managed_path, candidates)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "[NEXUS_SCRAPER] apply_nexus_offline_candidates failed: %s", exc
        )
        return str(getattr(candidates, "title", "") or "").strip()

    return str(getattr(candidates, "title", "") or "").strip()


def _folders_are_same_directory(source: Path, target: Path) -> bool:
    try:
        if source.resolve() == target.resolve():
            return True
    except OSError:
        pass
    try:
        return source.is_dir() and target.exists() and source.samefile(target)
    except OSError:
        return False


def _existing_target_identity_conflict(
    mod_id: str,
    target: Path,
) -> str:
    """Describe why an existing target directory must not be overwritten.

    Never rmtree / merge / silently replace. Same-identity and unknown-identity
    are both conflicts: keep both trees and report.
    """
    from services.file_ops import read_info_metadata_dict
    from services.mod_identity import resolve_existing_mod_id

    meta = read_info_metadata_dict(target) or {}
    if not meta:
        return (
            "identity/path conflict: 目标目录已存在且无法确认身份，"
            "已保留双方数据，未覆盖"
        )
    pub = str(meta.get("published_file_id") or "").strip()
    if pub and pub == str(mod_id):
        return (
            "identity/path conflict: 目标目录已存在且属于同一 Mod，"
            "已保留双方数据，未覆盖或合并"
        )
    try:
        resolved = resolve_existing_mod_id(meta)
    except Exception:  # noqa: BLE001
        resolved = ""
    if resolved and str(resolved) == str(mod_id):
        return (
            "identity/path conflict: 目标目录已存在且属于同一 Mod，"
            "已保留双方数据，未覆盖或合并"
        )
    return (
        "identity/path conflict: 目标目录已存在且身份不同或不明确，"
        "已保留双方数据，未覆盖"
    )


def _maybe_rename_empty_mod_folder_to_parsed_title(
    mod_id: str | int,
    managed_path: Path,
    parsed_title: str,
) -> Path:
    """Rename ``Empty Mod <random>`` to the parsed Mod title via path_lifecycle.

    Why this exists (do not delete in a future import-flow refactor):

    Import may create a temp ``Empty Mod <hex>`` folder before Offline HTML
    metadata is fully applied. Once a valid parsed title exists, that title
    is the canonical directory name. Isolated ``os.rename`` / ``shutil.move``
    would desync DB path, sidecar, and identity — so rename +
    ``record_filesystem_rename`` must stay together.

    ``Empty Mod <random>`` remains valid only when no usable title exists.
    An existing conflicting directory is never deleted or overwritten.
    """
    import logging as _logging

    _logger = _logging.getLogger(__name__)
    from core.models import is_unknown_mod_title
    from core.sanitize import sanitize_folder_name
    from services.identity_service import is_empty_mod_placeholder
    from services.path_lifecycle import record_filesystem_rename

    folder = Path(managed_path)
    title = str(parsed_title or "").strip()
    if not folder.is_dir():
        return folder
    if not is_empty_mod_placeholder(folder.name):
        return folder
    if not title or is_empty_mod_placeholder(title) or is_unknown_mod_title(title):
        return folder

    desired = sanitize_folder_name(title, fallback="")
    if not desired or is_empty_mod_placeholder(desired) or desired == folder.name:
        return folder

    target = folder.parent / desired
    if _folders_are_same_directory(folder, target):
        return folder
    if target.exists():
        conflict = _existing_target_identity_conflict(str(mod_id), target)
        _logger.error(
            "[NEXUS_OFFLINE_IMPORT] %s source=%s target=%s",
            conflict,
            folder,
            target,
        )
        return folder

    try:
        from services.directory_move import rename_directory_or_fallback
        from services.metadata_refresh import (
            prepare_managed_folder_for_rename,
            safe_directory_rename,
        )

        prepare_managed_folder_for_rename(folder)

        def _rename_once(src: Path, dst: Path) -> Path:
            return safe_directory_rename(src, dst, attempts=1)

        new_path = rename_directory_or_fallback(
            folder, target, rename_once=_rename_once
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "[NEXUS_OFFLINE_IMPORT] canonical rename failed source=%s target=%s: %s",
            folder,
            target,
            exc,
        )
        return folder

    commit = record_filesystem_rename(
        mod_id,
        folder,
        new_path,
        reason="offline_html_import",
    )
    if not commit.success:
        _logger.warning(
            "[NEXUS_OFFLINE_IMPORT] path_lifecycle commit failed stage=%s error=%s "
            "source=%s target=%s",
            commit.stage,
            commit.error,
            folder,
            new_path,
        )
        return Path(commit.new_path) if commit.new_path is not None else new_path
    return Path(commit.new_path) if commit.new_path is not None else new_path


# Backward-compatible alias.
attach_nexus_offline_html = attach_nexus_offline_page
