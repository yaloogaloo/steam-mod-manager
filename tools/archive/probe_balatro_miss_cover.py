#!/usr/bin/env python3
"""小丑牌 MISS cover + Refresh timing probe (visible Qt window)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_ID = 2379780
GAME = "小丑牌"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pump(app, seconds: float) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    deadline = time.monotonic() + max(0.05, float(seconds))
    while time.monotonic() < deadline:
        try:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        except Exception:  # noqa: BLE001
            app.processEvents(50)
        leftover = deadline - time.monotonic()
        if leftover <= 0:
            break
        loop = QEventLoop()
        QTimer.singleShot(min(50, int(leftover * 1000)), loop.quit)
        loop.exec()


def _wait_not_loading(view, app, timeout: float = 60.0) -> float:
    t0 = time.perf_counter()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(app, 0.05)
        if not bool(getattr(view, "_library_load_pending", False)):
            worker = getattr(view, "_load_worker", None)
            if worker is None or not worker.isRunning():
                return (time.perf_counter() - t0) * 1000.0
    return (time.perf_counter() - t0) * 1000.0


def main() -> int:
    import os

    os.environ.pop("QT_QPA_PLATFORM", None)
    from PySide6.QtWidgets import QApplication

    from core.db_manager import DatabaseManager, get_db
    from core.paths import database_path, default_mod_library
    from services.library_perf_metrics import (
        get_library_perf_metrics,
        reset_library_perf_metrics,
    )
    from services.mod_library_cache import reset_library_cache
    from services.mod_metadata_resolver import resolve_cover_path
    from services.presence_reconcile import (
        drain_presence_reconcile,
        last_presence_stats,
    )
    from ui.library_view import GAME_ROLE, ModLibraryView
    from ui.main_window import PAGE_LIBRARY, MainWindow

    DatabaseManager.instance(database_path())
    reset_library_cache()
    reset_library_perf_metrics()

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1400, 900)
    window.show()
    window.raise_()
    window.activateWindow()
    window.library_view.set_target_root(str(default_mod_library()))
    window._goto_page(PAGE_LIBRARY)
    _pump(app, 0.4)
    _wait_not_loading(window.library_view, app, timeout=60.0)

    view: ModLibraryView = window.library_view
    # Select 小丑牌
    selected = False
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item is None:
            continue
        text = str(item.text() or "")
        key = item.data(GAME_ROLE)
        if GAME in text or key == GAME:
            view.game_list.setCurrentItem(item)
            selected = True
            break
    if not selected:
        view._current_game_filter = GAME
        view._pending_game_filter = GAME
    _pump(app, 0.3)
    _wait_not_loading(view, app, timeout=60.0)
    reset_library_perf_metrics()

    metrics = get_library_perf_metrics()
    click_t0 = time.perf_counter()
    view.refresh_btn.click()
    click_ms = (time.perf_counter() - click_t0) * 1000.0
    wait_ms = _wait_not_loading(view, app, timeout=60.0)
    snap_after_apply = metrics.snapshot()
    drain_presence_reconcile(timeout=60.0)
    _pump(app, 1.5)
    snap = metrics.snapshot()
    presence = last_presence_stats()

    db = get_db()
    rows = db.list_mod_list_items(game_id=APP_ID)
    miss_rows = [r for r in rows if r.get("folder_absent")]
    cover_ok = 0
    cover_detail = []
    for r in miss_rows:
        mid = str(r.get("internal_id") or "")
        card_cover = str(r.get("cover_path") or "")
        detail = resolve_cover_path(mid, Path(str(r.get("managed_path") or "")))
        same = False
        if card_cover and detail is not None:
            try:
                same = Path(card_cover).resolve() == detail.resolve()
            except OSError:
                same = False
        visible_file = bool(card_cover) and Path(card_cover).is_file()
        if visible_file:
            cover_ok += 1
        cover_detail.append(
            {
                "mod_id": mid,
                "card_cover": card_cover,
                "detail_cover": str(detail) if detail else "",
                "same_source": same,
                "card_file_exists": visible_file,
            }
        )

    # Bound cards in viewport — check pixmap not placeholder only via cover field
    bound_covers = []
    for card in getattr(view, "_cards", []) or []:
        data = getattr(card, "_card_data", None)
        if data is None or not bool(getattr(data, "folder_absent", False)):
            continue
        bound_covers.append(
            {
                "mod_id": card._mod_id(),
                "cover": str(getattr(data, "cover", "") or ""),
                "cover_file": Path(str(getattr(data, "cover", "") or "")).is_file()
                if str(getattr(data, "cover", "") or "")
                else False,
            }
        )

    # Second refresh stability
    reset_library_perf_metrics()
    view.refresh_btn.click()
    _wait_not_loading(view, app, timeout=60.0)
    drain_presence_reconcile(timeout=60.0)
    _pump(app, 1.0)
    rows2 = db.list_mod_list_items(game_id=APP_ID)
    miss2 = [r for r in rows2 if r.get("folder_absent")]
    cover_ok2 = sum(
        1
        for r in miss2
        if str(r.get("cover_path") or "") and Path(str(r.get("cover_path"))).is_file()
    )

    payload = {
        "ts": _now(),
        "game": GAME,
        "app_id": APP_ID,
        "window_visible": bool(window.isVisible()),
        "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM") or "",
        "total_game_mods": len(rows),
        "miss_count": len(miss_rows),
        "cover_visible": f"{cover_ok} / {len(miss_rows)}",
        "cover_visible_second_refresh": f"{cover_ok2} / {len(miss2)}",
        "same_source_count": sum(1 for c in cover_detail if c["same_source"]),
        "refresh_click_ms": round(click_ms, 2),
        "ui_blocking_click_ms": snap_after_apply.extras.get("refresh_handler_ms"),
        "ui_blocking_apply_ms": snap_after_apply.extras.get("apply_on_gui_ms"),
        "apply_snapshot_ms": snap_after_apply.extras.get("apply_snapshot_ms"),
        "viewport_bind_ms": snap_after_apply.extras.get("viewport_bind_ms"),
        "cover_schedule_ms": snap.extras.get("cover_schedule_ms"),
        "projection_flush_ms": snap.extras.get("projection_flush_ms"),
        "wait_until_not_loading_ms": round(wait_ms, 2),
        "db_snapshot_ms": snap.library_index_load_ms,
        "presence": presence,
        "bound_miss_cards": bound_covers,
        "covers": cover_detail,
        "extras": snap.extras,
    }
    out = ROOT / "_tmp" / "dumps" / "refresh_probe" / f"balatro_miss_{_now().replace(':','')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("Wrote", out)
    window.close()
    return 0 if cover_ok >= 11 else 1


if __name__ == "__main__":
    raise SystemExit(main())
