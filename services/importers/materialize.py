"""Copy / stub an imported Mod into the managed library so it appears in UI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.models import ModMetadata
from core.sanitize import sanitize_folder_name
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.importers.image_scanner import install_cover_from_source
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
    cover_flat_roots: Sequence[str | Path] | None = None,
    cover_search_roots: Sequence[str | Path] | None = None,
    context: ImportContext | dict[str, Any] | None = None,
    allow_invalid_game_name: bool = False,
) -> Path:
    """
    Ensure a filesystem folder exists under the managed library.

    - If *source_folder* is a directory → copy into ``<game>/<title>/``
    - Else → create an empty stub folder with ``.info/mod.json`` only
    - When source exists, install a primary cover into ``.info/preview.*``

    Does not change ``.info`` schema — writes the existing ``mod.json`` shape.
    """
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
    )
    src = Path(str(source_folder)).expanduser() if source_folder else None
    if src is not None and src.is_dir():
        dest = mgr.copy_mod(meta, overwrite_existing=False)
    else:
        dest = mgr.allocate_destination(meta)
        dest.mkdir(parents=True, exist_ok=True)
    meta.managed_path = str(dest)
    mgr.save_metadata(meta, dest)

    if src is not None and src.is_dir():
        cover = install_cover_from_source(
            src,
            dest,
            extra_flat_roots=cover_flat_roots,
            extra_recursive_roots=cover_search_roots,
        )
        if cover is not None:
            meta.cover_path = str(cover)
            mgr.save_metadata(meta, dest)

    return dest
