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
from services.importers.offline_html_batch import (
    make_empty_mod_stub,
    normalize_offline_html_paths,
)
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
            if result.success or result.is_duplicate:
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

    def _prepare_identity(
        self,
        *,
        offline_html: str = "",
        allow_local_fallback: bool = False,
        workshop_id: str = "",
        nexus_url: str = "",
        nexus_id: str = "",
        github_url: str = "",
        modio_url: str = "",
        modio_id: str = "",
        source_url: str = "",
    ):
        """Identity resolve → optional early duplicate (before materialize)."""
        from services.importers.duplicate_check import check_import_duplicate
        from services.importers.identity_resolve import (
            ImportIdentity,
            resolve_import_identity,
        )

        params = self.params
        resolved = resolve_import_identity(
            self.platform,
            workshop_id=workshop_id
            or str(params.get("workshop_id") or "").strip(),
            nexus_url=nexus_url or str(params.get("nexus_url") or "").strip(),
            nexus_id=nexus_id or str(params.get("nexus_id") or "").strip(),
            github_url=github_url or str(params.get("github_url") or "").strip(),
            modio_url=modio_url or str(params.get("modio_url") or "").strip(),
            modio_id=modio_id or str(params.get("modio_id") or "").strip(),
            source_url=source_url
            or str(params.get("source_url") or params.get("other_url") or "").strip(),
            offline_html=offline_html or self._offline_html_path(),
            allow_local_fallback=allow_local_fallback,
        )
        if isinstance(resolved, ImportResult):
            return resolved
        assert isinstance(resolved, ImportIdentity)
        dup = check_import_duplicate(
            get_db(),
            platform=resolved.platform,
            external_id=resolved.external_id,
            source_url=resolved.source_url,
            workshop_id=resolved.workshop_id,
        )
        if dup is not None:
            return dup
        return resolved

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

        prepared = self._prepare_identity(
            offline_html=offline,
            # Official identity required when an offline page is the identity source.
            # Folder-only / batch / 其它 may use local placeholders.
            allow_local_fallback=(
                batch
                or self.platform == PLATFORM_OTHER
                or (
                    self.platform == PLATFORM_NEXUS
                    and not str(offline or "").strip()
                )
            ),
        )
        if isinstance(prepared, ImportResult):
            return prepared

        if self.platform == PLATFORM_STEAM:
            workshop_id = prepared.workshop_id or str(params.get("workshop_id") or "").strip()
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
            github_url = prepared.source_url or str(params.get("github_url") or "").strip()
            result = GithubImporter(db=db).import_mod(
                github_url=github_url,
                source_folder=str(folder),
                title=folder_title or prepared.title,
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
                title=folder_title or prepared.title,
                modio_url=(
                    ""
                    if batch
                    else (prepared.source_url or str(params.get("modio_url") or ""))
                ),
                modio_id=(
                    folder.name
                    if batch
                    else (prepared.external_id or str(params.get("modio_id") or ""))
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
                        prepared.source_url
                        or params.get("source_url")
                        or params.get("other_url")
                        or ""
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

        # Nexus — identity already resolved (including offline HTML).
        nexus_id = prepared.external_id if not batch else folder.name
        nexus_url = "" if batch else prepared.source_url
        result = NexusImporter(db=db).import_mod(
            source_folder=str(folder),
            title=folder_title or prepared.title,
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
        duplicates: list[ImportResult] = []
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
            elif result.is_duplicate:
                duplicates.append(result)
            else:
                failures.append(f"{folder.name}: {result.error or '导入失败'}")

        skipped = len(duplicates)
        if not successes and not duplicates:
            return ImportResult(
                success=False,
                error="; ".join(failures) if failures else "导入失败",
                platform=self.platform,
                skipped_count=skipped,
            )

        if successes:
            last = successes[-1]
        else:
            last = duplicates[-1]
            last.success = True
            last.status = "duplicate"
            last.error = ""
        last.imported_count = len(successes)
        last.skipped_count = skipped
        if failures:
            last.error = (
                f"成功 {len(successes)} 个，跳过 {skipped} 个"
                f"（{'; '.join(failures[:3])}）"
            )
        return last

    def _do_batch_offline_html_import(self, html_paths: list[Path]) -> ImportResult:
        """Each offline HTML/MHTML file is one Mod import task (identity from page)."""
        successes: list[ImportResult] = []
        duplicates: list[ImportResult] = []
        failures: list[str] = []
        total = len(html_paths)

        for index, html in enumerate(html_paths, start=1):
            if self.isInterruptionRequested():
                break
            self._emit_progress(
                f"正在导入离线页面 ({index}/{total})：{html.name}"
            )
            stub = make_empty_mod_stub(ident=html.stem)
            try:
                # batch=False: identity must come from the HTML (not folder name).
                result = self._import_one_folder(
                    stub,
                    title="",
                    cover_source="",
                    offline_html=str(html),
                    batch=False,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{html.name}: {exc}")
                continue
            if result.success:
                successes.append(result)
            elif result.is_duplicate:
                duplicates.append(result)
            else:
                failures.append(f"{html.name}: {result.error or '导入失败'}")

        skipped = len(duplicates)
        failed = len(failures)
        if not successes and not duplicates:
            return ImportResult(
                success=False,
                error="; ".join(failures) if failures else "导入失败",
                platform=self.platform,
                skipped_count=skipped,
                failed_count=failed,
            )

        if successes:
            last = successes[-1]
        else:
            last = duplicates[-1]
            last.success = True
            last.status = "duplicate"
            last.error = ""
        last.imported_count = len(successes)
        last.skipped_count = skipped
        last.failed_count = failed
        summary = (
            f"成功 {len(successes)}，跳过 {skipped}，失败 {failed}"
        )
        if failures:
            last.error = f"{summary}（{'; '.join(failures[:3])}）"
        else:
            last.error = summary
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

        raw_html_list = params.get("offline_html_paths")
        if isinstance(raw_html_list, (list, tuple)) and raw_html_list:
            html_paths = normalize_offline_html_paths(
                [str(p) for p in raw_html_list]
            )
            if not html_paths:
                return ImportResult(
                    success=False,
                    error="未选择有效的离线页面文件",
                    platform=self.platform,
                )
            self._emit_progress(f"检测到 {len(html_paths)} 个离线页面，开始批量导入…")
            return self._do_batch_offline_html_import(html_paths)

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
            prepared = self._prepare_identity(
                offline_html=offline_html,
                allow_local_fallback=(
                    self.platform == PLATFORM_OTHER
                    or (
                        self.platform == PLATFORM_NEXUS
                        and not str(offline_html or "").strip()
                    )
                ),
            )
            if isinstance(prepared, ImportResult):
                return prepared
            if self.platform == PLATFORM_STEAM:
                self._emit_progress("正在解压...")
            else:
                self._emit_progress("正在准备压缩包...")
            result = ArchiveImporter(db=db).import_mod(
                archive_path=archive_paths[0] if archive_paths else source,
                archive_paths=archive_paths or None,
                platform=self.platform,
                library_root=self.library_root,
                title=title or prepared.title,
                workshop_id=prepared.workshop_id
                or str(params.get("workshop_id") or ""),
                nexus_url=prepared.source_url
                if self.platform == PLATFORM_NEXUS
                else str(params.get("nexus_url") or ""),
                nexus_id=prepared.external_id
                if self.platform == PLATFORM_NEXUS
                else str(params.get("nexus_id") or ""),
                github_url=prepared.source_url
                if self.platform == PLATFORM_GITHUB
                else str(params.get("github_url") or ""),
                modio_url=prepared.source_url
                if self.platform == PLATFORM_MODIO
                else str(params.get("modio_url") or ""),
                modio_id=prepared.external_id
                if self.platform == PLATFORM_MODIO
                else str(params.get("modio_id") or ""),
                source_url=prepared.source_url
                if self.platform == PLATFORM_OTHER
                else str(params.get("source_url") or params.get("other_url") or ""),
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
        is_batch_mode = bool(params.get("is_batch_mode"))
        # Single Import: selected folder is the only Mod Root. Never discover children.
        if is_batch_mode and folder is not None and folder.is_dir():
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
