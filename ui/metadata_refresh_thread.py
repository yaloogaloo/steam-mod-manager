"""Background QThread: unified mod refresh (local reconcile + optional official sync)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from services.crash_trace import log_exception, traced
from services.metadata_refresh import refresh_selected_mods_metadata

_log = logging.getLogger(__name__)


class ModRefreshWorker(QThread):
    """Local reconcile first; official provider only when never synced."""

    refresh_started = Signal()
    refresh_finished = Signal(object)  # MetadataRefreshResult
    refresh_failed = Signal(str)

    def __init__(
        self,
        managed_path: str | Path,
        *,
        mod_id: str | int = "",
        library_root: str | Path | None = None,
        platform: str = "",
        source_url: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.managed_path = Path(managed_path)
        # Identity: mods.mod_id only — never folder.name / workspace_id fallback.
        self.mod_id = str(mod_id or "").strip()
        self.library_root = (
            Path(library_root) if library_root else self.managed_path.parents[1]
        )
        self.platform = str(platform or "").strip()
        self.source_url = str(source_url or "").strip()

    @traced("ModRefreshWorker.run")
    def run(self) -> None:
        from services.mod_refresh import refresh_mod
        from services.path_lifecycle import resolve_refresh_folder

        self.refresh_started.emit()
        mid = self.mod_id
        try:
            if not mid.isdigit():
                msg = (
                    "Steam/Mod refresh requires mods.mod_id (SQLite PK); "
                    "folder.name and workspace_id are not valid identity"
                )
                _log.error(
                    "[MOD_REFRESH_FAILED] mod_id=%s workspace_id=— app_id=0 "
                    "title=%r stage=worker_identity reason=%s exception=—",
                    mid or "—",
                    self.managed_path.name,
                    msg,
                )
                self.refresh_failed.emit(msg)
                return
            folder = resolve_refresh_folder(
                mid,
                self.managed_path,
            )
            if not folder.is_dir():
                msg = f"[PATH_INVALID] Mod 目录不存在: {self.managed_path}"
                _log.error(
                    "[MOD_REFRESH_FAILED] mod_id=%s workspace_id=— app_id=0 "
                    "title=%r stage=path_validate reason=%s exception=—",
                    mid,
                    self.managed_path.name,
                    msg,
                )
                self.refresh_failed.emit(msg)
                return
            result = refresh_mod(
                mid,
                folder,
                platform=self.platform,
                library_root=self.library_root,
                source_url=self.source_url,
            )
            if self.isInterruptionRequested():
                return
            compat = result.to_metadata_refresh_result()
            from services.path_lifecycle import resolve_managed_folder

            final = resolve_managed_folder(mid)
            if final.path is not None:
                compat.managed_path = final.path
                if compat.old_path is None:
                    compat.old_path = self.managed_path
            if not compat.success and not compat.skipped:
                _log.error(
                    "[MOD_REFRESH_FAILED] mod_id=%s workspace_id=— app_id=0 "
                    "title=%r stage=provider_result reason=%s exception=—",
                    mid,
                    compat.title or self.managed_path.name,
                    compat.error or result.message or "元数据刷新失败",
                )
                self.refresh_failed.emit(
                    compat.error or result.message or "元数据刷新失败"
                )
                return
            self.refresh_finished.emit(compat)
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "ModRefreshWorker.run",
                mod_id=mid,
                path=str(self.managed_path),
            )
            _log.exception(
                "[MOD_REFRESH_FAILED] mod_id=%s workspace_id=— app_id=0 "
                "title=%r stage=worker_crash reason=unhandled exception=%r",
                mid or "—",
                self.managed_path.name,
                exc,
            )
            self.refresh_failed.emit(str(exc))


class MetadataRefreshWorker(QThread):
    """Deprecated alias — delegates to :class:`ModRefreshWorker`."""

    refresh_started = Signal()
    refresh_finished = Signal(object)
    refresh_failed = Signal(str)

    def __init__(
        self,
        managed_path: str | Path,
        *,
        mod_id: str | int = "",
        library_root: str | Path | None = None,
        force: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._inner = ModRefreshWorker(
            managed_path,
            mod_id=mod_id,
            library_root=library_root,
            platform="steam",
            parent=parent,
        )
        self._inner.refresh_started.connect(self.refresh_started.emit)
        self._inner.refresh_finished.connect(self.refresh_finished.emit)
        self._inner.refresh_failed.connect(self.refresh_failed.emit)

    def run(self) -> None:
        self._inner.run()

    def requestInterruption(self) -> None:  # noqa: N802
        super().requestInterruption()
        self._inner.requestInterruption()


class ModioMetadataRefreshWorker(QThread):
    """Deprecated alias — delegates to :class:`ModRefreshWorker`."""

    refresh_started = Signal()
    refresh_finished = Signal(object)
    refresh_failed = Signal(str)

    def __init__(
        self,
        managed_path: str | Path,
        *,
        mod_id: str | int = "",
        library_root: str | Path | None = None,
        source_url: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._inner = ModRefreshWorker(
            managed_path,
            mod_id=mod_id,
            library_root=library_root,
            platform="modio",
            source_url=source_url,
            parent=parent,
        )
        self._inner.refresh_started.connect(self.refresh_started.emit)
        self._inner.refresh_finished.connect(self.refresh_finished.emit)
        self._inner.refresh_failed.connect(self.refresh_failed.emit)

    def run(self) -> None:
        self._inner.run()

    def requestInterruption(self) -> None:  # noqa: N802
        super().requestInterruption()
        self._inner.requestInterruption()


class MetadataBatchRefreshWorker(QThread):
    """Batch metadata refresh (all platforms) with progress ``current / total``."""

    refresh_started = Signal()
    progress = Signal(int, int, str)  # done, total, message
    refresh_finished = Signal(object)  # list[MetadataRefreshResult]
    refresh_failed = Signal(str)

    def __init__(
        self,
        entries: list[tuple[str, Path]] | list[tuple[str, Path, str]],
        *,
        library_root: str | Path | None = None,
        max_workers: int = 2,
        parent=None,
    ) -> None:
        super().__init__(parent)
        normalized: list[tuple[str, Path, str]] = []
        for item in entries:
            if len(item) >= 3:
                mid, path, plat = item[0], item[1], item[2]
            else:
                mid, path = item[0], item[1]
                plat = "steam"
            normalized.append((str(mid), Path(path), str(plat or "steam")))
        self.entries = normalized
        self.library_root = Path(library_root) if library_root else None
        self.max_workers = max(1, min(int(max_workers), 2))

    def run(self) -> None:
        self.refresh_started.emit()
        try:
            results = refresh_selected_mods_metadata(
                self.entries,
                library_root=self.library_root,
                max_workers=self.max_workers,
                on_progress=lambda d, t, msg: self.progress.emit(d, t, msg),
            )
            if self.isInterruptionRequested():
                return
            self.refresh_finished.emit(results)
        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "[MOD_REFRESH_FAILED] mod_id=— workspace_id=— app_id=0 "
                "title=— stage=batch_worker_crash reason=unhandled exception=%r",
                exc,
            )
            self.refresh_failed.emit(str(exc))
