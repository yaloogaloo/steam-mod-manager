"""Import one tiny Mod into the real library, then run the post-import refresh path."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.pop("SMM_LIBRARY_SYNC", None)
os.environ.pop("PYTEST_CURRENT_TEST", None)
os.environ.setdefault("PYTHONFAULTHANDLER", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.crash_trace import crash_log_path, install_crash_hooks, log_exception

install_crash_hooks()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.paths import default_mod_library
from services.importers.importer_base import ImportContext
from services.mod_projection_events import notify_mod_changed
from ui.import_thread import ImportWorker
from ui.library_view import ModLibraryView

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

CTX = ImportContext(game_id=292030, game_name="巫师三")


def _pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _wait_library_idle(app: QApplication, view: ModLibraryView, seconds: float = 90.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        worker = getattr(view, "_load_worker", None)
        pending = bool(getattr(view, "_library_load_pending", False))
        running = bool(worker is not None and worker.isRunning())
        if not pending and not running:
            return
        time.sleep(0.03)
    print("WARN: library load did not idle before timeout", flush=True)


def main() -> int:
    print(f"CRASH_LOG={crash_log_path()}", flush=True)
    lib = default_mod_library()
    print(f"lib={lib}", flush=True)

    src_root = Path(tempfile.mkdtemp(prefix="smm_crash_probe_src_"))
    folder = src_root / "ZZZ Crash Probe Temp"
    folder.mkdir()
    (folder / "main.pak").write_bytes(b"crash-probe")
    (folder / "cover.png").write_bytes(TINY_PNG)

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.set_preferred_filter("巫师三")
    view.resize(1400, 900)
    view.show()
    view.raise_()
    app.processEvents()

    print("pre_import_refresh start", flush=True)
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 1.0)
    print(
        f"pre_import cards={len(getattr(view, '_cards', []) or [])} "
        f"rows={len(getattr(view, '_filtered_row_entries', []) or [])}",
        flush=True,
    )

    imported = {"id": "", "ok": False}

    def _on_ok(result: object) -> None:
        imported["ok"] = True
        imported["id"] = str(getattr(result, "mod_id", "") or "")
        print(
            f"import_finished success={getattr(result, 'success', None)} "
            f"mod_id={imported['id']!r} err={getattr(result, 'error', None)!r}",
            flush=True,
        )
        view.refresh(force=True, reconcile=False)

    def _on_fail(message: str) -> None:
        print(f"import_failed {message!r}", flush=True)

    worker = ImportWorker(
        platform="other",
        library_root=lib,
        params={
            "folder": str(folder),
            "title": "ZZZ Crash Probe Temp",
            "game_id": 292030,
            "app_id": 292030,
            "game_name": "巫师三",
            "context": CTX,
        },
        parent=view,
    )
    worker.import_finished.connect(_on_ok)
    worker.import_failed.connect(_on_fail)
    print("import_worker start", flush=True)
    worker.start()

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        app.processEvents()
        if worker.isFinished() and not view._library_load_pending:
            w2 = view._load_worker
            if w2 is None or not w2.isRunning():
                break
        time.sleep(0.03)

    _wait_library_idle(app, view)
    _pump(app, 2.0)

    bar = view.scroll.verticalScrollBar()
    for value in (0, 300, 900, 0):
        bar.setValue(int(value))
        app.processEvents()
        time.sleep(0.08)

    if imported["id"]:
        notify_mod_changed(imported["id"])
        app.processEvents()

    _pump(app, 2.0)
    print(
        f"post_import cards={len(getattr(view, '_cards', []) or [])} "
        f"imported={imported}",
        flush=True,
    )
    QTimer.singleShot(300, app.quit)
    app.exec()
    print("real_import_finished_ok", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log_exception("tools._repro_real_import.main")
        raise
