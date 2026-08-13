"""Background QThread: Steam / Mod.io metadata refresh (single or batch)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from services.metadata_refresh import (
    MetadataRefreshResult,
    refresh_selected_mods_metadata,
    refresh_steam_mod_metadata,
)


class MetadataRefreshWorker(QThread):
    """Refresh Steam metadata for one managed Mod."""

    refresh_started = Signal()
    refresh_finished = Signal(object)  # MetadataRefreshResult
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
        self.managed_path = Path(managed_path)
        self.mod_id = str(mod_id or "").strip() or self.managed_path.name
        self.library_root = (
            Path(library_root) if library_root else self.managed_path.parents[1]
        )
        self.force = bool(force)

    def run(self) -> None:
        import logging

        self.refresh_started.emit()
        try:
            result = refresh_steam_mod_metadata(
                self.mod_id,
                self.managed_path,
                library_root=self.library_root,
                force=self.force,
            )
            if self.isInterruptionRequested():
                return
            if not result.success and not result.skipped:
                logging.getLogger(__name__).error(
                    "Steam metadata refresh failed: %s",
                    result.error or "元数据刷新失败",
                )
                self.refresh_failed.emit(result.error or "元数据刷新失败")
                return
            self.refresh_finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "MetadataRefreshWorker crashed: %s", exc
            )
            self.refresh_failed.emit(str(exc))


class ModioMetadataRefreshWorker(QThread):
    """Refresh Mod.io metadata for one managed Mod via the official REST API."""

    refresh_started = Signal()
    refresh_finished = Signal(object)  # MetadataRefreshResult
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
        self.managed_path = Path(managed_path)
        self.mod_id = str(mod_id or "").strip() or self.managed_path.name
        self.library_root = (
            Path(library_root) if library_root else self.managed_path.parents[1]
        )
        self.source_url = str(source_url or "").strip()

    def run(self) -> None:
        import logging

        _log = logging.getLogger(__name__)
        _log.info("Using Mod.io metadata provider")
        self.refresh_started.emit()
        try:
            from services.modio_metadata_refresh import refresh_modio_mod_metadata

            result = refresh_modio_mod_metadata(
                self.mod_id,
                self.managed_path,
                library_root=self.library_root,
                source_url=self.source_url,
            )
            if self.isInterruptionRequested():
                return
            if not result.success and not result.skipped:
                _log.error(
                    "Mod.io metadata refresh failed: %s",
                    result.error or "Mod.io 元数据刷新失败",
                )
                self.refresh_failed.emit(result.error or "Mod.io 元数据刷新失败")
                return
            self.refresh_finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            _log.exception("ModioMetadataRefreshWorker crashed: %s", exc)
            self.refresh_failed.emit(str(exc))


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
        import logging

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
            logging.getLogger(__name__).exception(
                "MetadataBatchRefreshWorker crashed: %s", exc
            )
            self.refresh_failed.emit(str(exc))
