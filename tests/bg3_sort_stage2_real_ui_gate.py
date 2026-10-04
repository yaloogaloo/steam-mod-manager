"""Real SMM UI gate for BG3 Sort Mode (Stage 2).

Writes live ``modsettings.lsx`` only through the production projector, then
restores the pre-gate bytes and verifies SHA-256.

Run outside pytest isolation:

    python tests/bg3_sort_stage2_real_ui_gate.py
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
from services.bg3_activation import (
    BG3_APP_ID,
    BG3_ORDER_FILENAME,
    default_bg3_modsettings_path,
    inspect_bg3_membership,
    load_saved_order,
)
from services.bg3_modsettings import GUSTAVX_UUID, parse_bg3_modsettings
from services.deploy_identity import is_frozen_internal_uuid
from services.file_ops import INFO_DIR_NAME
from services.order_backend import ORDER_MOVE_TOP, get_order_backend
from services.paradox_activation import CK3_APP_ID, STELLARIS_APP_ID
from services.wh3_activation import WH3_APP_ID
from ui.library_view import GAME_ID_ROLE
from ui.main_window import (
    APP_NAME,
    ORG_NAME,
    PAGE_LIBRARY,
    SETTING_TARGET,
    MainWindow,
)


def _fail(message: str) -> int:
    print(f"REAL UI FAIL: {message}")
    return 1


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def main() -> int:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(database_path())
    settings = QSettings(ORG_NAME, APP_NAME)
    target = Path(str(settings.value(SETTING_TARGET, "") or "")).expanduser()
    if not target.is_dir():
        target = Path(r"E:\mod")
    bg3_folder = target / "博德之门Ⅲ"
    if not bg3_folder.is_dir():
        return _fail(f"real BG3 library missing: {bg3_folder}")

    lsx_path = default_bg3_modsettings_path()
    if not lsx_path.is_file():
        return _fail(f"live modsettings.lsx missing: {lsx_path}")
    order_path = load_order_dir() / BG3_ORDER_FILENAME
    collateral = {
        load_order_dir() / "ck3.json": _read_optional(load_order_dir() / "ck3.json"),
        load_order_dir() / "stellaris.json": _read_optional(
            load_order_dir() / "stellaris.json"
        ),
        load_order_dir() / "wh3.json": _read_optional(load_order_dir() / "wh3.json"),
    }
    restore_targets = {
        order_path: _read_optional(order_path),
        lsx_path: lsx_path.read_bytes(),
    }
    before_lsx_sha = _sha256_bytes(restore_targets[lsx_path] or b"")
    before_json_sha = (
        _sha256_bytes(restore_targets[order_path])
        if restore_targets[order_path] is not None
        else None
    )

    backend = get_order_backend(BG3_APP_ID)
    if type(backend).__name__ != "Bg3OrderBackend":
        return _fail(f"registry did not return BG3OrderBackend: {backend!r}")
    report = inspect_bg3_membership(db)
    if len(report.sortable) < 2:
        return _fail(f"need two real sortable BG3 Mods, got {len(report.sortable)}")
    for member in report.sortable[:2]:
        if not member.uuids or not member.pak_paths:
            return _fail(f"sortable member missing pak/uuid: {member.internal_id}")
        if not Path(member.pak_paths[0]).is_file():
            return _fail(f"deployed pak missing: {member.pak_paths[0]}")

    identity_files: dict[Path, bytes | None] = {}

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
        view.set_preferred_filter("博德之门Ⅲ")
        view.refresh()
        app.processEvents()
        if not _select_game(view, BG3_APP_ID):
            return _fail("BG3 is not in the real Library sidebar")
        if view.btn_wh3_sort_mode.isHidden():
            return _fail("BG3 Sort Mode button is hidden")
        if type(view._order_backend()).__name__ != "Bg3OrderBackend":
            return _fail("Library Sort UI is not dispatching to BG3OrderBackend")
        if not view.btn_wh3_sort_mode.isChecked():
            view.btn_wh3_sort_mode.click()
            app.processEvents()
        if view._wh3_sort_mode is not True:
            return _fail("could not enter BG3 Sort Mode")

        tokens = _filtered_internal_ids(view)
        print("BG3_SORT_TOKENS", len(tokens), tokens[:8])
        deployed_ids = [
            str(getattr(index, "internal_id", "") or "")
            for index, _payload in view._game_row_entries
            if bool(getattr(index, "deployed", False))
        ]
        if set(tokens) != set(deployed_ids):
            return _fail(
                "BG3 Sorting Mode "
                f"({len(tokens)}) != canonical deployed membership ({len(deployed_ids)})"
            )
        if len(tokens) < 2:
            return _fail("BG3 Sort Mode has fewer than 2 sortable Mods")
        if not all(is_frozen_internal_uuid(tok) for tok in tokens):
            return _fail("BG3 Sort Mode tokens are not Frozen internal_id")

        by_iid = {m.internal_id: m for m in report.sortable}
        singles = [
            tok
            for tok in tokens
            if tok in by_iid and len(by_iid[tok].uuids) == 1
        ]
        if len(singles) >= 2:
            a, b = singles[0], singles[1]
        else:
            a, b = tokens[0], tokens[1]
        if a not in by_iid or b not in by_iid:
            return _fail("visible sort tokens are not in membership report")
        uuid_a = by_iid[a].uuids[0]
        uuid_b = by_iid[b].uuids[0]

        for iid in (a, b):
            managed = None
            for row in db.list_mod_list_items(app_id=BG3_APP_ID):
                if str(row.get("internal_id") or "") == iid:
                    managed = Path(str(row.get("managed_path") or ""))
                    break
            if managed and managed.is_dir():
                for name in ("deploy_manifest.json", "internal_id", "metadata.json"):
                    path = managed / INFO_DIR_NAME / name
                    identity_files[path] = _read_optional(path)

        view._on_wh3_sort_move(b, ORDER_MOVE_TOP)
        app.processEvents()
        after_tokens = _filtered_internal_ids(view)
        if after_tokens[:2] != [b, a]:
            return _fail(f"UI order after move {after_tokens[:2]!r} != {[b, a]!r}")
        saved = load_saved_order()
        if saved[:2] != [b, a]:
            return _fail(f"bg3.json after move {saved[:2]!r} != {[b, a]!r}")
        payload = json.loads(order_path.read_text(encoding="utf-8"))
        if payload.get("order", [])[:2] != [b, a]:
            return _fail("bg3.json payload mismatch")
        if uuid_a in order_path.read_text(encoding="utf-8"):
            return _fail("bg3.json stored a BG3 UUID")
        live_text = lsx_path.read_text(encoding="utf-8")
        _, nodes = parse_bg3_modsettings(live_text)
        live_uuids = [node.uuid for node in nodes]
        if live_uuids[0] != GUSTAVX_UUID:
            return _fail("GustavX is not first after projection")
        if live_uuids.index(uuid_b) >= live_uuids.index(uuid_a):
            return _fail("live lsx UUID order is not B then A")
        if "ModOrder" in live_text:
            return _fail("projection created ModOrder")

        view.btn_wh3_sort_mode.click()
        app.processEvents()
        view.refresh()
        app.processEvents()
        if not view.btn_wh3_sort_mode.isChecked():
            view.btn_wh3_sort_mode.click()
            app.processEvents()
        reloaded = _filtered_internal_ids(view)
        if reloaded[:2] != [b, a]:
            return _fail(f"reload order {reloaded[:2]!r} != {[b, a]!r}")

        for app_id, label in (
            (CK3_APP_ID, "CK3"),
            (STELLARIS_APP_ID, "Stellaris"),
            (WH3_APP_ID, "WH3"),
        ):
            if not _select_game(view, app_id):
                print(f"WARN {label} not in real Library sidebar")
                continue
            if view.btn_wh3_sort_mode.isHidden():
                return _fail(f"{label} Sort Mode hidden after BG3 sort")

        for path, before in collateral.items():
            after = _read_optional(path)
            if after != before:
                return _fail(f"collateral load-order file changed: {path.name}")
        for path, before in identity_files.items():
            after = _read_optional(path)
            if after != before:
                return _fail(f"identity/deploy file changed: {path}")

        print("REAL UI PASS")
        print(
            json.dumps(
                {
                    "visible": True,
                    "entered_sort_mode": True,
                    "a": a,
                    "b": b,
                    "uuid_a": uuid_a,
                    "uuid_b": uuid_b,
                    "bg3_json": str(order_path),
                    "reload_first": reloaded[0],
                    "gustavx_first": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        restore_ok = True
        _restore(restore_targets)
        after_restore_lsx = _sha256_file(lsx_path)
        after_restore_json = _sha256_file(order_path)
        if after_restore_lsx != before_lsx_sha:
            print(
                "REAL UI FAIL: lsx SHA after restore "
                f"{after_restore_lsx} != {before_lsx_sha}"
            )
            restore_ok = False
        if before_json_sha is None:
            if order_path.exists():
                print("REAL UI FAIL: bg3.json existed after restore")
                restore_ok = False
        elif after_restore_json != before_json_sha:
            print(
                "REAL UI FAIL: bg3.json SHA after restore "
                f"{after_restore_json} != {before_json_sha}"
            )
            restore_ok = False
        if restore_ok:
            print("PRODUCTION RESTORE PASS", after_restore_lsx)
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
        if not restore_ok:
            raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
