"""Background QThread for Mod Import dialog (folder or archive)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from core.db_manager import get_db
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from services.importers.archive import ArchiveImporter, is_archive_path
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportContext, ImportResult, coerce_import_context
from services.importers.nexus import NexusImporter, parse_nexus_id
from services.importers.steam import SteamImporter


class ImportWorker(QThread):
    """Run platform / archive import off the UI thread."""

    progress_changed = Signal(str)
    import_finished = Signal(object)  # ImportResult
    import_failed = Signal(str)

    def __init__(
        self,
        *,
        platform: str,
        library_root: str | Path,
        params: dict[str, Any],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.platform = str(platform or PLATFORM_STEAM).strip().lower()
        self.library_root = Path(library_root)
        self.params = dict(params)

    def run(self) -> None:
        try:
            result = self._do_import()
            if self.isInterruptionRequested():
                return
            if result.success:
                self.import_finished.emit(result)
            else:
                self.import_failed.emit(result.error or "导入失败")
        except Exception as exc:  # noqa: BLE001
            self.import_failed.emit(str(exc))

    def _emit_progress(self, message: str) -> None:
        if not self.isInterruptionRequested():
            self.progress_changed.emit(message)

    def _context(self) -> ImportContext | None:
        params = self.params
        return coerce_import_context(
            params.get("context"),
            game_id=int(params.get("game_id") or params.get("app_id") or 0),
            game_name=str(params.get("game_name") or ""),
            app_id=int(params.get("app_id") or params.get("game_id") or 0),
        )

    def _do_import(self) -> ImportResult:
        db = get_db()
        params = self.params
        source = str(params.get("source_path") or "").strip()
        title = str(params.get("title") or "").strip()
        use_archive = bool(params.get("use_archive")) or is_archive_path(source)
        context = self._context()

        if use_archive:
            self._emit_progress("正在解压...")
            return ArchiveImporter(db=db).import_mod(
                archive_path=source,
                platform=self.platform,
                library_root=self.library_root,
                title=title,
                workshop_id=str(params.get("workshop_id") or ""),
                nexus_url=str(params.get("nexus_url") or ""),
                nexus_id=str(params.get("nexus_id") or ""),
                github_url=str(params.get("github_url") or ""),
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                on_progress=self._emit_progress,
            )

        self._emit_progress("正在扫描Mod文件...")
        if self.platform == PLATFORM_STEAM:
            self._emit_progress("正在导入...")
            return SteamImporter(db=db).import_mod(
                workshop_id=str(params.get("workshop_id") or source),
                title=title,
                source_folder=str(params.get("folder") or ""),
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
            )
        if self.platform == PLATFORM_GITHUB:
            self._emit_progress("正在导入...")
            return GithubImporter(db=db).import_mod(
                github_url=str(params.get("github_url") or ""),
                source_folder=str(params.get("folder") or source),
                title=title,
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
            )
        # Nexus
        raw = str(params.get("nexus_url") or "")
        nexus_id = str(params.get("nexus_id") or parse_nexus_id(raw, ""))
        self._emit_progress("正在导入...")
        return NexusImporter(db=db).import_mod(
            source_folder=str(params.get("folder") or source),
            title=title,
            nexus_url=raw,
            nexus_id=nexus_id,
            library_root=self.library_root,
            game_name=str(params.get("game_name") or ""),
            app_id=int(params.get("game_id") or params.get("app_id") or 0),
            context=context,
        )
