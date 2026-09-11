"""Temporary: post-import UI path against the real library (refresh + rebind)."""

from __future__ import annotations

import os
import sys
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

from core.db_manager import get_db
from core.paths import default_mod_library
from services.mod_projection_events import notify_mod_changed
from ui.library_view import ModLibraryView


def _pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _wait_library_idle(app: QApplication, view: ModLibraryView, seconds: float = 60.0) -> None:
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
    db = get_db()
    print(f"db={db.db_path} lib={lib}", flush=True)

    rows = []
    try:
        rows = list(db.list_mod_list_items() or [])
    except Exception:
        log_exception("real_lib.list_mod_list_items")
        raise
    print(f"db_mods={len(rows)}", flush=True)

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.resize(1400, 900)
    view.show()
    view.raise_()
    app.processEvents()

    print("refresh_real start", flush=True)
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 1.5)
    print(
        f"refresh_real done cards={len(getattr(view, '_cards', []) or [])} "
        f"rows={len(getattr(view, '_filtered_row_entries', []) or [])}",
        flush=True,
    )

    bar = view.scroll.verticalScrollBar()
    for value in (0, 200, 600, 1200, 0):
        bar.setValue(int(value))
        app.processEvents()
        time.sleep(0.08)

    ids = []
    for item in rows[:20]:
        if isinstance(item, dict):
            mid = str(item.get("internal_id") or item.get("mod_id") or "")
        else:
            mid = str(getattr(item, "internal_id", "") or getattr(item, "mod_id", "") or "")
        if mid:
            ids.append(mid)
    for mid in ids:
        notify_mod_changed(mid)
        app.processEvents()

    cards = list(getattr(view, "_cards", []) or [])
    if cards:
        try:
            first = cards[0]
            first.rebind(first.managed_path, first.metadata, card_data=getattr(first, "_card_data", None))
            if hasattr(first, "ensure_cover"):
                first.ensure_cover()
        except Exception:
            log_exception("real_lib.manual_rebind")
            raise

    _pump(app, 2.0)
    print("real_lib_finished_ok", flush=True)
    QTimer.singleShot(200, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log_exception("tools._repro_real_library_refresh.main")
        raise
