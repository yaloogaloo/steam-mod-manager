"""Temporary: exercise import → library refresh → viewport rebind with crash hooks."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Must be set before QApplication / library refresh.
os.environ.pop("SMM_LIBRARY_SYNC", None)
os.environ.pop("PYTEST_CURRENT_TEST", None)
os.environ.setdefault("PYTHONFAULTHANDLER", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.crash_trace import crash_log_path, install_crash_hooks

install_crash_hooks()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from services.importers.importer_base import ImportContext
from services.importers.other import OtherImporter
from services.mod_projection_events import notify_mod_changed
from ui.import_thread import ImportWorker
from ui.library_view import ModLibraryView

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

CTX = ImportContext(game_id=1623730, game_name="Palworld")


def _src_mod(root: Path, name: str, *, with_cover: bool = False) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.pak").write_bytes(b"pak-payload")
    if with_cover:
        (folder / "cover.png").write_bytes(TINY_PNG)
    return folder


def _pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _wait_library_idle(app: QApplication, view: ModLibraryView, seconds: float = 20.0) -> None:
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
    log_path = crash_log_path()
    print(f"CRASH_LOG={log_path}", flush=True)

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="smm_import_crash_"))
    db_path = tmp / "repro.db"
    lib = tmp / "lib"
    src = tmp / "src"
    lib.mkdir()
    src.mkdir()

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    db.update_game_deploy_config(1623730, name="Palworld")

    importer = OtherImporter(db=db)
    seed_ids: list[str] = []
    for i in range(24):
        folder = _src_mod(src, f"SeedMod{i:02d}", with_cover=(i % 3 == 0))
        result = importer.import_mod(
            source_folder=folder,
            title=f"Seed Mod {i:02d}",
            library_root=lib,
            context=CTX,
            external_id_suffix=f"repro-{i:02d}",
        )
        if not result.success:
            print(f"SEED_FAIL i={i} err={result.error!r}", flush=True)
            return 2
        seed_ids.append(str(result.mod_id))

    print(f"seeded={len(seed_ids)} lib={lib}", flush=True)

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.resize(1280, 800)
    view.show()
    view.raise_()
    app.processEvents()

    print("refresh_1 start", flush=True)
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 0.8)
    print(
        f"refresh_1 done cards={len(getattr(view, '_cards', []) or [])} "
        f"rows={len(getattr(view, '_filtered_row_entries', []) or [])}",
        flush=True,
    )

    incoming = _src_mod(src, "ImportedCrashProbe", with_cover=True)
    imported_mid = {"id": ""}

    def _on_ok(result: object) -> None:
        mid = str(getattr(result, "mod_id", "") or "")
        imported_mid["id"] = mid
        print(f"import_finished mod_id={mid!r} success={getattr(result, 'success', None)}", flush=True)
        view.refresh(force=True, reconcile=False)

    def _on_fail(message: str) -> None:
        print(f"import_failed {message!r}", flush=True)

    worker = ImportWorker(
        platform="other",
        library_root=lib,
        params={
            "folder": str(incoming),
            "title": "Imported Crash Probe",
            "game_id": 1623730,
            "app_id": 1623730,
            "game_name": "Palworld",
            "context": CTX,
            "external_id_suffix": "repro-crash-probe",
        },
        parent=view,
    )
    worker.import_finished.connect(_on_ok)
    worker.import_failed.connect(_on_fail)
    print("import_worker start", flush=True)
    worker.start()

    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        app.processEvents()
        if worker.isFinished() and not view._library_load_pending:
            w2 = view._load_worker
            if w2 is None or w2.isFinished():
                break
        time.sleep(0.03)

    _wait_library_idle(app, view)
    _pump(app, 1.2)

    bar = view.scroll.verticalScrollBar()
    for value in (0, 400, 800, 0, 200):
        bar.setValue(value)
        app.processEvents()
        time.sleep(0.05)

    for mid in seed_ids[:8] + [imported_mid["id"]]:
        if mid:
            notify_mod_changed(mid)
            app.processEvents()

    _pump(app, 1.5)
    print(
        f"post_import cards={len(getattr(view, '_cards', []) or [])} "
        f"imported={imported_mid['id']!r}",
        flush=True,
    )

    QTimer.singleShot(200, app.quit)
    app.exec()
    DatabaseManager.reset_instance()
    print("repro_finished_ok", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        from services.crash_trace import log_exception

        log_exception("tools._repro_import_crash.main")
        raise
