"""Validate and install user-specified Mod cover images (no auto-discovery)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from services.file_ops import (
    COVER_BASENAME,
    INFO_DIR_NAME,
    LEGACY_COVER_BASENAME,
    ModFileManager,
)

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".jfif", ".png", ".webp"}
# Alias used by local_scanner / cleanup — display assets, never Mod packages.
IMAGE_EXTENSIONS = IMAGE_SUFFIXES | {".gif"}  # gif still excluded from mod_files


def is_image_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def validate_cover_image(path: str | Path) -> Path:
    """Ensure *path* is an existing supported cover image."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise FileNotFoundError(f"封面图片不存在: {candidate}")
    suffix = candidate.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(
            f"仅支持 png / jpg / jpeg / jfif / webp，收到: {suffix or '(无后缀)'}"
        )
    return candidate.resolve()


def suggest_sibling_covers(source: str | Path) -> list[Path]:
    """
    Suggest same-stem images next to an archive (prompt only — never auto-bind).

    ``PalAnalyzer.zip`` → ``PalAnalyzer.png`` / ``.jpg`` / ``.webp`` in the same folder.
    """
    path = Path(source).expanduser()
    if not path.is_file():
        return []
    parent = path.parent
    stem = path.stem
    found: list[Path] = []
    for ext in (".png", ".jpg", ".jpeg", ".jfif", ".webp"):
        candidate = parent / f"{stem}{ext}"
        if candidate.is_file():
            found.append(candidate.resolve())
    return found


def relative_cover_path(managed_path: str | Path, cover_file: str | Path) -> str:
    """Return ``.info/cover.ext`` style path relative to the Mod folder."""
    managed = Path(managed_path)
    cover = Path(cover_file)
    try:
        rel = cover.resolve().relative_to(managed.resolve())
        return rel.as_posix()
    except ValueError:
        return f"{INFO_DIR_NAME}/{cover.name}"


def resolve_cover_file(
    managed_path: str | Path,
    cover_path: str | None = "",
) -> Path | None:
    """
    Resolve an explicit cover reference.

    Does **not** scan Mod content for random images — only *cover_path* and
    ``.info/cover.*`` / legacy ``.info/preview.*``.
    """
    managed = Path(managed_path)
    raw = str(cover_path or "").strip()
    if raw:
        direct = Path(raw)
        if direct.is_file():
            return direct.resolve()
        nested = managed / raw
        if nested.is_file():
            return nested.resolve()
    return ModFileManager(managed.parent).find_local_cover(managed)


def install_cover_file(candidate: Path | str, managed_path: str | Path) -> Path | None:
    """Copy *candidate* into ``managed_path/.info/cover.<ext>``."""
    try:
        src = validate_cover_image(candidate)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Invalid cover image %s: %s", candidate, exc)
        return None

    dest_root = Path(managed_path)
    info = dest_root / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or ".png"
    if ext not in IMAGE_SUFFIXES:
        ext = ".png"
    target = info / f"{COVER_BASENAME}{ext}"

    for pattern in (f"{COVER_BASENAME}.*", f"{LEGACY_COVER_BASENAME}.*"):
        for old in info.glob(pattern):
            if old.resolve() == target.resolve():
                continue
            try:
                old.unlink()
            except OSError:
                pass
    try:
        shutil.copy2(src, target)
    except OSError as exc:
        logger.warning("Failed to install cover %s -> %s: %s", src, target, exc)
        return None
    return target


def apply_cover_to_mod(
    managed_path: str | Path,
    cover_source: str | Path,
    *,
    mod_id: str | int = "",
    update_db: bool = True,
    sync_backup: bool = True,
    mark_user_override: bool = True,
) -> str:
    """
    Install cover and return relative ``cover_path`` (``''`` on failure).

    Also updates ``.info/metadata.json`` and optionally SQLite ``mods.cover_path``.
    """
    dest = Path(managed_path)
    installed = install_cover_file(cover_source, dest)
    if installed is None:
        return ""
    rel = relative_cover_path(dest, installed)

    mgr = ModFileManager(dest.parents[1] if len(dest.parts) > 1 else dest.parent)
    meta = mgr.load_metadata(dest)
    if meta is not None:
        meta.cover_path = rel
        mgr.save_metadata(meta, dest, sync_backup=False)

    if update_db and str(mod_id).strip():
        try:
            from core.db_manager import get_db
            from services.metadata_ownership import FIELD_COVER

            get_db().update_mod_cover_path(mod_id, rel)
            if mark_user_override:
                get_db().set_user_override_field(
                    mod_id, FIELD_COVER, overridden=True
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("update_mod_cover_path failed: %s", exc)
    if sync_backup:
        try:
            from services.metadata_backup_sync import sync_after_metadata_change

            sync_after_metadata_change(mod_id, dest, "cover_change")
        except Exception:  # noqa: BLE001
            pass
    return rel


def cleanup_old_auto_cover(
    *,
    library_root: str | Path | None = None,
    db=None,
) -> dict[str, int]:
    """
    One-shot migration: keep existing ``.info/cover.*`` / ``preview.*`` bindings.

    Never re-scans Mod package images. Clears DB ``cover_path`` only when the
    referenced file is missing and no ``.info`` cover exists.
    """
    from core.db_manager import DatabaseManager, get_db
    from core.paths import default_mod_library

    database: DatabaseManager = db if db is not None else get_db()
    root = Path(library_root) if library_root else default_mod_library()
    kept = 0
    bound = 0
    cleared = 0

    mgr = ModFileManager(root)
    for folder in mgr.list_managed_mods():
        meta = mgr.load_metadata(folder)
        mid = (meta.published_file_id if meta else "") or ""
        existing = str((meta.cover_path if meta else "") or "").strip()
        resolved = resolve_cover_file(folder, existing)
        if resolved is not None and resolved.is_file():
            rel = relative_cover_path(folder, resolved)
            if meta is not None and meta.cover_path != rel:
                meta.cover_path = rel
                mgr.save_metadata(meta, folder)
                bound += 1
            else:
                kept += 1
            if mid:
                try:
                    database.update_mod_cover_path(mid, rel)
                except Exception:  # noqa: BLE001
                    pass
            continue

        # No valid cover — clear stale references; do not invent one.
        if meta is not None and existing:
            meta.cover_path = ""
            mgr.save_metadata(meta, folder)
            cleared += 1
        if mid:
            try:
                database.update_mod_cover_path(mid, "")
            except Exception:  # noqa: BLE001
                pass

    return {"kept": kept, "bound": bound, "cleared": cleared}
