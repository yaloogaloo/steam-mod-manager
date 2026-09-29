"""Real BG3 undeploy load-order gate.

Uses two already-deployed production Mods. Restores ``modsettings.lsx``,
``bg3.json``, and the undeployed pak/manifest/status afterwards.

Run outside pytest isolation:

    python tests/bg3_undeploy_order_real_gate.py
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

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.paths import database_path, load_order_dir
from services.bg3_activation import (
    BG3_APP_ID,
    BG3_ORDER_FILENAME,
    apply_order_move,
    default_bg3_modsettings_path,
    inspect_bg3_membership,
    load_saved_order,
    persist_load_order,
)
from services.bg3_modsettings import GUSTAVX_UUID, parse_bg3_modsettings
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME
from services.paradox_activation import ORDER_MOVE_TOP
from ui.main_window import APP_NAME, ORG_NAME, SETTING_TARGET


def _fail(message: str) -> int:
    print(f"REAL UNDEPLOY FAIL: {message}")
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


def _parse_uuids(text: str) -> list[str]:
    _version, nodes = parse_bg3_modsettings(text)
    return [node.uuid for node in nodes]


def _unmanaged(uuids: list[str], managed: set[str]) -> list[str]:
    return [uuid for uuid in uuids[1:] if uuid not in managed]


def _row_for(db: DatabaseManager, internal_id: str) -> dict | None:
    for row in db.list_mod_list_items(app_id=BG3_APP_ID):
        if str(row.get("internal_id") or "") == internal_id:
            return row
    return None


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
    restore_targets: dict[Path, bytes | None] = {
        order_path: _read_optional(order_path),
        lsx_path: lsx_path.read_bytes(),
        load_order_dir() / "ck3.json": _read_optional(load_order_dir() / "ck3.json"),
        load_order_dir() / "stellaris.json": _read_optional(
            load_order_dir() / "stellaris.json"
        ),
        load_order_dir() / "wh3.json": _read_optional(load_order_dir() / "wh3.json"),
    }
    before_lsx_sha = _sha256_bytes(restore_targets[lsx_path] or b"")
    before_json_sha = (
        _sha256_bytes(restore_targets[order_path])
        if restore_targets[order_path] is not None
        else None
    )
    original_uuids = _parse_uuids((restore_targets[lsx_path] or b"").decode("utf-8"))
    pk_b = ""
    info_b = None
    status = 1
    try:
        report = inspect_bg3_membership(db)
        singles = [member for member in report.sortable if len(member.uuids) == 1]
        if len(singles) < 2:
            return _fail(f"need two real single-uuid BG3 Mods, got {len(singles)}")
        member_a, member_b = singles[0], singles[1]
        iid_a, iid_b = member_a.internal_id, member_b.internal_id
        uuid_a, uuid_b = member_a.uuids[0], member_b.uuids[0]
        row_a = _row_for(db, iid_a)
        row_b = _row_for(db, iid_b)
        if row_a is None or row_b is None:
            return _fail("could not resolve managed rows for A/B")
        folder_a = Path(str(row_a.get("managed_path") or row_a.get("last_known_path") or ""))
        folder_b = Path(str(row_b.get("managed_path") or row_b.get("last_known_path") or ""))
        if not folder_a.is_dir() or not folder_b.is_dir():
            return _fail("managed folders for A/B are missing")
        pk_b = str(row_b.get("mod_id") or "")
        info_b = db.get_mod_deploy_info(pk_b) if pk_b else None

        for path in (*member_a.pak_paths, *member_b.pak_paths):
            pak = Path(path)
            if not pak.is_file():
                return _fail(f"deployed pak missing: {pak}")
            restore_targets[pak] = pak.read_bytes()
        for folder in (folder_a, folder_b):
            for name in ("deploy_manifest.json", "internal_id", "metadata.json"):
                path = folder / INFO_DIR_NAME / name
                restore_targets[path] = _read_optional(path)

        sortable_uuids = {uuid for member in report.sortable for uuid in member.uuids}
        original_unmanaged = _unmanaged(original_uuids, sortable_uuids)

        deployer = ModDeployer(library_root=target, db=db)
        deployed_a = deployer.deploy_mod(iid_a)
        deployed_b = deployer.deploy_mod(iid_b)
        if not deployed_a.get("success"):
            return _fail(f"Deploy A failed: {deployed_a}")
        if not deployed_b.get("success"):
            return _fail(f"Deploy B failed: {deployed_b}")

        persist_load_order(None, db, library_root=target)
        apply_order_move(iid_a, ORDER_MOVE_TOP, db, library_root=target)
        apply_order_move(iid_b, ORDER_MOVE_TOP, db, library_root=target)
        saved = load_saved_order()
        if saved[:2] != [iid_b, iid_a]:
            return _fail(f"sort B→A failed: {saved[:2]!r}")
        live_after_sort = _parse_uuids(lsx_path.read_text(encoding="utf-8"))
        if live_after_sort[0] != GUSTAVX_UUID:
            return _fail("GustavX is not first after sort")
        if live_after_sort.index(uuid_b) >= live_after_sort.index(uuid_a):
            return _fail("lsx UUID order is not B then A")
        if _unmanaged(live_after_sort, sortable_uuids) != original_unmanaged:
            return _fail("unmanaged BG3 entries changed during sort")

        und = deployer.undeploy_mod(iid_b)
        if not und.get("success"):
            return _fail(f"Undeploy B failed: {und}")

        saved_after = load_saved_order()
        if iid_b in saved_after:
            return _fail(f"bg3.json still contains B: {saved_after}")
        if iid_a not in saved_after:
            return _fail("bg3.json lost A after undeploying B")
        live_after = _parse_uuids(lsx_path.read_text(encoding="utf-8"))
        if live_after[0] != GUSTAVX_UUID:
            return _fail("GustavX was removed")
        if uuid_b in live_after:
            return _fail("modsettings.lsx still contains B UUID")
        if uuid_a not in live_after:
            return _fail("modsettings.lsx lost A UUID")
        remaining_managed = sortable_uuids - {uuid_b}
        if _unmanaged(live_after, remaining_managed) != original_unmanaged:
            return _fail("unmanaged BG3 entries changed")
        if "ModOrder" in lsx_path.read_text(encoding="utf-8"):
            return _fail("projection created ModOrder")
        payload = json.loads(order_path.read_text(encoding="utf-8"))
        if iid_b in payload.get("order", []):
            return _fail("bg3.json payload still lists B")
        print("REAL UNDEPLOY PASS")
        print(f"A={iid_a} uuid={uuid_a}")
        print(f"B={iid_b} uuid={uuid_b} removed")
        print(f"bg3.json remaining={len(saved_after)}")
        status = 0
    except Exception as exc:  # noqa: BLE001
        print(f"REAL UNDEPLOY FAIL: {exc}")
        status = 1
    finally:
        _restore(restore_targets)
        if pk_b and info_b is not None:
            try:
                db.update_mod_deploy_status(
                    pk_b,
                    deploy_status=str(info_b.deploy_status or DEPLOY_STATUS_DEPLOYED),
                    deploy_path=str(getattr(info_b, "deploy_path", "") or ""),
                )
            except Exception:  # noqa: BLE001
                print("REAL UNDEPLOY WARN: could not restore B deploy_status")
        after_lsx = _sha256_bytes(lsx_path.read_bytes()) if lsx_path.is_file() else None
        after_json = (
            _sha256_bytes(order_path.read_bytes()) if order_path.is_file() else None
        )
        if after_lsx != before_lsx_sha:
            print("REAL UNDEPLOY FAIL: production modsettings.lsx SHA mismatch after restore")
            status = 1
        if after_json != before_json_sha:
            print("REAL UNDEPLOY FAIL: production bg3.json SHA mismatch after restore")
            status = 1
        if status == 0:
            print(f"restored lsx sha={after_lsx}")
            print(f"restored json sha={after_json}")
        DatabaseManager.reset_instance()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
