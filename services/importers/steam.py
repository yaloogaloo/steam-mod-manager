"""Steam Workshop importer — preserves existing Workshop ID identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.mod_platform import PLATFORM_STEAM, ModFilesBundle, steam_workshop_url
from core.models import ModMetadata
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    ModImporter,
    coerce_import_context,
    is_invalid_game_name,
    resolve_game_for_import,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.source_files import apply_steam_file_semantics


class SteamImporter(ModImporter):
    platform = PLATFORM_STEAM

    def detect(self, value: str) -> bool:
        text = str(value or "").strip()
        if text.isdigit():
            return True
        low = text.lower()
        return "steamcommunity.com" in low and "filedetails" in low

    def import_mod(
        self,
        *,
        workshop_id: str | int = "",
        title: str = "",
        app_id: int = 0,
        source_folder: str | Path = "",
        library_root: str | Path = "",
        game_name: str = "",
        context: ImportContext | dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ImportResult:
        mid = str(workshop_id or "").strip()
        if not mid:
            return ImportResult(
                success=False, error="缺少 Workshop ID", platform=self.platform
            )
        if not mid.isdigit():
            if "id=" in mid:
                mid = mid.split("id=", 1)[-1].split("&", 1)[0].strip()
            if not mid.isdigit():
                return ImportResult(
                    success=False,
                    error=f"无效的 Workshop ID：{workshop_id}",
                    platform=self.platform,
                )

        ctx = coerce_import_context(context, game_id=app_id, game_name=game_name, app_id=app_id)
        resolved = resolve_game_for_import(
            context=ctx,
            game_name=game_name,
            app_id=int(app_id or 0),
            require_context=False,
        )
        if isinstance(resolved, ImportResult):
            return resolved
        resolved_app_id, resolved_game = resolved
        # Prefer explicit Steam app_id when provided; else ImportContext fallback.
        if int(app_id or 0) > 0:
            resolved_app_id = int(app_id)
        elif ctx is not None and ctx.game_id > 0:
            resolved_app_id = ctx.game_id
        if ctx is not None and ctx.game_name and (
            not game_name.strip() or is_invalid_game_name(game_name)
        ):
            resolved_game = ctx.game_name

        db = self._database()
        url = steam_workshop_url(mid)
        from services.importers.duplicate_check import check_import_duplicate

        dup = check_import_duplicate(
            db,
            platform=self.platform,
            workshop_id=mid,
            external_id=mid,
            source_url=url,
            app_id=int(resolved_app_id or 0),
        )
        if dup is not None:
            return dup

        folder = Path(str(source_folder or "")).expanduser() if source_folder else None
        if folder is not None and str(source_folder).strip() and not folder.is_dir():
            return ImportResult(
                success=False,
                error="Mod目录不存在",
                platform=self.platform,
            )

        name = (title or "").strip() or f"Unknown_Mod_{mid}"
        from services.identity_service import create_mod_identity

        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            workshop_id=mid,
            external_id=mid,
            source_url=url,
            title=name,
            app_id=int(resolved_app_id or 0),
            game_name=resolved_game,
        )
        info = db.get_mod_display_info(created.mod_id)
        if info is None:
            return ImportResult(
                success=False, error="Steam identity create failed", platform=self.platform
            )
        # Single Workshop Content semantics: empty bundle → deploy whole Mod.
        # Optional file_entries (tests / advanced) are annotated as steam_content.
        raw_entries = _kwargs.get("file_entries")
        if raw_entries:
            from core.mod_platform import ModFileEntry

            files = [
                e if isinstance(e, ModFileEntry) else ModFileEntry.from_dict(e)
                for e in raw_entries
            ]
            bundle = apply_steam_file_semantics(ModFilesBundle(files=files))
        else:
            bundle = ModFilesBundle()
        db.set_mod_files(mid, bundle)

        managed = ""
        if library_root:
            dest = materialize_imported_mod(
                library_root=library_root,
                mod_id=mid,
                title=name,
                game_name=resolved_game,
                source_folder=folder if folder and folder.is_dir() else None,
                cover_source=_kwargs.get("cover_source") or _kwargs.get("cover_path"),
                context=ctx,
                allow_invalid_game_name=ctx is None,
            )
            managed = str(dest)

        return ImportResult(
            success=True,
            mod_id=mid,
            platform=PLATFORM_STEAM,
            external_id=mid,
            source_url=url,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=int(resolved_app_id or 0),
            game_name=resolved_game,
        )
