"""Select the recently imported Nexus mod, then post-import refresh + detail rebind."""

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
    print("WARN: library load did not idle", flush=True)


def main() -> int:
    print(f"CRASH_LOG={crash_log_path()}", flush=True)
    db = get_db()
    rows = db._conn.execute(
        "select mod_id, title, last_known_path, platform, app_id "
        "from mods where title like '%E3 Combat%' or last_known_path like '%E3 Combat%' "
        "or title like '%Crash Probe%' or last_known_path like '%Crash Probe%'"
    ).fetchall()
    print("targets", [tuple(r) for r in rows], flush=True)

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    view.set_target_root(str(default_mod_library()))
    view.set_preferred_filter("巫师三")
    view.resize(1400, 900)
    view.show()
    app.processEvents()
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 1.0)

    for row in rows:
        mid = str(row[0])
        path = row[2]
        print(f"show_mod mid={mid} path={path}", flush=True)
        view.detail_panel.show_mod(path, mod_id=mid, game_id=292030, game_name="巫师三")
        app.processEvents()
        notify_mod_changed(mid)
        app.processEvents()

    print("post_select refresh", flush=True)
    view.refresh(force=True, reconcile=False)
    _wait_library_idle(app, view)
    _pump(app, 2.5)

    cards = list(getattr(view, "_cards", []) or [])
    print(f"cards={len(cards)}", flush=True)
    for card in cards:
        try:
            card.rebind(
                card.managed_path,
                card.metadata,
                card_data=getattr(card, "_card_data", None),
            )
            if hasattr(card, "ensure_cover"):
                card.ensure_cover()
        except Exception:
            log_exception("e3_repro.rebind", path=str(card.managed_path))
            raise
        app.processEvents()

    _pump(app, 2.0)
    print("e3_repro_finished_ok", flush=True)
    QTimer.singleShot(200, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log_exception("tools._repro_e3_detail.main")
        raise
