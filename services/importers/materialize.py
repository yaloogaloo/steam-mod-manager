"""Copy / stub an imported Mod into the managed library so it appears in UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.models import ModMetadata
from core.sanitize import sanitize_folder_name
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.importers.image_picker import apply_cover_to_mod
from services.importers.importer_base import (
    MISSING_GAME_CONTEXT,
    ImportContext,
    coerce_import_context,
    is_invalid_game_name,
)


def find_managed_mod_path(library_root: str | Path, mod_id: str | int) -> Path | None:
    """Locate ``…/<mod>/.info/mod.json`` whose published_file_id matches *mod_id*."""
    mid = str(mod_id).strip()
    root = Path(library_root)
    if not mid or not root.is_dir():
        return None
    for info_dir in root.rglob(INFO_DIR_NAME):
        if not info_dir.is_dir():
            continue
        meta_path = info_dir / METADATA_FILENAME
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if str(data.get("published_file_id") or "").strip() == mid:
            return info_dir.parent
    return None


def resolve_materialize_game_name(
    game_name: str = "",
    *,
    context: ImportContext | dict[str, Any] | None = None,
    allow_invalid_game_name: bool = False,
) -> str:
    """
    Pick a library game folder name.

    Platform names (GitHub / Nexus Mods / …) are rejected unless
    *allow_invalid_game_name* (legacy Steam-only stub without context).
    """
    ctx = coerce_import_context(context)
    name = str(game_name or "").strip()
    if is_invalid_game_name(name):
        name = ""
    if not name and ctx is not None:
        name = ctx.game_name.strip()
    if is_invalid_game_name(name):
        name = ""
    if name:
        return name
    if allow_invalid_game_name:
        fallback = str(game_name or "").strip() or "Steam Workshop"
        return fallback
    raise ValueError(MISSING_GAME_CONTEXT)


def materialize_imported_mod(
    *,
    library_root: str | Path,
    mod_id: str | int,
    title: str,
    game_name: str = "",
    source_folder: str | Path | None = None,
    cover_source: str | Path | None = None,
    context: ImportContext | dict[str, Any] | None = None,
    allow_invalid_game_name: bool = False,
    copy_ignore: list[Path] | tuple[Path, ...] | None = None,
    # Deprecated kwargs — ignored (legacy auto cover scan hooks).
    cover_flat_roots=None,
    cover_search_roots=None,
) -> Path:
    """
    Ensure a filesystem folder exists under the managed library.

    - If *source_folder* is a directory → copy into ``<game>/<title>/``
    - Else → create an empty stub folder with ``.info/mod.json`` only
    - Cover images / ``.mhtml`` discovered under the source (or passed via
      *copy_ignore*) are excluded from the managed copy
    - If *cover_source* is set → copy into ``.info/cover.<ext>`` and set cover_path

    Does not change ``.info`` schema — writes the existing ``mod.json`` shape.
    """
    del cover_flat_roots, cover_search_roots
    mid = str(mod_id).strip()
    name = (title or "").strip() or f"Unknown_Mod_{mid}"
    game = sanitize_folder_name(
        resolve_materialize_game_name(
            game_name,
            context=context,
            allow_invalid_game_name=allow_invalid_game_name,
        ),
        fallback="Imported",
    )
    if is_invalid_game_name(game) and not allow_invalid_game_name:
        raise ValueError(MISSING_GAME_CONTEXT)

    mgr = ModFileManager(library_root)
    meta = ModMetadata(
        published_file_id=mid,
        title=name,
        game_name=game,
        source_path=str(source_folder) if source_folder else None,
        cover_path="",
    )
    src = Path(str(source_folder)).expanduser() if source_folder else None
    ignore_files: list[Path] = list(copy_ignore or ())
    cover_raw = str(cover_source or "").strip()
    effective_cover: Path | None = Path(cover_raw).expanduser() if cover_raw else None

    if src is not None and src.is_dir():
        # Auto-extract cover / mhtml sidecars; never raise when missing.
        # Offline MHTML is ignored from the Mod tree copy here; ImportWorker /
        # Detail attach it once via attach_nexus_offline_page (single NexusCleaner).
        try:
            from services.importers.directory_batch import extract_directory_sidecars

            sidecars = extract_directory_sidecars(src)
            for path in sidecars.ignore_paths:
                ignore_files.append(path)
            if effective_cover is None and sidecars.cover is not None:
                effective_cover = sidecars.cover
        except Exception:  # noqa: BLE001
            pass
        dest = mgr.copy_mod(
            meta,
            overwrite_existing=False,
            ignore_files=ignore_files or None,
        )
    else:
        dest = mgr.allocate_destination(meta)
        dest.mkdir(parents=True, exist_ok=True)
    meta.managed_path = str(dest)
    mgr.save_metadata(meta, dest, sync_backup=False)

    if effective_cover is not None:
        rel = apply_cover_to_mod(
            dest, effective_cover, mod_id=mid, update_db=True, sync_backup=False
        )
        meta.cover_path = rel
        mgr.save_metadata(meta, dest, sync_backup=False)
    else:
        try:
            from core.db_manager import get_db

            get_db().update_mod_cover_path(mid, "")
        except Exception:  # noqa: BLE001
            pass

    # Portable snapshot for folder-copy reimport.
    try:
        from services.info_sidecar import write_sidecar_for_mod

        write_sidecar_for_mod(dest, mid, sync_backup=False)
    except Exception:  # noqa: BLE001
        pass

    # Empty dir / empty archive payloads are allowed — mark missing content.
    try:
        from services.file_ops import apply_missing_content_marker

        apply_missing_content_marker(dest, sync_backup=False)
    except Exception:  # noqa: BLE001
        pass

    try:
        from services.metadata_backup_sync import sync_after_metadata_change

        sync_after_metadata_change(mid, dest, "import")
    except Exception:  # noqa: BLE001
        pass

    return dest
