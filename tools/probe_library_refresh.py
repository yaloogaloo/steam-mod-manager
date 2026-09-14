#!/usr/bin/env python3
"""Real Qt Library Refresh probe — visible window, production or isolated data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pump(app, seconds: float) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    deadline = time.monotonic() + max(0.05, float(seconds))
    while time.monotonic() < deadline:
        # Prefer QEventLoop.ProcessEventsFlag when available (PySide6 version skew).
        try:
            from PySide6.QtCore import QEventLoop as _EL

            app.processEvents(_EL.ProcessEventsFlag.AllEvents, 50)
        except Exception:  # noqa: BLE001
            app.processEvents(50)
        leftover = deadline - time.monotonic()
        if leftover <= 0:
            break
        loop = QEventLoop()
        QTimer.singleShot(min(50, int(leftover * 1000)), loop.quit)
        loop.exec()


def _wait_not_loading(view, app, timeout: float = 30.0) -> float:
    t0 = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(app, 0.05)
        if not bool(getattr(view, "_library_load_pending", False)):
            worker = getattr(view, "_load_worker", None)
            running = worker is not None and worker.isRunning()
            if not running:
                return (time.perf_counter() - t0) * 1000.0
    return (time.perf_counter() - t0) * 1000.0


def _seed_isolated(db, lib: Path, n: int, missing: int) -> None:
    from core.db_manager import _utc_now
    from core.game_info import GameInfo

    db.upsert_game(GameInfo(app_id=4242, name="ScaleGame", folder_name="ScaleGame"))
    now = _utc_now()
    keep = max(0, n - missing)
    rows = []
    game = lib / "ScaleGame"
    game.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        mid = 800000 + i
        folder = game / f"Mod{i:05d}"
        if i < keep:
            folder.mkdir(parents=True, exist_ok=True)
        path = str(folder)
        present = 1  # stale LIVE so Presence must flip missing folders to MISS
        rows.append(
            (
                mid, 4242, f"Scale {i}", "", "", f"Scale {i}", "", "", 0,
                "not_deployed", "steam", f"https://example.test/{mid}",
                str(mid), str(mid), "{}", 0, "none", 1, "none", "",
                now, path, present, "healthy", "steam", str(mid),
            )
        )
    with db._lock:
        db._conn.executemany(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                deploy_status, platform, source_url, external_id, workspace_id,
                mod_files, is_invalid, conflict_status, enabled, offline_status,
                cover_path, updated_at, last_known_path, folder_present,
                content_status, source_type, internal_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            rows,
        )
        db._conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-missing", type=int, default=0)
    parser.add_argument("--isolated-total", type=int, default=2990)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from PySide6.QtWidgets import QApplication

    from core.db_manager import DatabaseManager
    from core.paths import database_path, default_mod_library
    from services.library_perf_metrics import (
        get_library_perf_metrics,
        reset_library_perf_metrics,
    )
    from services.mod_library_cache import reset_library_cache
    from services.presence_reconcile import (
        drain_presence_reconcile,
        last_presence_stats,
    )
    from ui.library_view import ALL_GAMES_LABEL, ModLibraryView
    from ui.main_window import PAGE_LIBRARY, MainWindow

    isolated = int(args.isolated_missing) > 0
    tmp_root: Path | None = None
    if isolated:
        import os
        import tempfile

        from core import paths as core_paths

        tmp_root = Path(tempfile.mkdtemp(prefix="smm_refresh_probe_"))
        os.environ.pop("QT_QPA_PLATFORM", None)
        lib = tmp_root / "mod"
        data = tmp_root / "data"
        data.mkdir(parents=True, exist_ok=True)
        DatabaseManager.reset_instance()
        db = DatabaseManager.instance(tmp_root / "probe.db")
        _seed_isolated(db, lib, int(args.isolated_total), int(args.isolated_missing))
        core_paths.default_mod_library = lambda: lib  # type: ignore[method-assign]
        core_paths.data_dir = lambda: data  # type: ignore[method-assign]
        core_paths.database_path = lambda: tmp_root / "probe.db"  # type: ignore[method-assign]
        target = str(lib)
    else:
        import os

        os.environ.pop("QT_QPA_PLATFORM", None)
        DatabaseManager.instance(database_path())
        target = str(default_mod_library())

    reset_library_cache()
    reset_library_perf_metrics()

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1400, 900)
    window.show()
    window.raise_()
    window.activateWindow()
    window.library_view.set_target_root(target)
    window._goto_page(PAGE_LIBRARY)
    _pump(app, 0.4)
    _wait_not_loading(window.library_view, app, timeout=60.0)

    view: ModLibraryView = window.library_view
    # Force All Games so Presence + projection cover the full library.
    try:
        from ui.library_view import GAME_ROLE

        for i in range(view.game_list.count()):
            item = view.game_list.item(i)
            if item is None:
                continue
            key = item.data(GAME_ROLE)
            if key is None or key == "" or str(item.text()).startswith(ALL_GAMES_LABEL):
                view.game_list.setCurrentItem(item)
                break
        view._current_game_filter = ALL_GAMES_LABEL
        view._pending_game_filter = ALL_GAMES_LABEL
    except Exception:  # noqa: BLE001
        view._current_game_filter = ALL_GAMES_LABEL
    _pump(app, 0.2)
    _wait_not_loading(view, app, timeout=60.0)
    reset_library_perf_metrics()

    metrics = get_library_perf_metrics()
    click_t0 = time.perf_counter()
    view.refresh_btn.click()
    handler_wall = (time.perf_counter() - click_t0) * 1000.0
    first_paint_wait = _wait_not_loading(view, app, timeout=60.0)
    apply_done = time.perf_counter()
    snap_after_apply = metrics.snapshot()
    drain_presence_reconcile(timeout=60.0)
    presence_done = time.perf_counter()
    _pump(app, 1.2)
    snap = metrics.snapshot()
    presence = last_presence_stats()
    from core.db_manager import get_db

    db = get_db()
    rows = list(db.iter_mod_backup_key_rows())
    miss = sum(1 for r in rows if str(r.get("folder_present")) == "0")
    live = sum(1 for r in rows if str(r.get("folder_present")) != "0")
    payload = {
        "ts": _now(),
        "isolated": isolated,
        "isolated_missing": int(args.isolated_missing),
        "isolated_total": int(args.isolated_total) if isolated else None,
        "window_visible": bool(window.isVisible()),
        "window_size": [window.width(), window.height()],
        "offscreen_attr": False,
        "total_mods": len(rows),
        "visible_cards": int(snap.visible_cards or len(getattr(view, "_cards", []) or [])),
        "cards_created": snap.cards_created,
        "cards_reused": snap.cards_reused,
        "refresh_handler_ms": snap.extras.get("refresh_handler_ms"),
        "refresh_click_wall_ms": round(handler_wall, 2),
        "ui_blocking_click_ms": snap_after_apply.extras.get("refresh_handler_ms")
        or round(handler_wall, 2),
        "ui_blocking_apply_ms": snap_after_apply.extras.get("apply_on_gui_ms"),
        "ui_blocking_cover_ms": snap.extras.get("cover_schedule_ms"),
        "ui_blocking_projection_flush_ms": snap.extras.get("projection_flush_ms"),
        "wait_until_not_loading_ms": round(first_paint_wait, 2),
        "apply_on_gui_ms": snap_after_apply.extras.get("apply_on_gui_ms"),
        "apply_snapshot_ms": snap_after_apply.extras.get("apply_snapshot_ms"),
        "viewport_bind_ms": snap_after_apply.extras.get("viewport_bind_ms"),
        "cover_schedule_ms": snap.extras.get("cover_schedule_ms"),
        "cover_visible": snap.extras.get("cover_visible"),
        "cover_submitted": snap.extras.get("cover_submitted"),
        "db_snapshot_ms": snap.library_index_load_ms,
        "database_query_ms": snap.database_query_ms,
        "viewmodel_create_ms": snap.viewmodel_create_ms,
        "after_apply_extras": snap_after_apply.extras,
        "after_presence_extras": snap.extras,
        "qt_qpa_platform": __import__("os").environ.get("QT_QPA_PLATFORM") or "",
        "presence": presence,
        "presence_wait_after_apply_ms": round((presence_done - apply_done) * 1000.0, 2),
        "folder_present_live": live,
        "folder_present_miss": miss,
        "current_game_filter": str(getattr(view, "_current_game_filter", "") or ""),
        "extras": snap.extras,
    }
    out = Path(args.out) if args.out else (
        ROOT / "_tmp" / "dumps" / "refresh_probe" / f"probe_{_now().replace(':','')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("Wrote", out)
    window.close()
    if isolated:
        DatabaseManager.reset_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
