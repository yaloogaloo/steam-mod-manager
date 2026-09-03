"""Mod.io metadata refresh (API → metadata.json / SQLite / rename / cover).

Does not touch Steam refresh, Import Pipeline, Deploy, or offline archive.
Reuses ``safe_directory_rename`` and existing cover install helpers.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, coerce_modio_api_mod_id
from core.sanitize import sanitize_folder_name
from services.file_ops import (
    INFO_DIR_NAME,
    ModFileManager,
    read_info_metadata_dict,
)
from services.metadata_refresh import (
    MetadataRefreshResult,
    collect_directory_rename_diagnostics,
    format_directory_rename_error,
    prepare_managed_folder_for_rename,
    safe_directory_rename,
)
from services.path_lifecycle import (
    PathLifecycleStage,
    record_filesystem_rename,
)
from services.modio_api import (
    ModioAPIError,
    ModioClient,
    parse_modio_url,
)

logger = logging.getLogger(__name__)


def _read_local_modio_ids(managed_path: Path) -> tuple[int, int]:
    """Return ``(modio_mod_id, modio_game_id)`` from metadata.json when present."""
    data = read_info_metadata_dict(managed_path) or {}
    mid = 0
    gid = 0
    raw_m = str(data.get("modio_mod_id") or "").strip()
    if raw_m.isdigit():
        mid = coerce_modio_api_mod_id(raw_m)
    raw_g = str(data.get("modio_game_id") or "").strip()
    if raw_g.isdigit():
        gid = int(raw_g)
    return mid, gid


def _resolve_source_url(
    *,
    managed_path: Path,
    meta: ModMetadata | None,
    display_info_source_url: str = "",
) -> str:
    if display_info_source_url.strip():
        return display_info_source_url.strip()
    if meta is not None and str(meta.url or "").strip():
        return str(meta.url).strip()
    data = read_info_metadata_dict(managed_path) or {}
    for key in ("url", "source_url", "website"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _logo_suffix(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


def _is_same_managed_directory(source: Path, target: Path) -> bool:
    """True when *source* and *target* are the same directory (Windows case-fold safe)."""
    try:
        if source.resolve() == target.resolve():
            return True
    except OSError:
        pass
    try:
        return source.is_dir() and target.exists() and source.samefile(target)
    except OSError:
        return False


def _is_duplicate_folder_for_same_mod(
    source: Path,
    target: Path,
    *,
    mod_id: int | str,
) -> bool:
    """
    True when *target* is a different path but belongs to the same internal mod_id.

    Used to skip rename (not fail) when slug-named and title-named folders coexist.
    """
    if not target.is_dir() or _is_same_managed_directory(source, target):
        return False
    meta = read_info_metadata_dict(target) or {}
    pub = str(meta.get("published_file_id") or "").strip()
    if pub and pub == str(mod_id):
        return True
    return False


def rename_modio_folder_for_title(
    managed_path: str | Path,
    new_title: str,
    *,
    library_root: str | Path | None = None,
) -> tuple[Path, bool]:
    """
    Rename the Mod folder to the sanitized Mod.io title.

    On name collision with a *different* existing directory, raises
    ``FileExistsError`` (no overwrite / no unique suffix).

    Case-only differences on case-insensitive filesystems (Windows) are treated
    as the same directory — no rename, no error — so metadata refresh can proceed.
    """
    folder = Path(managed_path).expanduser().resolve()
    if not folder.is_dir():
        return folder, False

    desired = sanitize_folder_name(
        str(new_title or "").strip(),
        fallback=folder.name,
    )
    if not desired or desired == folder.name:
        return folder, False

    target = (folder.parent / desired).resolve()
    # Windows: ``polyamoryfixes`` vs ``PolyamoryFixes`` resolve to the same dir.
    if _is_same_managed_directory(folder, target):
        logger.info(
            "Mod.io folder rename skipped (same directory / case-only): %s -> %s",
            folder.name,
            desired,
        )
        return folder, False

    diag = collect_directory_rename_diagnostics(folder, target)
    logger.info(
        "Mod.io folder rename: src=%s target=%s target_exists=%s "
        "source_writable=%s has_info=%s metadata_mtime=%s "
        "cover_inflight=%s cover_tokens=%s metadata_file=%s "
        "cwd=%s cwd_under_source=%s",
        diag.get("source"),
        diag.get("target"),
        diag.get("target_exists"),
        diag.get("source_writable"),
        diag.get("source_has_info"),
        diag.get("metadata_mtime"),
        diag.get("cover_inflight"),
        diag.get("cover_active_tokens"),
        diag.get("metadata_file"),
        diag.get("process_cwd"),
        diag.get("cwd_under_source"),
    )
    if diag.get("target_exists") and not _is_same_managed_directory(folder, target):
        raise FileExistsError(
            f"目录已存在，无法重命名为「{desired}」：{target}"
        )
    if not diag.get("source_exists"):
        return folder, False

    # Release readers / escape cwd before rename (CoverLoader + process cwd).
    prepare_managed_folder_for_rename(folder)
    post = collect_directory_rename_diagnostics(folder, target)
    logger.info(
        "Mod.io folder rename after prepare: cover_inflight=%s "
        "metadata_file=%s cwd_under_source=%s",
        post.get("cover_inflight"),
        post.get("metadata_file"),
        post.get("cwd_under_source"),
    )

    from services.directory_move import rename_directory_or_fallback

    def _rename_once(src: Path, dst: Path) -> Path:
        # Single attempt only — WinError 5 falls through to content move.
        return safe_directory_rename(src, dst, attempts=1)

    rename_directory_or_fallback(folder, target, rename_once=_rename_once)
    return target, True


def _patch_metadata_json(
    managed_path: Path,
    *,
    title: str,
    description: str,
    url: str,
    author: str,
    preview_url: str,
    modio_mod_id: int,
    modio_game_id: int,
    name_id: str,
    cover_rel: str = "",
    old_managed_path: Path | None = None,
    mod_id: str = "",
    db=None,
) -> None:
    """
    Write Mod.io identity fields into ``.info/metadata.json``.

    User-facing fields obey ``user_override_fields``; official ``title`` always updates.
    """
    from core.db_manager import get_db
    from services.metadata_ownership import merge_official_sidecar_fields

    mid = str(mod_id or "").strip()
    database = db if db is not None else get_db()
    overrides = database.get_user_override_fields(mid) if mid else {}
    data = merge_official_sidecar_fields(
        read_info_metadata_dict(managed_path) or {},
        mod_id=mid,
        overrides=overrides,
        official_title=title,
        official_description=description,
        official_preview_url=preview_url,
        cover_rel=cover_rel,
    )
    data["url"] = url
    data["source_url"] = url
    data["source_type"] = PLATFORM_MODIO
    if author:
        data["author"] = author
    if modio_mod_id > 0:
        data["modio_mod_id"] = int(modio_mod_id)
    if modio_game_id > 0:
        data["modio_game_id"] = int(modio_game_id)
    if name_id:
        data["modio_name_id"] = name_id
    data.pop("fetch_error", None)
    # Witcher 3 ONLY: Mod.io ``version`` is NOT ``game_version``. Leave any
    # existing sidecar game_version untouched (never copy API version here).

    final = Path(managed_path)
    data["managed_path"] = str(final)
    data["local_path"] = str(final)
    if old_managed_path is not None:
        old_prefix = str(old_managed_path)
        new_prefix = str(final)
        if old_prefix != new_prefix:
            for key in ("offline_page_path", "offline_page", "source_path"):
                raw = str(data.get(key) or "")
                if raw.startswith(old_prefix):
                    data[key] = new_prefix + raw[len(old_prefix) :]

    info = final / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / "metadata.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _update_modio_db_identity(
    mid: str,
    *,
    title: str,
    description: str,
    canonical: str,
    preview_url: str,
    modio_mod_id: int,
    db=None,
) -> None:
    """Persist Mod.io title / urls to SQLite; respect user metadata overrides."""
    from core.db_manager import get_db
    from services.metadata_ownership import (
        FIELD_DISPLAY_NAME,
        should_apply_official_field,
    )

    database = db if db is not None else get_db()
    ext_value = str(int(modio_mod_id)) if modio_mod_id > 0 else ""
    try:
        kwargs = dict(
            platform=PLATFORM_MODIO,
            source_url=canonical,
            title=title,
            description=description,
            preview_url=preview_url,
        )
        if ext_value:
            kwargs["external_id"] = ext_value
        database.update_mod_platform_info(mid, **kwargs)
    except ValueError:
        database.update_mod_platform_info(
            mid,
            platform=PLATFORM_MODIO,
            source_url=canonical,
            title=title,
            description=description,
            preview_url=preview_url,
        )
    info = database.get_mod_display_info(mid)
    local_display = str(info.user_display_name or "").strip() if info else ""
    if should_apply_official_field(
        FIELD_DISPLAY_NAME,
        overrides=database.get_user_override_fields(mid),
        local_value=local_display,
        mod_id=mid,
    ):
        try:
            database.update_mod_user_metadata(mid, {"display_name": ""})
        except Exception:  # noqa: BLE001
            logger.debug("clear display_name after mod.io sync skipped for %s", mid)


def _download_cover(
    client: ModioClient,
    logo_url: str,
    managed_path: Path,
    *,
    mod_id: str,
) -> str:
    if not logo_url:
        return ""
    from services.importers.image_picker import apply_cover_to_mod

    prepare_managed_folder_for_rename(managed_path)
    suffix = _logo_suffix(logo_url)
    with tempfile.TemporaryDirectory(prefix="modio_cover_") as tmp:
        tmp_file = Path(tmp) / f"logo{suffix}"
        client.download_file(logo_url, tmp_file)
        rel = apply_cover_to_mod(
            managed_path,
            tmp_file,
            mod_id=mod_id,
            update_db=True,
            sync_backup=False,
            mark_user_override=False,
        )
    return rel or ""


def refresh_modio_mod_metadata(
    mod_id: str | int,
    managed_path: str | Path,
    *,
    library_root: str | Path | None = None,
    client: ModioClient | None = None,
    download_cover: bool = True,
    source_url: str = "",
    allow_official_sync: bool = True,
    db=None,
) -> MetadataRefreshResult:
    """
    Fetch Mod.io Mod Object and persist name / description / cover / folder rename.

    When *allow_official_sync* is False or ``official_metadata_synced`` is set,
    no Mod.io network I/O is performed.
    """
    from core.db_manager import get_db
    from services.metadata_ownership import (
        FIELD_COVER,
        merge_official_sidecar_fields,
        should_apply_official_field,
    )

    mid = str(mod_id).strip()
    database = db if db is not None else get_db()

    from services.path_lifecycle import resolve_refresh_folder

    folder = resolve_refresh_folder(mid, managed_path, db=database)

    if not allow_official_sync or database.is_official_metadata_synced(mid):
        title = folder.name
        try:
            existing = read_info_metadata_dict(folder)
            if existing:
                title = str(existing.get("title") or existing.get("display_name") or title)
        except Exception:  # noqa: BLE001
            pass
        return MetadataRefreshResult(
            mod_id=mid,
            success=True,
            skipped=True,
            managed_path=folder,
            old_path=folder,
            title=title,
            message="已刷新本地状态",
        )

    logger.info(
        "Starting Mod.io metadata refresh for mod_id=%s path=%s", mid, folder
    )
    if not folder.is_dir():
        logger.error("Mod.io refresh aborted: directory missing (%s)", folder)
        return MetadataRefreshResult(
            mod_id=mid,
            success=False,
            managed_path=folder,
            old_path=folder,
            error=f"[PATH_INVALID] Mod 目录不存在: {folder}",
        )

    folder = folder.resolve()

    root = Path(library_root) if library_root else (
        folder.parents[1] if len(folder.parts) >= 2 else folder.parent
    )
    mgr = ModFileManager(root)
    existing = mgr.load_metadata(folder)

    url = _resolve_source_url(
        managed_path=folder,
        meta=existing,
        display_info_source_url=source_url,
    )
    parts = parse_modio_url(url) if url else None
    logger.info(
        "Mod.io refresh identity: mod_id=%s platform=%s url=%r "
        "parsed_game=%r parsed_name_id=%r",
        mid,
        PLATFORM_MODIO,
        (url or "")[:240],
        parts.game_slug if parts else None,
        parts.mod_name_id if parts else None,
    )
    if parts is None:
        logger.error(
            "Mod.io refresh aborted: invalid URL (raw=%r)",
            (url or "")[:200],
        )
        return MetadataRefreshResult(
            mod_id=mid,
            success=False,
            managed_path=folder,
            old_path=folder,
            error="[API_PARSE] Mod.io URL 无效。请在编辑信息中填写类似 "
            "https://mod.io/g/<game>/m/<mod-name> 的官方链接。",
        )

    local_mod_id, local_game_id = _read_local_modio_ids(folder)
    # Numeric external_id from DB may hold the Mod.io mod id (never internal mod_id).
    try:
        info = database.get_mod_display_info(mid)
        if info is not None:
            ext = str(info.external_id or "").strip()
            api_mod_id = coerce_modio_api_mod_id(ext)
            if api_mod_id > 0 and local_mod_id <= 0:
                local_mod_id = api_mod_id
            if not url and info.source_url:
                url = info.source_url
                parts = parse_modio_url(url) or parts
            logger.info(
                "Mod.io refresh DB identity: external_id=%r workspace_id=%r "
                "local_modio_mod_id=%s local_modio_game_id=%s",
                ext,
                getattr(info, "workspace_id", ""),
                local_mod_id,
                local_game_id,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Mod.io refresh: failed reading local DB identity")
        info = None

    owns = client is None
    api = client or ModioClient()
    try:
        try:
            logger.info(
                "Mod.io resolve_mod: game_slug=%r name_id=%r game_id=%s mod_id=%s",
                parts.game_slug if parts else "",
                parts.mod_name_id if parts else "",
                local_game_id,
                local_mod_id,
            )
            details = api.resolve_mod(
                game_slug=parts.game_slug if parts else "",
                mod_name_id=parts.mod_name_id if parts else "",
                game_id=local_game_id,
                mod_id=local_mod_id,
            )
        except ModioAPIError as exc:
            logger.exception("Mod.io API resolve failed: %s", exc)
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                error=f"[API_FETCH] {exc}",
            )

        logger.info(
            "Mod.io API payload: id=%s game_id=%s name=%r name_id=%r "
            "has_summary=%s has_description=%s has_logo=%s",
            details.mod_id,
            details.game_id,
            details.name,
            details.name_id,
            bool(details.summary),
            bool(details.description),
            bool(details.logo_url),
        )

        if not details.name.strip():
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                error="[API_PARSE] Mod.io API 未返回 Mod 名称",
            )

        # Conflict check before mutating files (except we still allow metadata
        # update if rename is not needed).
        desired = sanitize_folder_name(details.name, fallback=folder.name)
        target_probe = (folder.parent / desired).resolve()
        skip_rename_same_mod_duplicate = False
        if (
            desired
            and desired != folder.name
            and target_probe.exists()
            and not _is_same_managed_directory(folder, target_probe)
        ):
            if _is_duplicate_folder_for_same_mod(folder, target_probe, mod_id=mid):
                logger.warning(
                    "Mod.io rename skipped: duplicate folder for same mod_id %s: %s",
                    mid,
                    target_probe,
                )
                skip_rename_same_mod_duplicate = True
            else:
                return MetadataRefreshResult(
                    mod_id=mid,
                    success=False,
                    managed_path=folder,
                    old_path=folder,
                    title=details.name,
                    error=(
                        f"[METADATA_MERGE] 目录名冲突：目标「{desired}」已存在，"
                        "未覆盖现有 Mod，也未删除当前目录。"
                    ),
                )

        canonical = (
            details.profile_url.strip()
            or (parts.canonical_url if parts else "")
            or url
        )
        description = details.description or details.summary
        cover_err = ""
        cover_rel = ""

        # Rename first (atomic or content-move fallback). Identity fields are
        # written on the *final* path so every rename method shares one success path.
        prepare_managed_folder_for_rename(folder)

        new_path = folder
        renamed = False
        if skip_rename_same_mod_duplicate:
            new_path = folder
            renamed = False
        else:
            try:
                new_path, renamed = rename_modio_folder_for_title(
                    folder, details.name, library_root=root
                )
            except FileExistsError as exc:
                logger.exception("Mod.io rename conflict: %s", exc)
                return MetadataRefreshResult(
                    mod_id=mid,
                    success=False,
                    managed_path=folder,
                    old_path=folder,
                    title=details.name,
                    error=f"[METADATA_MERGE] {exc}",
                    cover_path="",
                    message="",
                )
            except OSError as exc:
                logger.exception("Mod.io rename failed: %s", exc)
                lock_summary = ""
                try:
                    from services.windows_path_locks import (
                        find_processes_locking_path,
                        summarize_lock_holders,
                    )

                    lock_summary = summarize_lock_holders(
                        find_processes_locking_path(folder)
                    )
                except Exception:  # noqa: BLE001
                    pass
                rename_err = format_directory_rename_error(
                    exc, source=folder, lock_summary=lock_summary
                )
                return MetadataRefreshResult(
                    mod_id=mid,
                    success=False,
                    managed_path=folder,
                    old_path=folder,
                    title=details.name,
                    error=f"[METADATA_MERGE] {rename_err}",
                    cover_path="",
                )

        if renamed:
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
                    title=details.name,
                    error=(
                        f"[{path_commit.stage}] {path_commit.error}"
                        if path_commit.error
                        else "路径提交失败"
                    ),
                )

        # Unified success path (os.rename / MoveFileExW / fallback content move).
        logger.info(
            "Updating metadata for Mod.io mod_id=%s title=%r path=%s renamed=%s",
            mid,
            details.name,
            new_path,
            renamed,
        )
        _patch_metadata_json(
            new_path,
            title=details.name,
            description=description,
            url=canonical,
            author=details.author,
            preview_url=details.logo_url,
            modio_mod_id=details.mod_id,
            modio_game_id=details.game_id,
            name_id=details.name_id or (parts.mod_name_id if parts else ""),
            cover_rel="",
            old_managed_path=folder if renamed else None,
            mod_id=mid,
            db=database,
        )
        try:
            _update_modio_db_identity(
                mid,
                title=details.name,
                description=description,
                canonical=canonical,
                preview_url=details.logo_url,
                modio_mod_id=details.mod_id,
                db=database,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Mod.io DB update failed for %s: %s", mid, exc)
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=new_path,
                old_path=folder,
                renamed=renamed,
                title=details.name,
                error=f"[{PathLifecycleStage.DB_WRITE.value}] 平台信息更新失败: {exc}",
            )

        overrides = database.get_user_override_fields(mid)
        display_info = database.get_mod_display_info(mid)
        local_cover = (
            str(display_info.cover_path or "").strip() if display_info else ""
        )
        if download_cover and details.logo_url:
            try:
                if should_apply_official_field(
                    FIELD_COVER,
                    overrides=overrides,
                    local_value=local_cover,
                    mod_id=mid,
                ):
                    cover_rel = _download_cover(
                        api, details.logo_url, new_path, mod_id=mid
                    )
                    if cover_rel:
                        merged = merge_official_sidecar_fields(
                            read_info_metadata_dict(new_path) or {},
                            mod_id=mid,
                            overrides=overrides,
                            official_title=details.name,
                            official_description=description,
                            official_preview_url=details.logo_url,
                            cover_rel=cover_rel,
                        )
                        (new_path / INFO_DIR_NAME / "metadata.json").write_text(
                            json.dumps(merged, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        try:
                            database.update_mod_cover_path(mid, cover_rel)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "Mod.io cover path DB update failed for %s", mid
                            )
            except ModioAPIError as exc:
                logger.exception("Mod.io cover refresh failed: %s", exc)
                cover_err = str(exc)

        if renamed:
            message = "Mod.io 元数据刷新成功，Mod 目录已重命名"
        else:
            message = "Mod.io 元数据刷新成功"
        if cover_err:
            message = f"{message}（封面下载失败: {cover_err}）"

        logger.info(
            "Mod.io refresh completed (renamed=%s title=%r path=%s)",
            renamed,
            details.name,
            new_path,
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
            title=details.name,
            cover_path=cover_rel,
            message=message,
        )
    finally:
        if owns:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                logger.exception("ModioClient.close failed")
