"""mod.io offline provider — Playwright SPA render + asset localization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_MODIO,
    PROVIDER_MODIO_ARCHIVE,
    is_anno_1800_game,
    is_baldurs_gate_3_game,
    normalize_platform,
)
from core.paths import default_mod_library
from services.archive import OfflinePageArchiver, normalize_page_url
from services.file_ops import ModFileManager, read_info_metadata_dict
from services.modio_api import parse_modio_url
from services.offline.base import OfflineProvider, OfflineUpdateResult
from services.offline.modio_browser_snapshot import (
    CaptureFunc,
    ModioSnapshotError,
    capture_modio_page_content,
    strip_modio_cookie_banner,
)

logger = logging.getLogger(__name__)

OFFLINE_SUBDIR = "offline"


def _map_modio_error(exc: BaseException) -> str:
    """User-facing Chinese error for mod.io archive failures."""
    if isinstance(exc, ModioSnapshotError):
        return exc.user_message
    text = str(exc or "").strip()
    if text:
        return text
    return "离线页面保存失败"


def _is_modio_web_page_url(url: str) -> bool:
    """
    True when *url* is a public mod.io Mod page (``/g/<game>/m/<name_id>``).

    Rejects API hosts (``modapi.io``, ``api.mod.io``) and bare numeric ids.
    """
    text = normalize_page_url(str(url or "").strip())
    if not text or text.isdigit():
        return False
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if "modapi.io" in host or host.startswith("api.") or host == "api.mod.io":
        return False
    return parse_modio_url(text) is not None


def _modio_game_slug(*, app_id: int = 0, game_name: str = "") -> str:
    if is_baldurs_gate_3_game(game_name, app_id):
        return "baldursgate3"
    if is_anno_1800_game(game_name, app_id):
        return "anno-1800"
    return ""


def _reconstruct_modio_page_url(
    sidecar: dict[str, Any],
    *,
    app_id: int = 0,
    game_name: str = "",
) -> str:
    """Rebuild page URL from sidecar ``modio_name_id`` + known game slug."""
    name_id = str(sidecar.get("modio_name_id") or "").strip()
    if not name_id or name_id.isdigit():
        return ""
    for key in ("source_url", "url", "website"):
        parts = parse_modio_url(str(sidecar.get(key) or ""))
        if parts is not None:
            return parts.canonical_url
    slug = _modio_game_slug(app_id=app_id, game_name=game_name)
    if slug:
        return f"https://mod.io/g/{slug}/m/{name_id}"
    return ""


def resolve_modio_offline_page_url(
    mod_id: str | int,
    *,
    managed_path: str | Path | None = None,
    db: DatabaseManager | None = None,
) -> str:
    """
    Resolve the mod.io **web page** URL for offline capture.

    Priority: SQLite ``source_url`` → sidecar ``url`` / ``source_url`` →
    reconstruct from ``modio_name_id``. Never uses ``external_id``.
    """
    from services.path_lifecycle import resolve_managed_folder

    mid = str(mod_id).strip()
    database = db if db is not None else get_db()
    info = database.get_mod_display_info(mid)

    candidates: list[str] = []
    if info is not None:
        candidates.append(str(info.source_url or "").strip())

    resolved = resolve_managed_folder(
        mid,
        hint_path=managed_path,
        db=database,
    )
    folder = resolved.path
    sidecar: dict[str, Any] = {}
    if folder is not None and folder.is_dir():
        sidecar = read_info_metadata_dict(folder) or {}
        for key in ("source_url", "url", "website"):
            candidates.append(str(sidecar.get(key) or "").strip())

    for raw in candidates:
        url = normalize_page_url(raw)
        if _is_modio_web_page_url(url):
            return url

    if sidecar:
        rebuilt = _reconstruct_modio_page_url(
            sidecar,
            app_id=int(getattr(info, "app_id", 0) or 0) if info else 0,
            game_name=str(getattr(info, "game_name", "") or "") if info else "",
        )
        if _is_modio_web_page_url(rebuilt):
            logger.info(
                "mod.io offline URL reconstructed mod_id=%s url=%s",
                mid,
                rebuilt,
            )
            return rebuilt

    raise ValueError(
        f"mod.io Mod has no valid page URL (source_url required): mod_id={mid}"
    )


def resolve_modio_offline_folder(
    mod_id: str | int,
    *,
    managed_path: str | Path | None = None,
    library_root: str | Path | None = None,
    db: DatabaseManager | None = None,
) -> Path:
    """Resolve live managed folder; heal stale UI hints via DB ``last_known_path``."""
    from services.importers.materialize import find_managed_mod_path
    from services.path_lifecycle import resolve_managed_folder

    mid = str(mod_id).strip()
    database = db if db is not None else get_db()
    root = Path(library_root) if library_root else default_mod_library()

    resolved = resolve_managed_folder(mid, hint_path=managed_path, db=database)
    folder = resolved.path
    if folder is not None and folder.is_dir():
        return folder

    healed = resolve_managed_folder(mid, db=database)
    if healed.path is not None and healed.path.is_dir():
        logger.info(
            "mod.io offline path healed mod_id=%s from=%s path=%s",
            mid,
            healed.resolved_from,
            healed.path,
        )
        return healed.path

    if managed_path is None:
        found = find_managed_mod_path(root, mid)
        if found is not None and found.is_dir():
            return found

    if managed_path is not None:
        hint = Path(managed_path).expanduser()
        if hint.is_dir():
            return hint.resolve()

    raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")


class ModioOfflineProvider(OfflineProvider):
    """
    Build ``.info/offline/`` for mod.io Mods.

    Path::

        source_url → normalize (strip #fragment)
        → Playwright Chromium (domcontentloaded → page.content())
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
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        del metadata, force_refresh
        mid = str(mod_id).strip()
        database = get_db()
        root = Path(library_root) if library_root else default_mod_library()

        path = resolve_modio_offline_folder(
            mid,
            managed_path=managed_path,
            library_root=root,
            db=database,
        )

        info = database.get_mod_display_info(mid)
        if info is None:
            raise ValueError(f"Mod not found in database: {mod_id}")

        try:
            source_url = resolve_modio_offline_page_url(
                mid,
                managed_path=path,
                db=database,
            )
        except ValueError as exc:
            database.update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise exc

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
            if isinstance(exc, ModioSnapshotError):
                logger.warning(
                    "[MODIO_OFFLINE] snapshot_failed kind=%s detail=%s",
                    exc.kind.value,
                    exc.detail or exc,
                )
            database.update_mod_offline_status(
                mid,
                status=status,
                provider=self.get_provider_name(),
            )
            raise RuntimeError(error) from exc

        database.update_mod_offline_status(
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
