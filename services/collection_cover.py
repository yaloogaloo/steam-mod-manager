"""Collection Cover filesystem lifecycle — independent of Mod ``.info`` covers.

Never calls ``apply_cover_to_mod`` / ``install_cover_file``.
Never writes Mod sidecar, ``mods.cover_path``, Identity, WH3, or Deploy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from core.paths import COLLECTION_COVERS_DIR_NAME, collection_covers_dir
from services.importers.image_picker import IMAGE_SUFFIXES, validate_cover_image

logger = logging.getLogger(__name__)

_INCOMING_MARK = ".incoming"


@dataclass(frozen=True)
class StagedCollectionCover:
    collection_id: int
    relative_path: str
    new_absolute: Path
    final_absolute: Path
    old_absolute: Path | None
    same_extension: bool


@dataclass(frozen=True)
class MemberCoverChoice:
    internal_id: str
    name: str
    cover_file: Path | None


def relative_collection_cover_path(collection_id: int | str, suffix: str) -> str:
    cid = int(str(collection_id).strip())
    ext = str(suffix or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if ext not in IMAGE_SUFFIXES:
        ext = ".png"
    return f"{COLLECTION_COVERS_DIR_NAME}/{cid}{ext}"


def absolute_collection_cover_path(relative: str) -> Path:
    rel = str(relative or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("collection cover_path must be non-empty")
    path = Path(rel)
    if path.is_absolute():
        raise ValueError("collection cover_path must be relative to data_dir()")
    from core.paths import data_dir

    root = data_dir().resolve()
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("collection cover_path escapes data_dir()") from exc
    return resolved


def stored_cover_file(relative: str) -> Path | None:
    rel = str(relative or "").strip()
    if not rel:
        return None
    try:
        path = absolute_collection_cover_path(rel)
    except ValueError:
        return None
    try:
        if path.is_file():
            return path
    except OSError:
        return None
    return None


def own_cover_files(collection_id: int | str) -> list[Path]:
    """Only ``<collection_id>.<ext>`` (and leftover incoming temps) in covers dir."""
    cid = int(str(collection_id).strip())
    root = collection_covers_dir()
    found: list[Path] = []
    for ext in sorted(IMAGE_SUFFIXES):
        candidate = root / f"{cid}{ext}"
        if candidate.is_file():
            found.append(candidate)
        incoming = root / f"{cid}{_INCOMING_MARK}{ext}"
        if incoming.is_file():
            found.append(incoming)
    return found


def validate_and_decode_cover(source: str | Path) -> Path:
    """Suffix/existence check plus QImage decode. Does not copy."""
    src = validate_cover_image(source)
    from PySide6.QtGui import QImage

    image = QImage(str(src))
    if image.isNull():
        raise ValueError(f"无法解码封面图片: {src}")
    return src


def _unlink_quiet(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError as exc:
        logger.warning("failed to remove collection cover %s: %s", path, exc)


def stage_collection_cover(
    collection_id: int | str,
    source: str | Path,
    *,
    current_relative: str = "",
) -> StagedCollectionCover:
    """Write the new cover file. Caller updates DB, then commit or rollback."""
    cid = int(str(collection_id).strip())
    src = validate_and_decode_cover(source)
    ext = src.suffix.lower()
    relative = relative_collection_cover_path(cid, ext)
    final_abs = absolute_collection_cover_path(relative)
    old_rel = str(current_relative or "").strip().replace("\\", "/")
    old_abs = stored_cover_file(old_rel) if old_rel else None
    same_extension = bool(
        old_abs is not None and old_abs.suffix.lower() == ext and old_abs.exists()
    )
    collection_covers_dir()
    if same_extension:
        incoming = final_abs.with_name(f"{cid}{_INCOMING_MARK}{ext}")
        _copy_file(src, incoming)
        return StagedCollectionCover(
            collection_id=cid,
            relative_path=relative,
            new_absolute=incoming,
            final_absolute=final_abs,
            old_absolute=old_abs,
            same_extension=True,
        )
    _copy_file(src, final_abs)
    return StagedCollectionCover(
        collection_id=cid,
        relative_path=relative,
        new_absolute=final_abs,
        final_absolute=final_abs,
        old_absolute=old_abs if old_abs != final_abs else None,
        same_extension=False,
    )


def commit_staged_cover(staged: StagedCollectionCover) -> Path:
    """Promote staged file and remove previous extension. Returns final path."""
    if staged.same_extension:
        os.replace(staged.new_absolute, staged.final_absolute)
        return staged.final_absolute
    if staged.old_absolute is not None and staged.old_absolute != staged.final_absolute:
        _unlink_quiet(staged.old_absolute)
    for leftover in own_cover_files(staged.collection_id):
        if leftover.resolve() == staged.final_absolute.resolve():
            continue
        _unlink_quiet(leftover)
    return staged.final_absolute


def rollback_staged_cover(staged: StagedCollectionCover) -> None:
    """Drop the new file; leave the previous cover in place."""
    if staged.same_extension:
        _unlink_quiet(staged.new_absolute)
        return
    if staged.new_absolute != staged.old_absolute:
        _unlink_quiet(staged.new_absolute)


def delete_collection_cover_files(collection_id: int | str) -> list[Path]:
    """Delete only this Collection's cover files. Returns removed absolute paths."""
    removed: list[Path] = []
    for path in own_cover_files(collection_id):
        abs_path = path.resolve() if path.exists() else path
        _unlink_quiet(path)
        removed.append(abs_path)
    return removed


def member_cover_file(
    internal_id: int | str,
    *,
    db=None,
) -> Path | None:
    """Resolve a member Mod's Cover file. Read-only — never writes Mod Cover."""
    from core.db_manager import DatabaseManager, get_db
    from services.cover_loader import resolve_cover_path

    database: DatabaseManager = db if db is not None else get_db()
    from services.mod_library_cache import dal_mod_pk

    mid = dal_mod_pk(internal_id)
    if not mid.isdigit():
        return None
    info = database.get_mod_display_info(mid)
    cover_ref = str(info.cover_path or "").strip() if info is not None else ""
    last_known = ""
    with database._lock:
        row = database._conn.execute(
            "SELECT last_known_path FROM mods WHERE mod_id = ?",
            (int(mid),),
        ).fetchone()
    if row is not None:
        last_known = str(row["last_known_path"] or "").strip()
    if not last_known and not cover_ref:
        return None
    managed = last_known if last_known else "__collection_cover_no_folder__"
    found = resolve_cover_path(managed, cover_ref)
    if found is not None and found.is_file():
        return found
    return None


def list_member_cover_choices(
    collection_id: int | str,
    *,
    db=None,
) -> list[MemberCoverChoice]:
    """Members of this Collection only — names + optional cover files."""
    from core.db_manager import DatabaseManager, get_db
    from services.collection import list_collection_member_ids

    database: DatabaseManager = db if db is not None else get_db()
    out: list[MemberCoverChoice] = []
    for mid in list_collection_member_ids(collection_id, db=database):
        info = database.get_mod_display_info(mid)
        name = str(info.display_name or "").strip() if info is not None else ""
        if not name:
            name = mid
        out.append(
            MemberCoverChoice(
                internal_id=str(mid),
                name=name,
                cover_file=member_cover_file(mid, db=database),
            )
        )
    return out


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    dest.write_bytes(data)
