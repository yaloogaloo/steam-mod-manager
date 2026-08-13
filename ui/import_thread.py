"""Background QThread for Mod Import dialog (folder or archive)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from core.db_manager import get_db
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    normalize_platform,
)
from services.importers.archive import ArchiveImporter, is_archive_path
from services.importers.directory_batch import (
    discover_mod_directories,
    extract_directory_sidecars,
)
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportContext, ImportResult, coerce_import_context
from services.importers.modio import ModioImporter
from services.importers.nexus import NexusImporter, parse_nexus_id
from services.importers.other import OtherImporter
from services.importers.steam import SteamImporter
from services.offline.manager import attach_nexus_offline_page


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
        self.platform = normalize_platform(platform)
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

    def _offline_html_path(self) -> str:
        params = self.params
        html = str(params.get("offline_html_path") or "").strip()
        if html:
            return html
        ctx = params.get("context")
        if isinstance(ctx, ImportContext):
            return str(ctx.offline_html_path or "").strip()
        if isinstance(ctx, dict):
            return str(ctx.get("offline_html_path") or "").strip()
        return ""

    def _context(self) -> ImportContext | None:
        params = self.params
        return coerce_import_context(
            params.get("context"),
            game_id=int(params.get("game_id") or params.get("app_id") or 0),
            game_name=str(params.get("game_name") or ""),
            app_id=int(params.get("app_id") or params.get("game_id") or 0),
            offline_html_path=self._offline_html_path() or None,
        )

    def _maybe_attach_nexus_offline(
        self,
        result: ImportResult,
        *,
        offline_html: str = "",
    ) -> ImportResult:
        """After a successful Nexus import, optionally attach user-saved HTML/MHTML."""
        if not result.success:
            return result
        if self.platform != PLATFORM_NEXUS:
            return result
        html = str(offline_html or "").strip() or self._offline_html_path()
        if not html:
            return result
        self._emit_progress("正在导入离线页面…")
        clean = self.params.get("offline_clean")
        if clean is None:
            clean = True
        try:
            attach_nexus_offline_page(
                result.mod_id,
                html,
                managed_path=result.managed_path or None,
                library_root=self.library_root,
                clean=bool(clean),
            )
        except Exception:  # noqa: BLE001
            # Missing / invalid offline pages must never fail the Mod import.
            pass
        return result

    def _resolve_folder_sidecars(
        self,
        folder: Path,
        *,
        cover_source: str = "",
        offline_html: str = "",
    ) -> tuple[str, str]:
        """Fill cover / offline from the Mod folder when the user did not pick them."""
        cover = str(cover_source or "").strip()
        offline = str(offline_html or "").strip()
        try:
            sidecars = extract_directory_sidecars(folder)
        except Exception:  # noqa: BLE001
            return cover, offline
        if not cover and sidecars.cover is not None:
            cover = str(sidecars.cover)
        if not offline and sidecars.offline_page is not None:
            offline = str(sidecars.offline_page)
        return cover, offline

    def _import_one_folder(
        self,
        folder: Path,
        *,
        title: str = "",
        cover_source: str = "",
        offline_html: str = "",
        batch: bool = False,
    ) -> ImportResult:
        db = get_db()
        params = self.params
        context = self._context()
        cover, offline = self._resolve_folder_sidecars(
            folder,
            cover_source=cover_source,
            offline_html=offline_html,
        )
        folder_title = (title or "").strip() or folder.name

        if self.platform == PLATFORM_STEAM:
            workshop_id = str(params.get("workshop_id") or "").strip()
            if batch and folder.name.isdigit():
                workshop_id = folder.name
            result = SteamImporter(db=db).import_mod(
                workshop_id=workshop_id,
                title=folder_title,
                source_folder=str(folder),
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover,
            )
            return result

        if self.platform == PLATFORM_GITHUB:
            github_url = str(params.get("github_url") or "").strip()
            result = GithubImporter(db=db).import_mod(
                github_url=github_url,
                source_folder=str(folder),
                title=folder_title,
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover,
                # Batch: allow unique external ids via folder name suffix.
                external_id_suffix=folder.name if batch else "",
                is_batch_mode=batch or bool(params.get("is_batch_mode")),
            )
            return result

        if self.platform == PLATFORM_MODIO:
            result = ModioImporter(db=db).import_mod(
                source_folder=str(folder),
                title=folder_title,
                modio_url=(
                    ""
                    if batch
                    else str(params.get("modio_url") or "")
                ),
                modio_id=(
                    folder.name if batch else str(params.get("modio_id") or "")
                ),
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover,
                is_batch_mode=batch or bool(params.get("is_batch_mode")),
            )
            return result

        if self.platform == PLATFORM_OTHER:
            result = OtherImporter(db=db).import_mod(
                source_folder=str(folder),
                title=folder_title,
                source_url=(
                    ""
                    if batch
                    else str(
                        params.get("source_url") or params.get("other_url") or ""
                    )
                ),
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover,
                external_id_suffix=folder.name if batch else "",
                is_batch_mode=batch or bool(params.get("is_batch_mode")),
            )
            return result

        # Nexus
        raw = str(params.get("nexus_url") or "")
        nexus_id = str(params.get("nexus_id") or parse_nexus_id(raw, ""))
        nexus_url = raw
        if batch:
            # Each subdirectory is its own Mod — identity follows folder name.
            nexus_id = folder.name
            nexus_url = ""
        result = NexusImporter(db=db).import_mod(
            source_folder=str(folder),
            title=folder_title,
            nexus_url=nexus_url,
            nexus_id=nexus_id,
            library_root=self.library_root,
            game_name=str(params.get("game_name") or ""),
            app_id=int(params.get("game_id") or params.get("app_id") or 0),
            context=context,
            cover_source=cover,
            offline_html_path=offline,
            is_batch_mode=batch or bool(params.get("is_batch_mode")),
        )
        return self._maybe_attach_nexus_offline(result, offline_html=offline)

    def _do_batch_folder_import(self, mod_dirs: list[Path]) -> ImportResult:
        successes: list[ImportResult] = []
        failures: list[str] = []
        user_cover = str(self.params.get("cover_source") or "")
        user_offline = self._offline_html_path()
        # Shared user picks apply only when importing a single folder.
        shared_cover = user_cover if len(mod_dirs) == 1 else ""
        shared_offline = user_offline if len(mod_dirs) == 1 else ""

        for index, folder in enumerate(mod_dirs, start=1):
            if self.isInterruptionRequested():
                break
            self._emit_progress(
                f"正在导入 ({index}/{len(mod_dirs)})：{folder.name}"
            )
            result = self._import_one_folder(
                folder,
                title=str(self.params.get("title") or "").strip() or folder.name,
                cover_source=shared_cover,
                offline_html=shared_offline,
                batch=True,
            )
            if result.success:
                successes.append(result)
            else:
                failures.append(f"{folder.name}: {result.error or '导入失败'}")

        if not successes:
            return ImportResult(
                success=False,
                error="; ".join(failures) if failures else "导入失败",
                platform=self.platform,
                skipped_count=len(failures),
            )

        last = successes[-1]
        last.imported_count = len(successes)
        last.skipped_count = len(failures)
        if failures:
            last.error = (
                f"成功 {len(successes)} 个，跳过 {len(failures)} 个"
                f"（{'; '.join(failures[:3])}）"
            )
        return last

    def _do_import(self) -> ImportResult:
        db = get_db()
        params = self.params
        source = str(params.get("source_path") or "").strip()
        title = str(params.get("title") or "").strip()
        use_archive = bool(params.get("use_archive")) or is_archive_path(source)
        context = self._context()
        cover_source = str(params.get("cover_source") or "")
        offline_html = self._offline_html_path()

        if use_archive:
            raw_paths = params.get("archive_paths")
            archive_paths: list[str] = []
            if isinstance(raw_paths, (list, tuple)):
                archive_paths = [str(p).strip() for p in raw_paths if str(p).strip()]
            if not archive_paths and source:
                # UI may join multi-select with "; "
                archive_paths = [
                    p.strip() for p in source.replace("\n", ";").split(";") if p.strip()
                ]
            if self.platform == PLATFORM_STEAM:
                self._emit_progress("正在解压...")
            else:
                self._emit_progress("正在准备压缩包...")
            result = ArchiveImporter(db=db).import_mod(
                archive_path=archive_paths[0] if archive_paths else source,
                archive_paths=archive_paths or None,
                platform=self.platform,
                library_root=self.library_root,
                title=title,
                workshop_id=str(params.get("workshop_id") or ""),
                nexus_url=str(params.get("nexus_url") or ""),
                nexus_id=str(params.get("nexus_id") or ""),
                github_url=str(params.get("github_url") or ""),
                modio_url=str(params.get("modio_url") or ""),
                modio_id=str(params.get("modio_id") or ""),
                source_url=str(params.get("source_url") or params.get("other_url") or ""),
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover_source,
                offline_html_path=offline_html,
                on_progress=self._emit_progress,
            )
            return self._maybe_attach_nexus_offline(result)

        folder_raw = str(params.get("folder") or source or "").strip()
        folder = Path(folder_raw).expanduser() if folder_raw else None
        if folder is not None and folder.is_dir():
            mod_dirs = discover_mod_directories(folder)
            if len(mod_dirs) > 1:
                self._emit_progress(f"检测到 {len(mod_dirs)} 个子目录，开始批量导入…")
                return self._do_batch_folder_import(mod_dirs)
            if mod_dirs:
                folder = mod_dirs[0]

        self._emit_progress("正在扫描Mod文件...")
        if folder is not None and folder.is_dir():
            return self._import_one_folder(
                folder,
                title=title,
                cover_source=cover_source,
                offline_html=offline_html,
                batch=False,
            )

        # Steam without a local folder (ID-only stub).
        if self.platform == PLATFORM_STEAM:
            self._emit_progress("正在导入...")
            return SteamImporter(db=db).import_mod(
                workshop_id=str(params.get("workshop_id") or source),
                title=title,
                source_folder="",
                library_root=self.library_root,
                game_name=str(params.get("game_name") or ""),
                app_id=int(params.get("game_id") or params.get("app_id") or 0),
                context=context,
                cover_source=cover_source,
            )

        return ImportResult(
            success=False,
            error="Mod目录不存在",
            platform=self.platform,
        )
