"""Real SMM UI gate for CK3 Sort Mode.

Run outside pytest isolation:

    python tests/ck3_sort_mode_real_ui_gate.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SMM_LIBRARY_SYNC"] = "1"

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.paths import database_path, load_order_dir
from services.deploy_identity import is_frozen_internal_uuid
from services.paradox_activation import (
    CK3_APP_ID,
    CK3_ORDER_FILENAME,
    DLC_LOAD_FILENAME,
    LAUNCHER_DB_FILENAME,
    ORDER_MOVE_TOP,
    STELLARIS_APP_ID,
    load_saved_order,
    resolve_paradox_user_dir,
)
from ui.library_view import GAME_ID_ROLE
from ui.main_window import (
    APP_NAME,
    ORG_NAME,
    PAGE_LIBRARY,
    SETTING_TARGET,
    MainWindow,
)

CK3 = CK3_APP_ID
STELLARIS = STELLARIS_APP_ID


def _fail(message: str) -> int:
    print(f"REAL UI FAIL: {message}")
    return 1


def _select_game(view, app_id: int) -> bool:
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if int(item.data(GAME_ID_ROLE) or 0) == int(app_id):
            view.game_list.setCurrentItem(item)
            QApplication.processEvents()
            return True
    return False


def _filtered_internal_ids(view) -> list[str]:
    return [
        str(getattr(index, "internal_id", "") or "")
        for index, _payload in view._filtered_row_entries
    ]


def _restore(snapshots: dict[Path, bytes | None]) -> None:
    for path, data in snapshots.items():
        if data is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def main() -> int:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(database_path())
    settings = QSettings(ORG_NAME, APP_NAME)
    target = Path(str(settings.value(SETTING_TARGET, "") or "")).expanduser()
    if not target.is_dir():
        target = Path(r"E:\mod")
    ck3_folder = target / "十字军之王Ⅲ"
    if not ck3_folder.is_dir():
        return _fail(f"real CK3 library missing: {ck3_folder}")

    user_dir = resolve_paradox_user_dir(db, app_id=CK3)
    order_path = load_order_dir() / CK3_ORDER_FILENAME
    dlc_path = user_dir / DLC_LOAD_FILENAME
    sqlite_path = user_dir / LAUNCHER_DB_FILENAME
    snapshots = {
        order_path: order_path.read_bytes() if order_path.is_file() else None,
        dlc_path: dlc_path.read_bytes() if dlc_path.is_file() else None,
        sqlite_path: sqlite_path.read_bytes() if sqlite_path.is_file() else None,
    }

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        if not window.isVisible():
            return _fail("MainWindow.show() did not make the window visible")
        window._goto_page(PAGE_LIBRARY)
        app.processEvents()
        view = window.library_view
        view.set_target_root(str(target))
        view.set_preferred_filter("十字军之王Ⅲ")
        view.refresh()
        app.processEvents()
        if not _select_game(view, CK3):
            return _fail("CK3 is not in the real Library sidebar")
        if view.btn_wh3_sort_mode.isHidden():
            return _fail("CK3 Sort Mode button is hidden")
        if not view.btn_wh3_sort_mode.isChecked():
            view.btn_wh3_sort_mode.click()
            app.processEvents()
        if view._wh3_sort_mode is not True:
            return _fail("could not enter CK3 Sort Mode")
        tokens = _filtered_internal_ids(view)
        print("CK3_SORT_TOKENS", len(tokens), tokens[:8])
        if not tokens:
            return _fail("CK3 Sort Mode has no sortable Mods")
        if not all(is_frozen_internal_uuid(tok) for tok in tokens):
            return _fail("CK3 Sort Mode tokens are not Frozen internal_id")
        if len(tokens) < 2:
            return _fail("need two sortable CK3 Mods to prove reorder")

        before = list(load_saved_order(app_id=CK3))
        moving = tokens[-1]
        view._on_wh3_sort_move(moving, ORDER_MOVE_TOP)
        app.processEvents()
        after = load_saved_order(app_id=CK3)
        if not after or after[0] != moving:
            return _fail(f"order file top is {after[:1]!r}, expected {moving}")
        if after == before:
            return _fail("ck3.json order did not change")
        saved = json.loads(order_path.read_text(encoding="utf-8"))
        if saved.get("order", [None])[0] != moving:
            return _fail("ck3.json first token mismatch")
        payload = json.loads(dlc_path.read_text(encoding="utf-8"))
        enabled = [
            str(item) for item in payload.get("enabled_mods") or [] if str(item).strip()
        ]
        if not enabled:
            return _fail("Launcher enabled_mods empty after CK3 sort")
        if "ugc_" not in enabled[0]:
            return _fail(f"Launcher first entry is not ugc_*: {enabled[0]}")

        view.btn_wh3_sort_mode.click()
        app.processEvents()
        view.refresh()
        app.processEvents()
        if not view.btn_wh3_sort_mode.isChecked():
            view.btn_wh3_sort_mode.click()
            app.processEvents()
        reloaded = _filtered_internal_ids(view)
        if not reloaded or reloaded[0] != moving:
            return _fail(f"reload order {reloaded[:1]!r} != {moving}")

        if not _select_game(view, STELLARIS):
            return _fail("Stellaris missing from real Library")
        if view.btn_wh3_sort_mode.isHidden():
            return _fail("Stellaris Sort Mode hidden after CK3 sort")

        print("REAL UI PASS")
        print(
            json.dumps(
                {
                    "visible": True,
                    "entered_sort_mode": True,
                    "moved_internal_id": moving,
                    "ck3_json": str(order_path),
                    "launcher_first": enabled[0],
                    "reload_first": reloaded[0],
                    "stellaris_sort_visible": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        _restore(snapshots)
        try:
            view = getattr(window, "library_view", None)
            if view is not None:
                view.cancel_pending_library_load()
        except Exception:
            pass
        window.hide()
        window.deleteLater()
        app.processEvents()
        DatabaseManager.reset_instance()


if __name__ == "__main__":
    raise SystemExit(main())
