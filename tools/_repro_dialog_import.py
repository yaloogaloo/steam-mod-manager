"""Reproduce import via ModImportDialog.accept() then Library refresh."""

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
from ui.library_view import ModLibraryView
from ui.mod_import_dialog import ModImportDialog
from ui.window_lifecycle import exec_dialog

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


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


def main() -> int:
    print(f"CRASH_LOG={crash_log_path()}", flush=True)
    lib = default_mod_library()
    src = Path(tempfile.mkdtemp(prefix="smm_dialog_import_")) / "ZZZ Dialog Crash Probe"
    src.mkdir(parents=True)
    (src / "main.pak").write_bytes(b"dialog-probe")
    (src / "cover.png").write_bytes(TINY_PNG)

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.set_preferred_filter("巫师三")
    view.resize(1400, 900)
    view.show()
    app.processEvents()
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 0.5)

    imported_ok = {"v": False, "result": None}

    def _mark(result: object) -> None:
        imported_ok["v"] = True
        imported_ok["result"] = result
        print(
            f"imported signal mod_id={getattr(result, 'mod_id', None)!r} "
            f"success={getattr(result, 'success', None)}",
            flush=True,
        )

    dialog = ModImportDialog(
        lib,
        parent=view,
        game_context={"game_id": 292030, "game_name": "巫师三"},
    )
    dialog.imported.connect(_mark)
    if dialog.radio_other is not None:
        dialog.radio_other.setChecked(True)
    dialog.other_folder_edit.setText(str(src))
    dialog.other_title_edit.setText("ZZZ Dialog Crash Probe")
    QTimer.singleShot(200, dialog._on_import)

    print("exec_dialog start", flush=True)
    code = exec_dialog(dialog)
    print(f"exec_dialog done code={code} imported_ok={imported_ok['v']}", flush=True)

    if imported_ok["v"]:
        print("post_dialog refresh", flush=True)
        view.refresh(force=True, reconcile=False)
        _wait_library_idle(app, view)
        _pump(app, 2.0)

    print("dialog_repro_finished_ok", flush=True)
    QTimer.singleShot(200, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log_exception("tools._repro_dialog_import.main")
        raise
