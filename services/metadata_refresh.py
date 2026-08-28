"""Steam Workshop metadata retry / refresh (manual + batch).

Isolated from Import Pipeline / deploy / offline archive.
Reuses ``SteamWorkshopClient.refresh_details`` and folder sanitize helpers.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core.models import ModMetadata, is_unknown_mod_title
from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
    silent_correct_nexus_workspace_id,
)
from core.sanitize import unique_destination
from core.steam_api import SteamWorkshopClient
from services.file_ops import (
    COVER_BASENAME,
    INFO_DIR_NAME,
    ModFileManager,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)

logger = logging.getLogger(__name__)

MetadataProgress = Callable[[int, int, str], None]

# Exponential backoff between rename attempts (0.5→1→2→4; 4 retries after first try).
DEFAULT_RENAME_BACKOFF_SEC: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

# Re-export for existing tests / callers.
__all__ = [
    "MetadataRefreshResult",
    "collect_directory_rename_diagnostics",
    "filter_steam_batch_entries",
    "format_directory_rename_error",
    "is_unknown_mod_title",
    "needs_metadata_refresh",
    "prepare_managed_folder_for_rename",
    "refresh_selected_mods_metadata",
    "refresh_steam_mod_metadata",
    "refresh_steam_mods_metadata",
    "rename_managed_folder_for_title",
    "safe_directory_rename",
]

@dataclass
class MetadataRefreshResult:
    """Outcome of one metadata refresh attempt."""

    mod_id: str
    success: bool
    skipped: bool = False
    renamed: bool = False
    managed_path: Path | None = None
    old_path: Path | None = None
    title: str = ""
    error: str = ""
    cover_path: str = ""
    message: str = ""


def needs_metadata_refresh(
    meta: ModMetadata | None,
    *,
    folder_name: str = "",
) -> bool:
    """
    Refresh only failed / unknown metadata.

    Skip when title is a real name and ``fetch_error`` is absent.
    """
    if meta is None:
        return True
    if str(meta.fetch_error or "").strip():
        return True
    mid = str(meta.published_file_id or "").strip()
    if is_unknown_mod_title(meta.title, published_file_id=mid):
        return True
    folder = str(folder_name or "").strip()
    if folder and is_unknown_mod_title(folder, published_file_id=mid):
        return True
    return False


def _persist_cleared_fetch_error(managed_path: Path) -> None:
    """Remove stale ``fetch_error`` from metadata.json after a successful refresh."""
    data = read_info_metadata_dict(managed_path)
    if not data or "fetch_error" not in data:
        return
    data.pop("fetch_error", None)
    info = Path(managed_path) / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    meta_file = info / "metadata.json"
    meta_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clear_placeholder_display_name(mod_id: str, managed_path: Path) -> None:
    """
    Drop stale ``Unknown_Mod_*`` user overrides so Steam ``title`` can surface.

    Refresh writes ``mods.title`` but never overwrites ``mods.display_name``.
    Placeholder overrides were previously stamped from sidecar restore and block
    the Detail header (which prefers display_name over steam title).
    """
    mid = str(mod_id or "").strip()
    folder = Path(managed_path)

    try:
        from core.db_manager import get_db
        from services.metadata_ownership import (
            FIELD_DISPLAY_NAME,
            user_has_override,
        )

        db = get_db()
        overrides = db.get_user_override_fields(mid)
        if user_has_override(overrides, FIELD_DISPLAY_NAME):
            return
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "skip placeholder clear (override check) for %s: %s", mid, exc
        )
        db = None  # type: ignore[assignment]

    # metadata.json
    data = read_info_metadata_dict(folder)
    if data:
        dn = str(data.get("display_name") or "").strip()
        if is_unknown_mod_title(dn, published_file_id=mid):
            data.pop("display_name", None)
            info = folder / INFO_DIR_NAME
            info.mkdir(parents=True, exist_ok=True)
            (info / "metadata.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # SQLite user override column
    try:
        from core.db_manager import get_db

        db = get_db()
        info = db.get_mod_display_info(mid)
        if info is not None and is_unknown_mod_title(
            info.user_display_name, published_file_id=mid
        ):
            db.update_mod_user_metadata(mid, {"display_name": ""})
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "clear placeholder display_name skipped for %s: %s", mid, exc
        )


def _is_access_denied(exc: BaseException) -> bool:
    """True for Windows WinError 5 / POSIX EACCES-style rename locks."""
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror == 5:
        return True
    errno = getattr(exc, "errno", None)
    # 13 = EACCES, 11 = EAGAIN (rare on rename)
    if errno in (13, 11):
        return True
    text = str(exc).lower()
    return "access is denied" in text or "winerror 5" in text


def format_directory_rename_error(
    exc: BaseException,
    *,
    source: Path | None = None,
    lock_summary: str = "",
) -> str:
    """Human-readable rename failure; keeps WinError 5 explicit."""
    if _is_access_denied(exc):
        winerror = getattr(exc, "winerror", None)
        tag = f"WinError {winerror}" if winerror else type(exc).__name__
        path_bit = f" ({source})" if source is not None else ""
        locks = f" Lock holders: {lock_summary}." if lock_summary else ""
        return (
            f"目录重命名失败: {tag} Access denied{path_bit}. "
            "Directory may be locked by external process "
            "(Explorer / antivirus / indexer / open offline page)."
            f"{locks}"
        )
    return f"目录重命名失败: {exc}"


def _cwd_is_under(folder: Path) -> bool:
    try:
        cwd = Path.cwd().resolve()
        root = folder.resolve()
        cwd.relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _escape_cwd_if_inside(folder: Path) -> bool:
    """If process cwd is inside *folder*, chdir to parent (Windows rename lock)."""
    try:
        root = folder.resolve()
        if not _cwd_is_under(root):
            return False
        parent = root.parent
        os.chdir(parent)
        logger.info("Escaped process cwd from inside rename source → %s", parent)
        return True
    except OSError as exc:
        logger.warning("Failed to escape cwd before rename: %s", exc)
        return False


def _metadata_exclusive_probe(folder: Path) -> str:
    """
    Probe whether ``.info/metadata.json`` can be opened exclusively.

    Returns a short status string (never raises).
    """
    meta = folder / INFO_DIR_NAME / "metadata.json"
    if not meta.is_file():
        return "missing"
    try:
        # Binary r+ forces a real open; close immediately.
        with meta.open("r+b"):
            pass
        return "closed"
    except OSError as exc:
        return f"busy:{type(exc).__name__}"


def collect_directory_rename_diagnostics(
    source: str | Path,
    target: str | Path | None = None,
) -> dict[str, Any]:
    """
    Snapshot in-process state that may lock a Mod folder on Windows.

    Does not log secrets. Safe to call from worker threads.
    """
    src = Path(source).expanduser()
    try:
        src = src.resolve()
    except OSError:
        pass
    dest = Path(target) if target is not None else None
    if dest is not None:
        try:
            dest = dest.expanduser().resolve()
        except OSError:
            dest = Path(target)

    cover_inflight = 0
    cover_tokens = 0
    cover_cancelled = False
    try:
        from services.cover_loader import CoverLoaderManager

        mgr = CoverLoaderManager.instance()
        cover_inflight = mgr.inflight_count(src)
        cover_tokens = mgr.active_token_count_for_path(src)
        cover_cancelled = mgr.is_path_cancelled(src)
    except Exception:  # noqa: BLE001
        pass

    info_dir = src / INFO_DIR_NAME
    meta_path = info_dir / "metadata.json"
    meta_mtime = ""
    if meta_path.is_file():
        try:
            meta_mtime = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(meta_path.stat().st_mtime),
            )
        except OSError:
            meta_mtime = "stat_failed"

    writable = False
    try:
        writable = os.access(str(src), os.W_OK) if src.exists() else False
    except OSError:
        writable = False

    info: dict[str, Any] = {
        "source": str(src),
        "target": str(dest) if dest is not None else "",
        "source_exists": src.is_dir(),
        "target_exists": bool(dest is not None and dest.exists()),
        "source_writable": writable,
        "source_has_info": info_dir.is_dir(),
        "metadata_mtime": meta_mtime,
        "cwd_under_source": _cwd_is_under(src) if src.exists() else False,
        "cover_inflight": cover_inflight,
        "cover_active_tokens": cover_tokens,
        "cover_path_cancelled": cover_cancelled,
        "metadata_file": _metadata_exclusive_probe(src),
        "process_cwd": str(Path.cwd()),
        "process_pid": os.getpid(),
    }
    return info


def prepare_managed_folder_for_rename(managed_path: str | Path) -> None:
    """
    Release UI/file readers that may lock *managed_path* on Windows.

    Explicit pre-rename release:
    - escape process cwd if inside the folder
    - CoverLoader cancel + wait
    - GUI path_release (drop detail/card pixmaps)
    - gc.collect()
    """
    folder = Path(managed_path)
    _escape_cwd_if_inside(folder)
    try:
        from services.cover_loader import CoverLoaderManager
        from services.windows_path_locks import audit_self_open_files

        mgr = CoverLoaderManager.instance()
        before = audit_self_open_files(folder)
        logger.info(
            "pre-rename release: path=%s cover_inflight=%s cover_tokens=%s "
            "self_open_before=%s",
            folder,
            mgr.inflight_count(folder),
            mgr.bound_card_token_count(folder),
            before or "(none)",
        )
        mgr.request_ui_release(folder, wait_ms=500)
        gc.collect()
        after = audit_self_open_files(folder)
        logger.info(
            "pre-rename release done: cover_inflight=%s cover_tokens=%s "
            "self_open_after=%s",
            mgr.inflight_count(folder),
            mgr.bound_card_token_count(folder),
            after or "(none)",
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "prepare_managed_folder_for_rename: cover release skipped",
            exc_info=True,
        )
        gc.collect()


def _ensure_cwd_outside_rename_source(source: Path) -> None:
    """If cwd is under *source*, chdir to project root (fallback: source.parent)."""
    if not _cwd_is_under(source):
        return
    try:
        from core.paths import project_root as _project_root

        escape_to = _project_root()
    except Exception:  # noqa: BLE001
        escape_to = source.parent
    try:
        os.chdir(str(escape_to))
        logger.info(
            "Escaped process cwd from inside rename source → %s", escape_to
        )
    except OSError:
        parent = source.parent
        os.chdir(str(parent))
        logger.info(
            "Escaped process cwd from inside rename source → %s", parent
        )


def _rename_directory_once(source: Path, target: Path) -> None:
    """
    One rename attempt: ``os.rename``, then Windows ``MoveFileExW`` on
    ``PermissionError``. Never uses ``Path.rename`` / ``shutil.move``.
    """
    src_s = str(source)
    dst_s = str(target)
    try:
        os.rename(src_s, dst_s)
        logger.debug("Directory rename via os.rename: %s → %s", src_s, dst_s)
        return
    except PermissionError as exc:
        logger.info(
            "os.rename PermissionError; trying MoveFileExW: %s", exc
        )
        from services.windows_rename import move_directory_movefile_ex

        move_directory_movefile_ex(src_s, dst_s)
        logger.debug("Directory rename via MoveFileExW: %s → %s", src_s, dst_s)


def safe_directory_rename(
    src: str | Path,
    dest: str | Path,
    *,
    attempts: int | None = None,
    delay_sec: float | None = None,
    backoff: Sequence[float] | None = None,
) -> Path:
    """
    Rename a directory with retries for transient Windows access-denied locks.

    Default backoff: 0.5s → 1s → 2s → 4s (5 attempts total).
    Uses ``os.rename`` then ``MoveFileExW``; does not use copy/delete.
    Raises the last OSError after exhausting retries.
    """
    source = Path(src).expanduser()
    target = Path(dest).expanduser()
    try:
        source = source.resolve()
    except OSError:
        pass
    try:
        # resolve() works for non-existent paths (resolves parents).
        target = target.resolve()
    except OSError:
        pass

    if source == target or (
        source.exists()
        and target.exists()
        and source.resolve() == target.resolve()
    ):
        return target

    if backoff is not None:
        pauses = [max(0.05, float(x)) for x in backoff]
    elif attempts is not None or delay_sec is not None:
        # Legacy fixed-delay API (tests).
        n = max(1, int(attempts if attempts is not None else 5))
        pause = max(0.05, float(delay_sec if delay_sec is not None else 0.3))
        pauses = [pause] * max(0, n - 1)
    else:
        pauses = [max(0.05, float(x)) for x in DEFAULT_RENAME_BACKOFF_SEC]

    tries = len(pauses) + 1

    prepare_managed_folder_for_rename(source)

    last: OSError | None = None
    for attempt in range(1, tries + 1):
        try:
            _ensure_cwd_outside_rename_source(source)
            gc.collect()

            if not source.exists():
                raise FileNotFoundError(f"Rename source missing: {source}")
            if not source.is_dir():
                raise NotADirectoryError(f"Rename source is not a directory: {source}")
            if target.exists():
                try:
                    same = source.resolve() == target.resolve()
                except OSError:
                    same = False
                if same:
                    return target
                raise FileExistsError(f"Rename target already exists: {target}")

            logger.info(
                "Directory rename attempt=%s/%s cwd=%s source_exists=%s "
                "target_exists=%s source=%s target=%s",
                attempt,
                tries,
                os.getcwd(),
                source.exists(),
                target.exists(),
                source,
                target,
            )
            _rename_directory_once(source, target)
            return target
        except OSError as exc:
            last = exc
            if not _is_access_denied(exc) or attempt >= tries:
                if _is_access_denied(exc):
                    logger.error(
                        "Directory rename failed after %s attempts: %s",
                        attempt,
                        format_directory_rename_error(exc, source=source),
                    )
                raise
            logger.info(
                "Directory rename locked (attempt %s/%s); recheck then retry",
                attempt,
                tries,
            )
            prepare_managed_folder_for_rename(source)
            time.sleep(pauses[attempt - 1])
    assert last is not None
    raise last


def rename_managed_folder_for_title(
    managed_path: str | Path,
    meta: ModMetadata,
    *,
    library_root: str | Path | None = None,
) -> tuple[Path, bool]:
    """
    Rename ``Unknown_Mod_*`` folder to the real sanitized title when needed.

    Returns ``(path, renamed)``. Preserves ``.info`` (whole-folder rename) and
    uses ``unique_destination`` for collisions.
    """
    folder = Path(managed_path).expanduser().resolve()
    if not folder.is_dir():
        return folder, False

    root = Path(library_root) if library_root else folder.parents[1]
    mgr = ModFileManager(root)
    desired = mgr.mod_folder_name(meta)
    if desired == folder.name:
        return folder, False

    # Only rename away from unknown / numeric leaf names (safe for deployed state:
    # deploy tracks mod_id, not folder path).
    if not is_unknown_mod_title(folder.name, published_file_id=meta.published_file_id):
        # Folder already has a human name; still allow rename when it is clearly
        # the placeholder style, otherwise keep path stable.
        return folder, False

    target = unique_destination(
        folder.parent,
        desired,
        published_file_id=str(meta.published_file_id or ""),
    )
    if target.resolve() == folder.resolve():
        return folder, False

    from services.directory_move import rename_directory_or_fallback

    def _rename_once(src: Path, dst: Path) -> Path:
        return safe_directory_rename(src, dst, attempts=1)

    rename_directory_or_fallback(folder, target, rename_once=_rename_once)
    return target, True


def refresh_steam_mod_metadata(
    mod_id: str | int,
    managed_path: str | Path,
    *,
    library_root: str | Path | None = None,
    client: SteamWorkshopClient | None = None,
    force: bool = False,
    download_cover: bool = True,
    allow_official_sync: bool = True,
    db=None,
) -> MetadataRefreshResult:
    """
    Re-fetch Steam Workshop metadata for one managed Mod and persist results.

    When *force* is False (default), healthy mods are skipped (Feature 3).
    When *allow_official_sync* is False or ``official_metadata_synced`` is set,
    no network I/O is performed.
    """
    from core.db_manager import get_db
    from services.metadata_ownership import (
        FIELD_COVER,
        FIELD_DESCRIPTION,
        FIELD_DISPLAY_NAME,
        merge_official_sidecar_fields,
        should_apply_official_field,
    )

    mid = str(mod_id).strip()
    folder = Path(managed_path).expanduser().resolve()
    database = db if db is not None else get_db()

    from services.path_lifecycle import record_filesystem_rename, resolve_managed_folder

    resolved = resolve_managed_folder(mid, hint_path=folder, db=database)
    folder = resolved.path or folder

    if not allow_official_sync or database.is_official_metadata_synced(mid):
        existing = None
        try:
            root = Path(library_root) if library_root else (
                folder.parents[1] if len(folder.parts) >= 2 else folder.parent
            )
            existing = ModFileManager(root).load_metadata(folder)
        except Exception:  # noqa: BLE001
            existing = None
        title = (existing.title if existing else "") or folder.name
        return MetadataRefreshResult(
            mod_id=mid,
            success=True,
            skipped=True,
            managed_path=folder,
            old_path=folder,
            title=title,
            message="已刷新本地状态",
        )

    root = Path(library_root) if library_root else (
        folder.parents[1] if len(folder.parts) >= 2 else folder.parent
    )
    mgr = ModFileManager(root)
    existing = mgr.load_metadata(folder)

    if not force and not needs_metadata_refresh(existing, folder_name=folder.name):
        title = (existing.title if existing else "") or folder.name
        return MetadataRefreshResult(
            mod_id=mid,
            success=True,
            skipped=True,
            managed_path=folder,
            old_path=folder,
            title=title,
        )

    overrides = database.get_user_override_fields(mid)
    display_info = database.get_mod_display_info(mid)
    local_custom_desc = (
        str(display_info.custom_description or "").strip()
        if display_info
        else ""
    )

    owns_client = client is None
    api = client or SteamWorkshopClient(enable_scrape_fallback=False)
    try:
        try:
            # Manual refresh: only this mod id, API retries, no workshop scrape fan-out.
            fetched_list = api.refresh_details(
                [mid],
                enable_scrape_fallback=False,
            )
            fetched = fetched_list[0] if fetched_list else ModMetadata(
                published_file_id=mid,
                fetch_error="Empty response from Steam API",
            )
        except Exception as exc:  # noqa: BLE001
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                error=str(exc) or "Steam metadata fetch failed",
            )

        if fetched.fetch_error or is_unknown_mod_title(
            fetched.title, published_file_id=mid
        ):
            # Persist failure marker so UI / next retry still see fetch_error.
            failed = existing or ModMetadata(published_file_id=mid)
            failed.published_file_id = mid
            if fetched.title:
                failed.title = fetched.title
            failed.fetch_error = (
                str(fetched.fetch_error or "").strip()
                or "GetPublishedFileDetails failed"
            )
            failed.managed_path = str(folder)
            try:
                mgr.save_metadata(failed, folder)
            except OSError:
                pass
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                title=failed.title or "",
                error=failed.fetch_error,
            )

        # Merge into existing sidecar fields we want to keep.
        meta = existing or ModMetadata(published_file_id=mid)
        meta.published_file_id = mid
        meta.title = fetched.title.strip()
        official_description = str(fetched.description or "").strip()
        if official_description and should_apply_official_field(
            FIELD_DESCRIPTION,
            overrides=overrides,
            local_value=local_custom_desc or str(meta.description or ""),
            mod_id=mid,
        ):
            meta.description = official_description
        meta.preview_url = fetched.preview_url or meta.preview_url
        meta.file_size = fetched.file_size or meta.file_size
        meta.time_created = fetched.time_created or meta.time_created
        meta.time_updated = fetched.time_updated or meta.time_updated
        meta.creator_steam_id = fetched.creator_steam_id or meta.creator_steam_id
        meta.app_id = fetched.app_id or meta.app_id
        meta.game_name = fetched.game_name or meta.game_name
        if fetched.tags:
            meta.tags = list(fetched.tags)
        meta.fetch_error = None

        local_cover = (
            str(display_info.cover_path or "").strip()
            if display_info
            else str(meta.cover_path or "")
        )

        # Cover under .info/
        cover_path = ""
        if (
            download_cover
            and meta.preview_url
            and should_apply_official_field(
                FIELD_COVER,
                overrides=overrides,
                local_value=local_cover,
                mod_id=mid,
            )
        ):
            try:
                info_dir = mgr.ensure_info_dir(folder)
                with SteamWorkshopClient(
                    timeout=10,
                    enable_scrape_fallback=False,
                    request_interval=0.05,
                ) as cover_client:
                    saved = cover_client.fetch_and_save_cover(
                        meta, info_dir, filename=COVER_BASENAME
                    )
                if saved:
                    meta.cover_path = str(saved)
                    cover_path = str(saved)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cover refresh failed for %s: %s", mid, exc)

        # Rename Unknown_* folder → real title
        new_path = folder
        renamed = False
        try:
            new_path, renamed = rename_managed_folder_for_title(
                folder, meta, library_root=root
            )
        except OSError as exc:
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                title=meta.title,
                error=f"Folder rename failed: {exc}",
                cover_path=cover_path,
            )

        if renamed:
            from services.path_lifecycle import PathLifecycleStage

            path_commit = record_filesystem_rename(
                mid,
                folder,
                new_path,
                reason="refresh",
                db=database,
            )
            if not path_commit.success:
                return MetadataRefreshResult(
                    mod_id=mid,
                    success=False,
                    managed_path=new_path,
                    old_path=folder,
                    renamed=True,
                    title=meta.title,
                    error=(
                        f"[{path_commit.stage}] {path_commit.error}"
                        if path_commit.error
                        else "路径提交失败"
                    ),
                    cover_path=cover_path,
                )

        meta.managed_path = str(new_path)
        meta.local_path = str(new_path)
        sidecar_existing = read_info_metadata_dict(new_path) or {}
        local_display = str(sidecar_existing.get("display_name") or "").strip()
        if display_info and str(display_info.user_display_name or "").strip():
            local_display = str(display_info.user_display_name or "").strip()
        if should_apply_official_field(
            FIELD_DISPLAY_NAME,
            overrides=overrides,
            local_value=local_display,
            mod_id=mid,
        ):
            meta.json_display_name = ""
        else:
            meta.json_display_name = local_display

        # Upsert Steam title first so sidecar merge / Detail see the real name.
        try:
            from core.db_manager import get_db

            upsert_meta = ModMetadata(
                published_file_id=mid,
                title=meta.title,
                description=official_description or meta.description or "",
                preview_url=meta.preview_url,
                app_id=meta.app_id,
            )
            database.upsert_mod(upsert_meta)
            _clear_placeholder_display_name(mid, new_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DB upsert after metadata refresh failed for %s: %s", mid, exc
            )

        try:
            mgr.save_metadata(meta, new_path, sync_backup=False)
            cover_rel = ""
            if cover_path:
                from services.importers.image_picker import relative_cover_path

                cover_rel = relative_cover_path(new_path, Path(cover_path))
            merged_sidecar = merge_official_sidecar_fields(
                read_info_metadata_dict(new_path) or {},
                mod_id=mid,
                overrides=overrides,
                official_title=meta.title,
                official_description=meta.description or "",
                official_preview_url=meta.preview_url or "",
                cover_rel=cover_rel,
            )
            persist_unified_metadata_dict(
                new_path,
                merged_sidecar,
                sync_backup=False,
                sync_reason="refresh",
            )
            _persist_cleared_fetch_error(new_path)
            _clear_placeholder_display_name(mid, new_path)
        except OSError as exc:
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=new_path,
                old_path=folder,
                renamed=renamed,
                title=meta.title,
                error=f"Failed to write metadata.json: {exc}",
                cover_path=cover_path,
            )

        try:
            from services.metadata_backup_sync import sync_after_metadata_change

            sync_after_metadata_change(mid, new_path, "refresh")
        except Exception:  # noqa: BLE001
            pass

        return MetadataRefreshResult(
            mod_id=mid,
            success=True,
            skipped=False,
            renamed=renamed,
            managed_path=new_path,
            old_path=folder,
            title=meta.title,
            cover_path=cover_path or str(meta.cover_path or ""),
        )
    finally:
        if owns_client:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass


def refresh_steam_mods_metadata(
    entries: Sequence[tuple[str, Path]],
    *,
    library_root: str | Path | None = None,
    max_workers: int = 2,
    on_progress: MetadataProgress | None = None,
    download_cover: bool = True,
) -> list[MetadataRefreshResult]:
    """
    Batch refresh with skip-healthy + concurrency capped at 2.

    *entries*: ``(mod_id, managed_path)`` pairs. Duplicate mod_ids are collapsed
    to a single request.
    """
    # Dedupe by mod_id (first path wins).
    seen: set[str] = set()
    unique: list[tuple[str, Path]] = []
    for mid_raw, path in entries:
        mid = str(mid_raw).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        unique.append((mid, Path(path)))

    total = len(unique)
    if total == 0:
        return []

    # Pre-filter skips without hitting Steam (Feature 3).
    to_fetch: list[tuple[str, Path]] = []
    results: list[MetadataRefreshResult] = []
    root = Path(library_root) if library_root else None
    mgr = ModFileManager(root) if root else None

    for mid, path in unique:
        meta = None
        try:
            loader = mgr or ModFileManager(path.parents[1])
            meta = loader.load_metadata(path)
        except Exception:  # noqa: BLE001
            meta = None
        if not needs_metadata_refresh(meta, folder_name=path.name):
            results.append(
                MetadataRefreshResult(
                    mod_id=mid,
                    success=True,
                    skipped=True,
                    managed_path=path,
                    old_path=path,
                    title=(meta.title if meta else path.name),
                )
            )
        else:
            to_fetch.append((mid, path))

    done = len(results)
    if on_progress:
        on_progress(done, total, f"Refreshing metadata: {done} / {total}")

    if not to_fetch:
        return results

    workers = max(1, min(int(max_workers), 2, len(to_fetch)))

    def _one(item: tuple[str, Path]) -> MetadataRefreshResult:
        mid, path = item
        lib = library_root or (
            path.parents[1] if len(path.parts) >= 2 else path.parent
        )
        from core.db_manager import get_db

        database = get_db()
        allow = not database.is_official_metadata_synced(mid)
        return refresh_steam_mod_metadata(
            mid,
            path,
            library_root=lib,
            client=None,
            force=True,
            download_cover=download_cover,
            allow_official_sync=allow,
            db=database,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, item): item for item in to_fetch}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                mid, path = futures[fut]
                results.append(
                    MetadataRefreshResult(
                        mod_id=mid,
                        success=False,
                        managed_path=path,
                        old_path=path,
                        error=str(exc),
                    )
                )
            done = len(results)
            shown = min(done, total)
            if on_progress:
                on_progress(
                    shown,
                    total,
                    f"Refreshing metadata: {shown} / {total}",
                )

    # Preserve input order for stable UI reporting.
    by_id = {r.mod_id: r for r in results}
    ordered = [by_id[mid] for mid, _ in unique if mid in by_id]
    return ordered


def filter_steam_batch_entries(
    entries: Iterable[tuple[str, Path, str]],
) -> list[tuple[str, Path]]:
    """Keep Steam platform entries as ``(mod_id, path)`` for batch refresh."""
    out: list[tuple[str, Path]] = []
    for mid, path, plat in entries:
        if normalize_platform(plat) != PLATFORM_STEAM:
            continue
        out.append((str(mid), Path(path)))
    return out


def refresh_selected_mods_metadata(
    entries: Sequence[tuple[str, Path, str]],
    *,
    library_root: str | Path | None = None,
    max_workers: int = 2,
    on_progress: MetadataProgress | None = None,
) -> list[MetadataRefreshResult]:
    """
    Batch refresh for mixed platforms (Steam / Nexus / Mod.io / GitHub / 其它).

    Nexus ``workspace_id`` correction runs as a silent per-item wash inside
    the loop — never raises, never blocks the next mod.
    """
    unique: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for mid_raw, path, plat in entries:
        mid = str(mid_raw or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        unique.append((mid, Path(path), normalize_platform(plat)))

    total = len(unique)
    if total == 0:
        return []

    from core.db_manager import get_db
    from services.mod_refresh import refresh_mod

    database = get_db()
    results: list[MetadataRefreshResult] = []
    done = 0

    def _report() -> None:
        if on_progress:
            on_progress(
                done,
                total,
                f"Refreshing metadata: {done} / {total}",
            )

    _report()

    for mid, path, plat in unique:
        if plat == PLATFORM_NEXUS:
            silent_correct_nexus_workspace_id(mid)
        source_url = ""
        try:
            info = database.get_mod_display_info(mid)
            if info is not None:
                source_url = str(info.source_url or "").strip()
        except Exception:  # noqa: BLE001
            source_url = ""
        lib = library_root or (
            path.parents[1] if len(path.parts) >= 2 else path.parent
        )
        try:
            refresh_result = refresh_mod(
                mid,
                path if path.parts else Path("."),
                platform=plat,
                library_root=lib,
                source_url=source_url,
                db=database,
            )
            result = refresh_result.to_metadata_refresh_result()
            if plat == PLATFORM_NEXUS:
                silent_correct_nexus_workspace_id(mid)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            results.append(
                MetadataRefreshResult(
                    mod_id=mid,
                    success=False,
                    managed_path=path if path.parts else None,
                    old_path=path if path.parts else None,
                    error=str(exc),
                )
            )
        done += 1
        _report()

    by_id = {r.mod_id: r for r in results}
    return [by_id[mid] for mid, _path, _plat in unique if mid in by_id]
