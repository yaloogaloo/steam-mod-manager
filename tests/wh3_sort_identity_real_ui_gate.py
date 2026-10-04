"""Real SMM UI gate for WH3 Sort identity (Frozen internal_id).

Restores ``wh3.json`` and ``used_mods.txt`` after the gate.

Run outside pytest isolation:

    python tests/wh3_sort_identity_real_ui_gate.py
"""

from __future__ import annotations

import hashlib
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
from services.order_backend import ORDER_MOVE_TOP, get_order_backend
from services.wh3_activation import (
    WH3_APP_ID,
    WH3_CANONICAL_ORDER_FILENAME,
    load_saved_order,
    load_wh3_game_paths,
    used_mods_path,
)
from ui.library_view import GAME_ID_ROLE
from ui.main_window import (
    APP_NAME,
    ORG_NAME,
    PAGE_LIBRARY,
    SETTING_TARGET,
    MainWindow,
)


def _fail(message: str) -> int:
    print(f"REAL WH3 FAIL: {message}")
    return 1


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _restore(snapshots: dict[Path, bytes | None]) -> None:
    for path, data in snapshots.items():
        if data is None:
            if path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


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


def _filtered_pks(view) -> list[str]:
    return [
        str(getattr(index, "mod_id", "") or "")
        for index, _payload in view._filtered_row_entries
    ]


def main() -> int:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(database_path())
    settings = QSettings(ORG_NAME, APP_NAME)
    target = Path(str(settings.value(SETTING_TARGET, "") or "")).expanduser()
    if not target.is_dir():
        target = Path(r"E:\mod")
    wh3_folder = None
    for name in ("全面战争 战锤Ⅲ", "全面战争：战锤 III", "Warhammer3"):
        candidate = target / name
        if candidate.is_dir():
            wh3_folder = candidate
            break
    if wh3_folder is None:
        return _fail(f"real WH3 library missing under {target}")

    order_path = load_order_dir() / WH3_CANONICAL_ORDER_FILENAME
    paths = load_wh3_game_paths(db)
    used_path = (
        used_mods_path(paths.install_path) if paths.install_path else None
    )
    restore_targets: dict[Path, bytes | None] = {
        order_path: _read_optional(order_path),
        load_order_dir() / "ck3.json": _read_optional(load_order_dir() / "ck3.json"),
        load_order_dir() / "stellaris.json": _read_optional(
            load_order_dir() / "stellaris.json"
        ),
        load_order_dir() / "bg3.json": _read_optional(load_order_dir() / "bg3.json"),
    }
    if used_path is not None:
        restore_targets[used_path] = _read_optional(used_path)
    before_json_sha = (
        _sha256_bytes(restore_targets[order_path])
        if restore_targets[order_path] is not None
        else None
    )
    before_used_sha = (
        _sha256_bytes(restore_targets[used_path])
        if used_path is not None and restore_targets.get(used_path) is not None
        else None
    )
    status = 1
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
        view.set_preferred_filter(wh3_folder.name)
        view.refresh()
        app.processEvents()
        if not _select_game(view, WH3_APP_ID):
            view._set_current_game_context(wh3_folder.name, game_id=WH3_APP_ID)
            app.processEvents()
        if view.btn_wh3_sort_mode.isHidden():
            return _fail("WH3 Sort Mode button is hidden")
        backend = get_order_backend(WH3_APP_ID)
        if type(backend).__name__ != "Wh3OrderBackend":
            return _fail(f"registry did not return Wh3OrderBackend: {backend!r}")
        if not view.btn_wh3_sort_mode.isChecked():
            view.btn_wh3_sort_mode.click()
            app.processEvents()
        if view._wh3_sort_mode is not True:
            return _fail("could not enter WH3 Sort Mode")

        tokens = _filtered_internal_ids(view)
        pks = _filtered_pks(view)
        print("WH3_SORT_TOKENS", len(tokens), tokens[:4])
        print("WH3_SORT_PKS", pks[:4])
        deployed_ids = [
            str(getattr(index, "internal_id", "") or "")
            for index, _payload in view._game_row_entries
            if bool(getattr(index, "deployed", False))
        ]
        if set(tokens) != set(deployed_ids):
            return _fail(
                "WH3 Sorting Mode "
                f"({len(tokens)}) != canonical deployed membership ({len(deployed_ids)})"
            )
        if len(tokens) < 2:
            return _fail("WH3 Sort Mode has fewer than 2 sortable Mods")
        if not all(is_frozen_internal_uuid(tok) for tok in tokens):
            return _fail("WH3 Sort Mode tokens are not Frozen internal_id")
        if any(is_frozen_internal_uuid(pk) for pk in pks):
            return _fail("mod_pk column unexpectedly looks like Frozen UUID")
        if tokens[0] == pks[0]:
            return _fail("Sort token equals mod_pk")

        a, b = tokens[0], tokens[1]
        pk_a, pk_b = pks[0], pks[1]
        view._on_wh3_sort_drop(b, a)
        app.processEvents()
        after_drop = _filtered_internal_ids(view)
        if after_drop[:2] != [b, a]:
            return _fail(f"DnD B onto A failed: {after_drop[:2]!r}")
        view._on_wh3_sort_move(b, ORDER_MOVE_TOP)
        app.processEvents()
        after_move = _filtered_internal_ids(view)
        if after_move[0] != b:
            return _fail(f"Top B failed: {after_move[:2]!r}")
        saved = load_saved_order(db)
        if saved[:2] != after_move[:2]:
            return _fail(f"wh3.json mismatch UI {after_move[:2]!r} vs {saved[:2]!r}")
        payload = json.loads(order_path.read_text(encoding="utf-8"))
        order = payload.get("order") or []
        if not all(is_frozen_internal_uuid(tok) for tok in order):
            return _fail("wh3.json contains a non-UUID token")
        if pk_a in order or pk_b in order:
            return _fail("wh3.json persisted mod_pk")
        if a not in order or b not in order:
            return _fail("wh3.json missing A/B internal_id")

        backend.sync_projection(db=db, library_root=target)
        if used_path is not None and used_path.is_file():
            used_text = used_path.read_text(encoding="utf-8")
            if pk_a in used_text or pk_b in used_text:
                return _fail("used_mods.txt contains mod_pk")
            if a in used_text or b in used_text:
                return _fail("used_mods.txt contains internal_id")
            if "mod \"" not in used_text:
                return _fail("used_mods.txt missing pack projection")

        view.btn_wh3_sort_mode.click()
        app.processEvents()
        view.btn_wh3_sort_mode.click()
        app.processEvents()
        reentered = _filtered_internal_ids(view)
        if reentered[:2] != after_move[:2]:
            return _fail(f"reload lost order: {reentered[:2]!r} vs {after_move[:2]!r}")
        print("REAL WH3 PASS")
        print(f"A={a} pk={pk_a}")
        print(f"B={b} pk={pk_b}")
        print(f"order={after_move[:2]}")
        status = 0
    except Exception as exc:  # noqa: BLE001
        print(f"REAL WH3 FAIL: {exc}")
        status = 1
    finally:
        _restore(restore_targets)
        after_json = (
            _sha256_bytes(order_path.read_bytes()) if order_path.is_file() else None
        )
        if after_json != before_json_sha:
            print("REAL WH3 FAIL: production wh3.json SHA mismatch after restore")
            status = 1
        if used_path is not None:
            after_used = (
                _sha256_bytes(used_path.read_bytes()) if used_path.is_file() else None
            )
            if after_used != before_used_sha:
                print("REAL WH3 FAIL: production used_mods.txt SHA mismatch after restore")
                status = 1
        if status == 0:
            print(f"restored json sha={after_json}")
        DatabaseManager.reset_instance()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
