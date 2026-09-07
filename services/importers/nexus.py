"""Nexus Mods manual folder importer (multi-file → one Mod).

Identity flow (do not invert):
    Nexus Mod ID → external_id → workspace_id
Internal PK is allocated separately and must never become Workspace ID.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.mod_platform import PLATFORM_NEXUS
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    ModImporter,
    require_import_context,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.source_files import build_nexus_mod_files


def parse_nexus_id(nexus_url: str = "", nexus_id: str = "") -> str:
    ext = str(nexus_id or "").strip()
    if ext:
        return ext.split("?")[0].strip()
    url = str(nexus_url or "").strip()
    if not url:
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "mods" in parts:
        try:
            idx = parts.index("mods")
            return parts[idx + 1].split("?")[0]
        except (ValueError, IndexError):
            return ""
    if url.isdigit():
        return url
    return ""


def parse_nexus_game(nexus_url: str = "") -> str:
    """Extract Nexus URL game slug (informational only — not used as library game)."""
    url = str(nexus_url or "").strip()
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "mods" in parts:
        idx = parts.index("mods")
        if idx > 0:
            return parts[idx - 1]
    return ""


def is_valid_nexus_mod_id(value: str = "") -> bool:
    """True when *value* is a numeric Nexus Mod ID (not a local folder placeholder)."""
    return str(value or "").strip().isdigit()


def local_nexus_external_id(folder_name: str) -> str:
    """Internal placeholder external_id for imports without a real Nexus Mod ID."""
    name = str(folder_name or "").strip() or "unknown"
    return f"local/{name}"


def local_directory_external_id(folder: str | Path) -> str:
    """
    REMOVED as an identity key — path must never become external_id.

    Returns empty string so callers fall through to official URL / Sync-only
    workshop identity. Kept as a named stub so import call sites fail closed.
    """
    _ = folder
    return ""


class NexusImporter(ModImporter):
    platform = PLATFORM_NEXUS

    def detect(self, value: str) -> bool:
        text = str(value or "").strip()
        low = text.lower()
        if "nexusmods.com" in low:
            return True
        return text.isdigit()

    def import_mod(
        self,
        *,
        source_folder: str | Path = "",
        title: str = "",
        nexus_url: str = "",
        nexus_id: str = "",
        app_id: int = 0,
        library_root: str | Path = "",
        game_name: str = "",
        context: ImportContext | dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ImportResult:
        required = require_import_context(
            context, game_id=app_id, game_name=game_name, app_id=app_id
        )
        if isinstance(required, ImportResult):
            return required
        ctx = required

        folder = Path(str(source_folder or "")).expanduser()
        if not str(source_folder or "").strip() or not folder.is_dir():
            return ImportResult(
                success=False,
                error="Mod目录不存在",
                platform=self.platform,
            )

        url = str(nexus_url or "").strip()
        ext = parse_nexus_id(url, nexus_id)
        is_batch = bool(_kwargs.get("is_batch_mode"))
        if url.isdigit() and not nexus_id:
            ext = url
            url = f"https://www.nexusmods.com/mods/{ext}"
        # Nexus external_id / workshop_id only from official URL digits.
        if not url:
            ext = ext if is_valid_nexus_mod_id(ext) else ""
        elif not ext:
            ext = parse_nexus_id(url, "")
        if not is_valid_nexus_mod_id(ext):
            from services.importers.identity_resolve import MISSING_OFFICIAL_IDENTITY

            return ImportResult(
                success=False,
                error=MISSING_OFFICIAL_IDENTITY,
                platform=self.platform,
                source_url=url,
            )
        if is_batch:
            # Batch: keep official id; never invent URLs.
            url = url if "nexusmods.com" in url.lower() else ""
        elif not url and is_valid_nexus_mod_id(ext):
            slug = parse_nexus_game(nexus_url) or ctx.game_name.replace(" ", "").lower()
            if slug and slug.lower() != "mods":
                url = f"https://www.nexusmods.com/{slug}/mods/{ext}"
            else:
                url = f"https://www.nexusmods.com/mods/{ext}"

        name = (title or "").strip() or folder.name
        # Canonical directory name is *name* (passed to materialize). For
        # Nexus Offline HTML, callers must supply the parsed Mod title here.
        # Empty Mod <random> stub folders are payload placeholders only.

        db = self._database()
        from services.importers.duplicate_check import check_import_duplicate

        dup = check_import_duplicate(
            db,
            platform=PLATFORM_NEXUS,
            external_id=ext,
            source_url=url,
            app_id=int(app_id or ctx.game_id or 0),
            title=name,
        )
        if dup is not None:
            return dup

        bundle = build_nexus_mod_files(
            folder,
            file_entries=_kwargs.get("file_entries"),
        )
        from services.identity_service import create_mod_identity

        try:
            created = create_mod_identity(
                db,
                platform=PLATFORM_NEXUS,
                external_id=ext,
                source_url=url,
                title=name,
                app_id=ctx.game_id,
                game_name=ctx.game_name,
                mod_files=bundle,
            )
        except ValueError as exc:
            return ImportResult(
                success=False,
                error=str(exc) or "Nexus identity conflict",
                platform=PLATFORM_NEXUS,
                external_id=ext,
                source_url=url,
            )
        info = db.get_mod_display_info(created.mod_id)
        if info is None:
            return ImportResult(
                success=False, error="Nexus identity create failed", platform=PLATFORM_NEXUS
            )

        managed = ""
        if library_root:
            dest = materialize_imported_mod(
                library_root=library_root,
                mod_id=info.mod_id,
                title=name,
                game_name=ctx.game_name,
                source_folder=folder,
                cover_source=_kwargs.get("cover_source") or _kwargs.get("cover_path"),
                context=ctx,
            )
            managed = str(dest)

        return ImportResult(
            success=True,
            mod_id=info.mod_id,
            platform=PLATFORM_NEXUS,
            external_id=ext,
            source_url=url,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=ctx.game_id,
            game_name=ctx.game_name,
        )
