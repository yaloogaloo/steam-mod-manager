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
from services.importers.local_scanner import scan_mod_directory
from services.importers.materialize import materialize_imported_mod


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

        url = str(github_url or "").strip()
        if not url:
            return ImportResult(
                success=False, error="缺少 GitHub URL", platform=self.platform
            )
        repo = self.parse_repo(url)
        if not repo or "/" not in repo:
            return ImportResult(
                success=False,
                error=f"无法解析仓库：{github_url}",
                platform=self.platform,
            )

        folder = Path(str(source_folder or "")).expanduser()
        if not str(source_folder or "").strip() or not folder.is_dir():
            return ImportResult(
                success=False,
                error="Mod目录不存在",
                platform=self.platform,
            )

        db = self._database()
        existing = db.find_mod_by_external(PLATFORM_GITHUB, repo)
        if existing is not None:
            return ImportResult(
                success=False,
                error="该Mod已经存在",
                platform=self.platform,
                external_id=repo,
                mod_id=existing.mod_id,
                source_url=existing.source_url,
            )

        bundle = scan_mod_directory(folder)
        name = (title or "").strip() or repo.split("/")[-1]
        canonical = url if url.startswith("http") else f"https://github.com/{repo}"
        info = db.register_external_mod(
            platform=PLATFORM_GITHUB,
            external_id=repo,
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
                cover_flat_roots=_kwargs.get("cover_flat_roots"),
                cover_search_roots=_kwargs.get("cover_search_roots"),
                context=ctx,
            )
            managed = str(dest)

        return ImportResult(
            success=True,
            mod_id=info.mod_id,
            platform=PLATFORM_GITHUB,
            external_id=repo,
            source_url=canonical,
            title=info.display_name,
            display=info,
            files_count=len(bundle.files),
            managed_path=managed,
            game_id=ctx.game_id,
            game_name=ctx.game_name,
        )
