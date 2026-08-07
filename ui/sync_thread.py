"""Background QThread that runs ModSyncService without blocking the UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from services.sync import ModSyncService, SyncOptions, SyncResult


class SyncWorker(QThread):
    """
    Run a full library sync on a worker thread.

    Signals
    -------
    progress_changed(percent, message)
        Overall progress 0–100 and a human-readable status line.
    sync_finished(result)
        Emitted with a :class:`SyncResult` on success.
    sync_failed(error_message)
        Emitted when the sync raises before producing a result.
    """

    progress_changed = Signal(int, str)
    sync_finished = Signal(object)
    sync_failed = Signal(str)

    def __init__(
        self,
        workshop_dir: str | Path,
        target_dir: str | Path,
        options: SyncOptions | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.workshop_dir = Path(workshop_dir)
        self.target_dir = Path(target_dir)
        self.options = options or SyncOptions()
        self._service: ModSyncService | None = None

    def run(self) -> None:
        try:
            self._service = ModSyncService(self.workshop_dir, self.target_dir)
            result = self._service.sync(
                self.options,
                on_progress=self._on_progress,
            )
            if self.isInterruptionRequested():
                return
            self.sync_finished.emit(result)
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.sync_failed.emit(str(exc))
        finally:
            if self._service is not None:
                self._service.close()
                self._service = None

    def _on_progress(
        self,
        phase: str,
        current: int,
        total: int,
        message: str,
        **_kwargs: object,
    ) -> None:
        if self.isInterruptionRequested():
            return
        percent = _phase_to_percent(phase, current, total)
        self.progress_changed.emit(percent, message)


class OfflinePagesSyncWorker(QThread):
    """Archive Steam offline pages for mods already in the library."""

    progress_changed = Signal(int, str)
    sync_finished = Signal(object)
    sync_failed = Signal(str)

    def __init__(
        self,
        target_dir: str | Path,
        *,
        proxy_url: str = "",
        steam_cookie: str = "",
        mod_ids: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.target_dir = Path(target_dir)
        self.proxy_url = proxy_url
        self.steam_cookie = steam_cookie
        self.mod_ids = mod_ids
        self._service: ModSyncService | None = None

    def run(self) -> None:
        try:
            # workshop_dir unused for offline-only; pass target as placeholder.
            self._service = ModSyncService(self.target_dir, self.target_dir)
            result = self._service.sync_offline_pages_only(
                mod_ids=self.mod_ids,
                options=SyncOptions(
                    archive_pages=True,
                    download_covers=False,
                    skip_existing=True,
                    overwrite_files=False,
                    proxy_url=self.proxy_url,
                    steam_cookie=self.steam_cookie,
                ),
                on_progress=self._on_progress,
            )
            if self.isInterruptionRequested():
                return
            self.sync_finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.sync_failed.emit(str(exc))
        finally:
            if self._service is not None:
                self._service.close()
                self._service = None

    def _on_progress(
        self,
        phase: str,
        current: int,
        total: int,
        message: str,
        *,
        current_mod_name: str = "",
        phase_detail: str = "",
        in_progress: bool = False,
        queued_count: int | None = None,
        running_count: int | None = None,
        completed_count: int | None = None,
        **_kwargs: object,
    ) -> None:
        if self.isInterruptionRequested():
            return
        completed = (
            completed_count if completed_count is not None else current
        )
        running = running_count if running_count is not None else (1 if in_progress else 0)
        queued = (
            queued_count
            if queued_count is not None
            else max(0, total - completed - running)
        )
        percent = _phase_to_percent(
            phase,
            completed,
            total,
            in_progress=in_progress or running > 0,
            running_count=running,
        )
        display = format_offline_progress_message(
            phase=phase,
            current=completed,
            total=total,
            message=message,
            current_mod_name=current_mod_name,
            phase_detail=phase_detail,
            queued_count=queued,
            running_count=running,
            completed_count=completed,
        )
        self.progress_changed.emit(percent, display)


def _phase_to_percent(
    phase: str,
    current: int,
    total: int,
    *,
    in_progress: bool = False,
    running_count: int = 0,
) -> int:
    """Map sync pipeline phases onto a single 0–100 progress scale."""
    total = max(total, 1)
    spans = {
        "scan": (0, 5),
        "metadata": (5, 25),
        "sync": (25, 95),
        "offline": (0, 95),
        "done": (100, 100),
    }
    start, end = spans.get(phase, (0, 100))
    if start == end:
        return start

    if phase == "offline" and (in_progress or running_count > 0):
        # Credit completed mods + partial credit for in-flight workers.
        active = max(running_count, 1 if in_progress else 0)
        ratio = max(0.0, min(1.0, (current + 0.5 * active) / total))
        percent = int(start + (end - start) * ratio)
        return max(1, min(99, percent))

    ratio = max(0.0, min(1.0, current / total))
    return int(start + (end - start) * ratio)


def format_offline_progress_message(
    *,
    phase: str,
    current: int,
    total: int,
    message: str,
    current_mod_name: str = "",
    phase_detail: str = "",
    queued_count: int | None = None,
    running_count: int | None = None,
    completed_count: int | None = None,
) -> str:
    """Build the status label text for offline archive progress."""
    if phase == "done":
        return message

    total = max(total, 1)
    completed = completed_count if completed_count is not None else current
    running = running_count if running_count is not None else 0
    queued = (
        queued_count
        if queued_count is not None
        else max(0, total - completed - running)
    )

    lines = [
        "Steam 离线网页同步",
        "正在处理:",
        f"{running}/{total}",
        "等待:",
        str(queued),
        "已完成:",
        f"{completed}/{total}",
    ]
    if current_mod_name:
        lines.append("当前Mod:")
        lines.append(current_mod_name)
    if phase_detail:
        lines.append("状态:")
        lines.append(phase_detail)
    elif message and message != "Steam 离线网页同步":
        lines.append(message)
    return "\n".join(lines)


def summarize_result(result: SyncResult) -> str:
    return (
        f"同步完成：成功 {len(result.success)}，"
        f"跳过 {len(result.skipped)}，"
        f"失败 {len(result.failed)}"
    )


def summarize_offline_result(result: SyncResult) -> str:
    return (
        f"离线网页同步完成：成功 {len(result.success)}，"
        f"失败 {len(result.failed)}，"
        f"429 {len(result.rate_limited)}，"
        f"跳过 {len(result.skipped)}"
    )
