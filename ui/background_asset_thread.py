"""QThread wrapper for BackgroundAssetTask — keeps mass Asset IO off the UI."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from services.background_asset_task import (
    AssetTaskProgress,
    BackgroundAssetTask,
    CancelledAssetTask,
)


class BackgroundAssetWorker(QThread):
    """
    Run a callable with a shared :class:`BackgroundAssetTask` on a worker thread.

    Signals (UI thread):
      progress(dict)  — AssetTaskProgress.to_dict()
      finished_ok(object)
      failed(str)
      cancelled()
    """

    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        runner: Callable[[BackgroundAssetTask], Any],
        *,
        batch_size: int = 2000,
        batch_pause_ms: int = 40,
        checkpoint_every_files: int = 2000,
        task_name: str = "ui_asset_task",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self.task = BackgroundAssetTask(
            batch_size=batch_size,
            batch_pause_ms=batch_pause_ms,
            checkpoint_every_files=checkpoint_every_files,
            name=task_name,
        )

    def request_cancel(self) -> None:
        self.task.request_cancel()
        self.requestInterruption()

    def run(self) -> None:
        def _on_progress(prog: AssetTaskProgress) -> None:
            try:
                self.progress.emit(prog.to_dict())
            except Exception:  # noqa: BLE001
                pass

        # Expose progress hook via task attribute for runners that opt in.
        self.task._ui_progress = _on_progress  # type: ignore[attr-defined]
        try:
            if self.isInterruptionRequested():
                self.cancelled.emit()
                return
            result = self._runner(self.task)
            if self.task.cancelled() or self.isInterruptionRequested():
                self.cancelled.emit()
                return
            self.finished_ok.emit(result)
        except CancelledAssetTask:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc) or repr(exc))
