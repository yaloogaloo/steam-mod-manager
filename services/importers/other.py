"""「其它」来源 — 纯本地 Mod，源链接 / 离线页面均可选。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.mod_platform import PLATFORM_OTHER
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    ModImporter,
    require_import_context,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.source_files import build_other_mod_files


class OtherImporter(ModImporter):
    platform = PLATFORM_OTHER

    def detect(self, value: str) -> bool:
        key = str(value or "").strip().casefold()
        return key in {"other", "其它", "其他", "local", "manual"}

    def import_mod(
        self,
        *,
        source_folder: str | Path = "",
        title: str = "",
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

        url = str(source_url or "").strip()
        suffix = str(_kwargs.get("external_id_suffix") or "").strip()
        local_key = suffix or folder.name
        external_id = f"local/{local_key}"
        name = (title or "").strip() or local_key

        db = self._database()
        from services.importers.duplicate_check import check_import_duplicate

        dup = check_import_duplicate(
            db,
            platform=PLATFORM_OTHER,
            external_id=external_id,
            source_url=url,
        )
        if dup is not None:
            return dup

        bundle = build_other_mod_files(
            folder,
            file_entries=_kwargs.get("file_entries"),
        )
        info = db.register_external_mod(
            platform=PLATFORM_OTHER,
            external_id=external_id,
            source_url=url,
            title=name,
            app_id=ctx.game_id,
            game_name=ctx.game_name,
            mod_files=bundle,
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
            platform=PLATFORM_OTHER,
            external_id=external_id,
            source_url=url,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=ctx.game_id,
            game_name=ctx.game_name,
        )
