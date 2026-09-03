"""Background library snapshot load — no QWidget on the worker thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from services.mod_library_cache import LibrarySnapshot, get_library_cache


class LibraryLoadWorker(QThread):
    """Filesystem scan + metadata + batch DB → ``LibrarySnapshot``."""

    loaded = Signal(object)  # LibrarySnapshot
    failed = Signal(str)

    def __init__(
        self,
        library_root: str | Path,
        *,
        generation: int = 0,
        force: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.library_root = Path(library_root)
        self.generation = int(generation)
        self.force = bool(force)

    def run(self) -> None:
        try:
            from services.startup_io_trace import begin as _io_begin

            _io_begin("library_load", force=int(self.force))
        except Exception:  # noqa: BLE001
            pass
        try:
            snapshot = get_library_cache().load_snapshot(
                self.library_root, force=self.force
            )
            if self.isInterruptionRequested():
                return
            self.loaded.emit(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            try:
                from services.startup_io_trace import end as _io_end

                _io_end("library_load")
            except Exception:  # noqa: BLE001
                pass
