"""mod.io manual folder importer (Anno 1800 / Baldur's Gate 3 in UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.mod_platform import (
    MODIO_DEFAULT_URL,
    PLATFORM_MODIO,
    default_source_url_for_platform,
)
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    ModImporter,
    require_import_context,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.source_files import build_modio_mod_files


def parse_modio_id(modio_url: str = "", modio_id: str = "") -> str:
    ext = str(modio_id or "").strip()
    if ext:
        return ext.split("?")[0].strip()
    url = str(modio_url or "").strip()
    if not url:
        return ""
    if url.isdigit():
        return url
    parts = [p for p in urlparse(url).path.split("/") if p]
    # https://mod.io/g/anno-1800/m/slug-or-id
    if "m" in parts:
        try:
            idx = parts.index("m")
            return parts[idx + 1].split("?")[0].strip()
        except (ValueError, IndexError):
            return ""
    return ""


class ModioImporter(ModImporter):
    platform = PLATFORM_MODIO

    def detect(self, value: str) -> bool:
        low = str(value or "").strip().lower()
        return "mod.io" in low

    def import_mod(
        self,
        *,
        source_folder: str | Path = "",
        title: str = "",
        modio_url: str = "",
        modio_id: str = "",
        source_url: str = "",
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

        url = str(modio_url or source_url or "").strip()
        ext = parse_modio_id(url, modio_id)
        if not ext:
            ext = folder.name
        name = (title or "").strip() or folder.name
        is_batch = bool(_kwargs.get("is_batch_mode"))
        if is_batch:
            # Batch folder import: never invent / default source URLs.
            url = ""
        elif not url:
            url = default_source_url_for_platform(
                PLATFORM_MODIO,
                game_name=ctx.game_name,
                game_id=ctx.game_id,
            ) or MODIO_DEFAULT_URL

        db = self._database()
        from services.importers.duplicate_check import check_import_duplicate

        dup = check_import_duplicate(
            db,
            platform=PLATFORM_MODIO,
            external_id=ext,
            source_url=url,
            app_id=int(ctx.game_id or 0),
        )
        if dup is not None:
            return dup

        bundle = build_modio_mod_files(
            folder,
            file_entries=_kwargs.get("file_entries"),
        )
        from services.identity_service import create_mod_identity

        created = create_mod_identity(
            db,
            platform=PLATFORM_MODIO,
            external_id=ext,
            source_url=url,
            title=name,
            app_id=ctx.game_id,
            game_name=ctx.game_name,
            mod_files=bundle,
        )
        info = db.get_mod_display_info(created.mod_id)
        if info is None:
            return ImportResult(
                success=False, error="mod.io identity create failed", platform=PLATFORM_MODIO
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
            platform=PLATFORM_MODIO,
            external_id=ext,
            source_url=url,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=ctx.game_id,
            game_name=ctx.game_name,
        )
