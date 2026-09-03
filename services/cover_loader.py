"""Async Mod cover loading via QThreadPool (GUI-safe QImage → QPixmap)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

MAX_COVER_WORKERS = 4
# How long rename prep waits for in-flight QImage readers to release a folder.
CANCEL_WAIT_MS_DEFAULT = 2500

# Profiling counters (tests / diagnostics).
COVER_LOAD_REQUESTS = 0
COVER_LOAD_COMPLETED = 0
COVER_LOAD_CANCELLED = 0
COVER_LOAD_CACHE_HITS = 0
COVER_LOAD_MS_TOTAL = 0.0


def reset_cover_loader_stats() -> None:
    global COVER_LOAD_REQUESTS, COVER_LOAD_COMPLETED, COVER_LOAD_CANCELLED
    global COVER_LOAD_CACHE_HITS, COVER_LOAD_MS_TOTAL
    COVER_LOAD_REQUESTS = 0
    COVER_LOAD_COMPLETED = 0
    COVER_LOAD_CANCELLED = 0
    COVER_LOAD_CACHE_HITS = 0
    COVER_LOAD_MS_TOTAL = 0.0
    try:
        from services.startup_io_trace import reset_cover_wave

        reset_cover_wave()
    except Exception:  # noqa: BLE001
        pass


def note_cover_cache_hit() -> None:
    global COVER_LOAD_CACHE_HITS
    COVER_LOAD_CACHE_HITS += 1


def note_cover_cancelled() -> None:
    global COVER_LOAD_CANCELLED
    COVER_LOAD_CANCELLED += 1


def resolve_cover_path(
    managed_path: str | Path,
    cover_ref: str = "",
) -> Path | None:
    """Resolve a cover file path via Metadata Resolver (safe off the GUI thread)."""
    ref = str(cover_ref or "").strip()
    if ref:
        direct = Path(ref)
        try:
            if direct.is_file():
                return direct.resolve()
        except OSError:
            pass
    try:
        from services.mod_metadata_resolver import resolve_cover_path as resolve_meta

        found = resolve_meta(None, managed_path)
        if found is not None:
            return found
    except Exception:  # noqa: BLE001
        logger.debug("metadata resolver cover failed for %s", managed_path, exc_info=True)
    return None


def _path_key(managed_path: str | Path) -> str:
    try:
        return str(Path(managed_path).expanduser().resolve())
    except OSError:
        return str(Path(managed_path))


class _CoverTaskSignals(QObject):
    """Signals for one cover load task (must live until emission is delivered)."""

    finished = Signal(str, object)  # token, QImage | None


class CoverLoadTask(QRunnable):
    """Background: resolve path + load/scale QImage. Never touches QPixmap / widgets."""

    def __init__(
        self,
        token: str,
        managed_path: str | Path,
        cover_ref: str,
        width: int,
        height: int,
        *,
        cancel_check: Callable[[str | Path], bool] | None = None,
        on_finished_path: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.token = str(token)
        self.managed_path = Path(managed_path)
        self.path_key = _path_key(managed_path)
        self.cover_ref = str(cover_ref or "")
        self.width = int(width)
        self.height = int(height)
        self._cancel_check = cancel_check
        self._on_finished_path = on_finished_path
        self.signals = _CoverTaskSignals()
        self.setAutoDelete(True)

    def _cancelled(self) -> bool:
        check = self._cancel_check
        if check is None:
            return False
        try:
            return bool(check(self.managed_path))
        except Exception:  # noqa: BLE001
            return False

    def run(self) -> None:  # noqa: D401
        global COVER_LOAD_COMPLETED, COVER_LOAD_MS_TOTAL
        t0 = time.perf_counter()
        image: QImage | None = None
        try:
            if self._cancelled():
                image = None
            else:
                path = resolve_cover_path(self.managed_path, self.cover_ref)
                if self._cancelled():
                    image = None
                elif path is not None and path.is_file():
                    from services.cover_cache import get_cover_image, put_cover_image

                    cached = get_cover_image(path, self.width, self.height)
                    if cached is not None:
                        image = cached
                    else:
                        # QImage reads the file; drop reference ASAP after scale.
                        img = QImage(str(path))
                        if self._cancelled():
                            image = None
                        elif not img.isNull():
                            image = img.scaled(
                                self.width,
                                self.height,
                                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                            put_cover_image(path, self.width, self.height, image)
                        del img
        except Exception:  # noqa: BLE001
            logger.debug(
                "cover load failed token=%s path=%s",
                self.token,
                self.managed_path,
                exc_info=True,
            )
            image = None
        finally:
            COVER_LOAD_COMPLETED += 1
            COVER_LOAD_MS_TOTAL += (time.perf_counter() - t0) * 1000.0
            try:
                from services.startup_io_trace import cover_wave_on_task_done

                cover_wave_on_task_done(failed=image is None)
            except Exception:  # noqa: BLE001
                pass
            # Release path lock tracking on the pool thread (before GUI emit).
            cb = self._on_finished_path
            if cb is not None:
                try:
                    cb(self.path_key)
                except Exception:  # noqa: BLE001
                    logger.debug("cover inflight decrement failed", exc_info=True)
        self.signals.finished.emit(self.token, image)


class CoverLoaderManager(QObject):
    """
    Singleton pool for Mod card covers.

    Flow: ``request()`` → ``CoverLoadTask`` → ``image_ready(token, QImage)``
    on the GUI thread (QueuedConnection). Callers convert to QPixmap there.
    """

    image_ready = Signal(str, object)  # token, QImage | None
    # Emitted (queued to GUI) so cards/detail can drop QPixmap / cancel work
    # before a folder rename. Payload is the resolved path key.
    path_release_requested = Signal(str)

    _instance: CoverLoaderManager | None = None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_COVER_WORKERS)
        self._active_tokens: set[str] = set()
        # Paths whose in-flight loads must stop reading (folder about to rename).
        self._cancelled_paths: set[str] = set()
        self._token_paths: dict[str, str] = {}
        self._lock = threading.Lock()
        # In-flight QImage readers per managed path (pool-thread updated).
        self._inflight_by_path: dict[str, int] = {}

    @classmethod
    def instance(cls) -> CoverLoaderManager:
        if cls._instance is None:
            mgr = CoverLoaderManager()
            # Prefer main/GUI thread affinity so release signals queue correctly.
            try:
                from PySide6.QtWidgets import QApplication

                app = QApplication.instance()
                if app is not None:
                    mgr.moveToThread(app.thread())
            except Exception:  # noqa: BLE001
                pass
            cls._instance = mgr
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """
        Test helper — clear singleton after draining the cover thread pool.

        Must wait for in-flight tasks before dropping the QObject; otherwise
        queued ``image_ready`` / pool teardown can corrupt the Qt heap
        (Windows ``0xc0000374``) during pytest fixture teardown.
        """
        mgr = cls._instance
        cls._instance = None
        if mgr is None:
            return
        try:
            mgr._active_tokens.clear()
            mgr._token_paths.clear()
            mgr._cancelled_paths.clear()
            with mgr._lock:
                mgr._inflight_by_path.clear()
            pool = getattr(mgr, "_pool", None)
            if pool is not None:
                pool.clear()
                pool.waitForDone(2000)
        except Exception:  # noqa: BLE001
            logger.exception("CoverLoaderManager.reset_instance cleanup failed")
        try:
            mgr.deleteLater()
        except Exception:  # noqa: BLE001
            pass

    def is_path_cancelled(self, managed_path: str | Path) -> bool:
        return _path_key(managed_path) in self._cancelled_paths

    def inflight_count(self, managed_path: str | Path) -> int:
        key = _path_key(managed_path)
        with self._lock:
            return int(self._inflight_by_path.get(key, 0))

    def active_token_count_for_path(self, managed_path: str | Path) -> int:
        """How many CoverLoader tokens still map to *managed_path*."""
        key = _path_key(managed_path)
        return sum(1 for path in self._token_paths.values() if path == key)

    def bound_card_token_count(self, managed_path: str | Path) -> int:
        """Alias for diagnostics: active tokens still bound to this path."""
        return self.active_token_count_for_path(managed_path)

    def _inc_inflight(self, path_key: str) -> None:
        with self._lock:
            self._inflight_by_path[path_key] = (
                int(self._inflight_by_path.get(path_key, 0)) + 1
            )

    def _dec_inflight(self, path_key: str) -> None:
        with self._lock:
            n = int(self._inflight_by_path.get(path_key, 0)) - 1
            if n <= 0:
                self._inflight_by_path.pop(path_key, None)
            else:
                self._inflight_by_path[path_key] = n

    def request_ui_release(
        self,
        managed_path: str | Path,
        *,
        wait_ms: int = 400,
    ) -> None:
        """
        Cancel cover loads and ask GUI widgets to drop pixmaps for *path*.

        Safe to call from a worker thread: the signal is queued to the GUI
        thread; this method waits briefly so handlers can run.
        """
        key = _path_key(managed_path)
        self.cancel_for_managed_path(managed_path, wait_ms=min(wait_ms, 2000))
        done = threading.Event()

        def _emit() -> None:
            try:
                self.path_release_requested.emit(key)
            finally:
                done.set()

        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                self.path_release_requested.emit(key)
                done.set()
            else:
                QTimer.singleShot(0, _emit)
                done.wait(timeout=max(0.05, wait_ms / 1000.0))
        except Exception:  # noqa: BLE001
            try:
                self.path_release_requested.emit(key)
            except Exception:  # noqa: BLE001
                pass
            done.set()

    def request(
        self,
        token: str,
        managed_path: str | Path,
        *,
        cover_ref: str = "",
        width: int,
        height: int,
    ) -> None:
        global COVER_LOAD_REQUESTS
        tok = str(token)
        if not tok:
            return
        path_key = _path_key(managed_path)
        # New request for this path clears a prior rename-cancel mark.
        self._cancelled_paths.discard(path_key)
        COVER_LOAD_REQUESTS += 1
        try:
            from services.startup_io_trace import cover_wave_on_request

            cover_wave_on_request()
        except Exception:  # noqa: BLE001
            pass
        self._active_tokens.add(tok)
        self._token_paths[tok] = path_key
        self._inc_inflight(path_key)
        task = CoverLoadTask(
            tok,
            managed_path,
            cover_ref,
            width,
            height,
            cancel_check=self.is_path_cancelled,
            on_finished_path=self._dec_inflight,
        )
        task.signals.finished.connect(
            self._on_task_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._pool.start(task)

    def cancel(self, token: str) -> None:
        """Mark token inactive so late results are ignored by cards."""
        tok = str(token)
        if tok in self._active_tokens or tok in self._token_paths:
            note_cover_cancelled()
        self._active_tokens.discard(tok)
        self._token_paths.pop(tok, None)

    def cancel_for_managed_path(
        self,
        managed_path: str | Path,
        *,
        wait_ms: int = CANCEL_WAIT_MS_DEFAULT,
    ) -> None:
        """
        Stop treating in-flight cover loads for *managed_path* as active.

        Marks the path cancelled so workers stop reading ASAP, then waits until
        in-flight ``QImage`` readers for that path finish (Windows rename lock).
        Does not forcibly kill pool threads (Qt limitation).
        """
        key = _path_key(managed_path)
        self._cancelled_paths.add(key)
        stale = [tok for tok, path in self._token_paths.items() if path == key]
        for tok in stale:
            self._active_tokens.discard(tok)
            self._token_paths.pop(tok, None)

        timeout = max(0, int(wait_ms))
        if timeout <= 0:
            return
        deadline = time.monotonic() + (timeout / 1000.0)
        while self.inflight_count(managed_path) > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        remaining = self.inflight_count(managed_path)
        if remaining > 0:
            logger.warning(
                "CoverLoader still inflight=%s after cancel wait for %s",
                remaining,
                key,
            )

    def _on_task_finished(self, token: str, image: object) -> None:
        tok = str(token)
        path_key = self._token_paths.pop(tok, "")
        if tok not in self._active_tokens:
            return
        if path_key and path_key in self._cancelled_paths:
            self._active_tokens.discard(tok)
            return
        self._active_tokens.discard(tok)
        self.image_ready.emit(tok, image)
