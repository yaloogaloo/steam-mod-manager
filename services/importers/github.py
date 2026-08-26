"""GitHub importer — URL + local scan, no GitHub API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.mod_platform import PLATFORM_GITHUB
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    ModImporter,
    require_import_context,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.source_files import build_github_mod_files


class GithubImporter(ModImporter):
    platform = PLATFORM_GITHUB

    def detect(self, value: str) -> bool:
        low = str(value or "").strip().lower()
        return "github.com" in low

    @staticmethod
    def parse_repo(url: str) -> str:
        text = str(url or "").strip().rstrip("/")
        if text.endswith(".git"):
            text = text[:-4]
        parsed = urlparse(text if "://" in text else f"https://{text}")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return text

    def import_mod(
        self,
        *,
        github_url: str = "",
        source_folder: str | Path = "",
        title: str = "",
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

        is_batch = bool(_kwargs.get("is_batch_mode"))
        suffix = str(_kwargs.get("external_id_suffix") or "").strip()
        url = str(github_url or "").strip()

        # Single-mod imports still require a GitHub URL (absolute red line).
        # Batch mode may leave the link empty and use a local placeholder id.
        if not url:
            if not (is_batch or suffix):
                return ImportResult(
                    success=False, error="缺少 GitHub URL", platform=self.platform
                )
            local_key = suffix or folder.name
            external_id = f"local/{local_key}"
            name = (title or "").strip() or local_key
            canonical = ""
        else:
            repo = self.parse_repo(url)
            if not repo or "/" not in repo:
                return ImportResult(
                    success=False,
                    error=f"无法解析仓库：{github_url}",
                    platform=self.platform,
                )
            external_id = f"{repo}#{suffix}" if suffix else repo
            name = (title or "").strip() or (
                suffix if suffix else repo.split("/")[-1]
            )
            canonical = url if url.startswith("http") else f"https://github.com/{repo}"

        db = self._database()
        from services.importers.duplicate_check import check_import_duplicate

        dup = check_import_duplicate(
            db,
            platform=PLATFORM_GITHUB,
            external_id=external_id,
            source_url=canonical,
            app_id=int(ctx.game_id or 0),
        )
        if dup is not None:
            return dup

        bundle = build_github_mod_files(
            folder,
            file_entries=_kwargs.get("file_entries"),
        )
        info = db.register_external_mod(
            platform=PLATFORM_GITHUB,
            external_id=external_id,
            source_url=canonical,
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
            platform=PLATFORM_GITHUB,
            external_id=external_id,
            source_url=canonical,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=ctx.game_id,
            game_name=ctx.game_name,
        )
