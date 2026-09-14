"""Cooperative background runner for large Asset Store / cleanup IO.

Keeps long-running file work off the UI thread (when used with a QThread
wrapper) and avoids saturating Windows disk IO via batching + pauses.

Does not change Asset Store hash rules, Backup CAS protocol, or Identity.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_BATCH_SIZE = 2000
DEFAULT_BATCH_PAUSE_MS = 40
DEFAULT_CHECKPOINT_EVERY_FILES = 2000


@dataclass
class AssetTaskProgress:
    phase: str = ""
    processed: int = 0
    total: int = 0
    message: str = ""
    bytes_done: int = 0
    files_done: int = 0
    cancelled: bool = False

    @property
    def ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, float(self.processed) / float(self.total))

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "processed": self.processed,
            "total": self.total,
            "message": self.message,
            "bytes_done": self.bytes_done,
            "files_done": self.files_done,
            "cancelled": self.cancelled,
            "ratio": self.ratio,
        }


@dataclass
class BackgroundAssetTask:
    """
    Cooperative batch runner for mass file operations.

    * Non-blocking relative to a UI thread when invoked from a worker thread
    * ``request_cancel()`` is thread-safe
    * Optional progress + checkpoint callbacks
    * Batch size + pause keep Windows disk from being saturated
    """

    batch_size: int = DEFAULT_BATCH_SIZE
    batch_pause_ms: int = DEFAULT_BATCH_PAUSE_MS
    checkpoint_every_files: int = DEFAULT_CHECKPOINT_EVERY_FILES
    name: str = "asset_task"
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    files_since_checkpoint: int = 0
    total_files_processed: int = 0
    total_bytes_processed: int = 0

    def request_cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def sleep_batch_pause(self) -> None:
        ms = max(0, int(self.batch_pause_ms))
        if ms <= 0:
            return
        # Short slices so cancel is responsive.
        end = time.monotonic() + (ms / 1000.0)
        while time.monotonic() < end:
            if self.cancelled():
                return
            time.sleep(min(0.02, max(0.0, end - time.monotonic())))

    def emit_progress(
        self,
        on_progress: Callable[[AssetTaskProgress], None] | None,
        *,
        phase: str,
        processed: int,
        total: int,
        message: str = "",
        bytes_done: int = 0,
        files_done: int = 0,
    ) -> None:
        if on_progress is None:
            return
        prog = AssetTaskProgress(
            phase=phase,
            processed=processed,
            total=total,
            message=message,
            bytes_done=bytes_done,
            files_done=files_done,
            cancelled=self.cancelled(),
        )
        try:
            on_progress(prog)
        except Exception:  # noqa: BLE001
            logger.debug("progress callback failed", exc_info=True)

    def run_batches(
        self,
        items: Iterable[T],
        process_one: Callable[[T], int],
        *,
        total: int | None = None,
        phase: str = "process",
        on_progress: Callable[[AssetTaskProgress], None] | None = None,
        on_checkpoint: Callable[[AssetTaskProgress], None] | None = None,
    ) -> AssetTaskProgress:
        """
        Process *items* in batches.

        ``process_one`` returns bytes processed for the item (0 if N/A).
        Raises ``CancelledError`` subclass via return progress.cancelled=True.
        """
        seq = list(items) if total is None else items
        if total is None:
            total_n = len(seq)  # type: ignore[arg-type]
            iterator: Iterator[T] = iter(seq)  # type: ignore[arg-type]
        else:
            total_n = int(total)
            iterator = iter(items)

        processed = 0
        bytes_done = 0
        batch_count = 0
        self.files_since_checkpoint = 0

        self.emit_progress(
            on_progress,
            phase=phase,
            processed=0,
            total=total_n,
            message=f"{self.name}:{phase} start",
        )

        for item in iterator:
            if self.cancelled():
                prog = AssetTaskProgress(
                    phase=phase,
                    processed=processed,
                    total=total_n,
                    message="cancelled",
                    bytes_done=bytes_done,
                    files_done=processed,
                    cancelled=True,
                )
                self.emit_progress(
                    on_progress,
                    phase=phase,
                    processed=processed,
                    total=total_n,
                    message="cancelled",
                    bytes_done=bytes_done,
                    files_done=processed,
                )
                return prog

            try:
                nbytes = int(process_one(item) or 0)
            except Exception:
                raise

            processed += 1
            bytes_done += max(0, nbytes)
            batch_count += 1
            self.total_files_processed += 1
            self.total_bytes_processed += max(0, nbytes)
            self.files_since_checkpoint += 1

            if batch_count >= max(1, int(self.batch_size)):
                self.emit_progress(
                    on_progress,
                    phase=phase,
                    processed=processed,
                    total=total_n,
                    message=f"{self.name}:{phase} batch",
                    bytes_done=bytes_done,
                    files_done=processed,
                )
                if (
                    on_checkpoint is not None
                    and self.files_since_checkpoint
                    >= max(1, int(self.checkpoint_every_files))
                ):
                    prog = AssetTaskProgress(
                        phase=phase,
                        processed=processed,
                        total=total_n,
                        message="checkpoint",
                        bytes_done=bytes_done,
                        files_done=processed,
                    )
                    try:
                        on_checkpoint(prog)
                    except Exception:  # noqa: BLE001
                        logger.warning("checkpoint callback failed", exc_info=True)
                    self.files_since_checkpoint = 0
                batch_count = 0
                self.sleep_batch_pause()

        prog = AssetTaskProgress(
            phase=phase,
            processed=processed,
            total=total_n,
            message="done",
            bytes_done=bytes_done,
            files_done=processed,
            cancelled=False,
        )
        self.emit_progress(
            on_progress,
            phase=phase,
            processed=processed,
            total=total_n,
            message="done",
            bytes_done=bytes_done,
            files_done=processed,
        )
        if on_checkpoint is not None and self.files_since_checkpoint > 0:
            try:
                on_checkpoint(prog)
            except Exception:  # noqa: BLE001
                logger.warning("final checkpoint callback failed", exc_info=True)
            self.files_since_checkpoint = 0
        return prog


class CancelledAssetTask(Exception):
    """Raised when a background asset task is cancelled cooperatively."""


def run_in_thread(
    fn: Callable[[], Any],
    *,
    name: str = "asset-worker",
    daemon: bool = True,
) -> tuple[threading.Thread, list[Any], list[BaseException]]:
    """
    Run *fn* on a daemon thread. Returns (thread, result_box, error_box).

    Caller joins the thread. Used by CLI tools and tests (not Qt).
    """
    result: list[Any] = []
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_target, name=name, daemon=daemon)
    thread.start()
    return thread, result, errors
