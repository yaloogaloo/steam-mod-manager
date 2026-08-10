"""Background QThread: refresh offline page for one Mod via OfflineManager."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.mod_platform import PLATFORM_STEAM, normalize_platform
from core.models import ModMetadata
from services.offline.manager import OfflineManager, attach_nexus_offline_page


class OfflineArchiveWorker(QThread):
    """
    Routes through ``OfflineManager`` (Steam / GitHub / mod.io archives).

    UI must not call platform providers directly.
    Nexus uses :class:`OfflineHtmlImportWorker` instead (manual HTML import).
    """

    archive_started = Signal()
    archive_finished = Signal(str)  # index.html path
    archive_failed = Signal(str)

    def __init__(
        self,
        managed_path: str | Path,
        *,
        platform: str = PLATFORM_STEAM,
        published_file_id: str | int = "",
        metadata: ModMetadata | None = None,
        library_root: str | Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.managed_path = Path(managed_path)
        self.platform = normalize_platform(platform)
        self.published_file_id = str(published_file_id or "").strip()
        self.metadata = metadata
        self.library_root = Path(library_root) if library_root else self.managed_path.parents[1]

    def run(self) -> None:
        self.archive_started.emit()
        try:
            mid = self.published_file_id or (
                self.metadata.published_file_id if self.metadata else ""
            )
            if not mid:
                mid = self.managed_path.name
            manager = OfflineManager(library_root=self.library_root)
            result = manager.update_mod_offline(
                mid,
                managed_path=self.managed_path,
                metadata=self.metadata,
                platform=self.platform,
            )
            if self.isInterruptionRequested():
                return
            if result.status == "failed" and result.error:
                if not result.index_path.is_file() or result.index_path.stat().st_size <= 0:
                    self.archive_failed.emit(result.error)
                    return
            self.archive_finished.emit(str(result.index_path))
        except Exception as exc:  # noqa: BLE001
            self.archive_failed.emit(str(exc))


class OfflineHtmlImportWorker(QThread):
    """Import a user-saved HTML file as the Nexus offline page."""

    archive_started = Signal()
    archive_finished = Signal(str)
    archive_failed = Signal(str)

    def __init__(
        self,
        managed_path: str | Path,
        html_path: str | Path,
        *,
        platform: str,
        published_file_id: str | int = "",
        library_root: str | Path | None = None,
        clean: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.managed_path = Path(managed_path)
        self.html_path = Path(html_path)
        self.platform = normalize_platform(platform)
        self.published_file_id = str(published_file_id or "").strip()
        self.library_root = Path(library_root) if library_root else self.managed_path.parents[1]
        self.clean = bool(clean)

    def run(self) -> None:
        self.archive_started.emit()
        try:
            mid = self.published_file_id or self.managed_path.name
            result = attach_nexus_offline_page(
                mid,
                self.html_path,
                managed_path=self.managed_path,
                library_root=self.library_root,
                clean=self.clean,
            )
            if self.isInterruptionRequested():
                return
            self.archive_finished.emit(str(result.index_path))
        except Exception as exc:  # noqa: BLE001
            self.archive_failed.emit(str(exc))
