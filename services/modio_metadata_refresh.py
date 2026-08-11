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
from core.mod_platform import PLATFORM_MODIO
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
        mid = int(raw_m)
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
    if diag.get("target_exists"):
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
) -> None:
    """
    Write Mod.io identity fields into ``.info/metadata.json``.

    Mod.io refresh always syncs both ``title`` and ``display_name`` to the
    official API name so Library / Detail show the refreshed Mod 名称.
    """
    data = read_info_metadata_dict(managed_path) or {}
    data["title"] = title
    data["display_name"] = title
    data["description"] = description
    data["url"] = url
    data["source_url"] = url
    data["source_type"] = PLATFORM_MODIO
    data["preview_url"] = preview_url
    if author:
        data["author"] = author
    if modio_mod_id > 0:
        data["modio_mod_id"] = int(modio_mod_id)
    if modio_game_id > 0:
        data["modio_game_id"] = int(modio_game_id)
    if name_id:
        data["modio_name_id"] = name_id
    data.pop("fetch_error", None)
    if cover_rel:
        data["cover_path"] = cover_rel

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
) -> None:
    """Persist Mod.io title / urls to SQLite and clear stale user display overrides."""
    from core.db_manager import get_db

    db = get_db()
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
        db.update_mod_platform_info(mid, **kwargs)
    except ValueError:
        db.update_mod_platform_info(
            mid,
            platform=PLATFORM_MODIO,
            source_url=canonical,
            title=title,
            description=description,
            preview_url=preview_url,
        )
    # Mod.io refresh owns the visible name — clear user override so Detail /
    # Library pick up mods.title / metadata display_name.
    try:
        db.update_mod_user_metadata(mid, {"display_name": ""})
    except Exception:  # noqa: BLE001
        logger.debug("clear display_name override skipped for %s", mid, exc_info=True)


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
) -> MetadataRefreshResult:
    """
    Fetch Mod.io Mod Object and persist name / description / cover / folder rename.
    """
    mid = str(mod_id).strip()
    folder = Path(managed_path).expanduser().resolve()
    logger.info("Starting Mod.io metadata refresh for mod_id=%s path=%s", mid, folder)
    if not folder.is_dir():
        logger.error("Mod.io refresh aborted: directory missing (%s)", folder)
        return MetadataRefreshResult(
            mod_id=mid,
            success=False,
            managed_path=folder,
            old_path=folder,
            error=f"Mod 目录不存在: {folder}",
        )

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
            error="Mod.io URL 无效。请在编辑信息中填写类似 "
            "https://mod.io/g/anno-1800/m/harborlife 的官方链接。",
        )

    logger.info(
        "Parsed Mod.io URL: game=%s mod=%s",
        parts.game_slug,
        parts.mod_name_id,
    )

    local_mod_id, local_game_id = _read_local_modio_ids(folder)
    # Numeric external_id from DB can also be the Mod.io mod id.
    try:
        from core.db_manager import get_db

        info = get_db().get_mod_display_info(mid)
        if info is not None:
            ext = str(info.external_id or "").strip()
            if ext.isdigit() and local_mod_id <= 0:
                local_mod_id = int(ext)
            if not url and info.source_url:
                url = info.source_url
                parts = parse_modio_url(url) or parts
    except Exception:  # noqa: BLE001
        logger.exception("Mod.io refresh: failed reading local DB identity")
        info = None

    owns = client is None
    api = client or ModioClient()
    try:
        try:
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
                error=str(exc),
            )

        if not details.name.strip():
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                error="Mod.io API 未返回 Mod 名称",
            )

        # Conflict check before mutating files (except we still allow metadata
        # update if rename is not needed).
        desired = sanitize_folder_name(details.name, fallback=folder.name)
        target_probe = (folder.parent / desired).resolve()
        if (
            desired
            and desired != folder.name
            and target_probe.exists()
            and target_probe != folder
        ):
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                title=details.name,
                error=(
                    f"目录名冲突：目标「{desired}」已存在，"
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
                error=str(exc),
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
            return MetadataRefreshResult(
                mod_id=mid,
                success=False,
                managed_path=folder,
                old_path=folder,
                title=details.name,
                error=format_directory_rename_error(
                    exc, source=folder, lock_summary=lock_summary
                ),
                cover_path="",
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
        )
        try:
            _update_modio_db_identity(
                mid,
                title=details.name,
                description=description,
                canonical=canonical,
                preview_url=details.logo_url,
                modio_mod_id=details.mod_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Mod.io DB update failed for %s: %s", mid, exc)

        if download_cover and details.logo_url:
            try:
                cover_rel = _download_cover(
                    api, details.logo_url, new_path, mod_id=mid
                )
                if cover_rel:
                    data = read_info_metadata_dict(new_path) or {}
                    data["cover_path"] = cover_rel
                    data["preview_url"] = details.logo_url
                    data["title"] = details.name
                    data["display_name"] = details.name
                    (new_path / INFO_DIR_NAME / "metadata.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    try:
                        from core.db_manager import get_db

                        get_db().update_mod_cover_path(mid, cover_rel)
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
