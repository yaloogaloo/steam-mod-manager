"""CK3 Sort Mode uses the existing Library Sort Mode + generic Paradox order API."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from core.paths import load_order_dir
from services.deploy_identity import is_frozen_internal_uuid
from services.identity_service import create_mod_identity, identity_create_scope
from services.paradox_activation import (
    CK3_APP_ID,
    CK3_ORDER_FILENAME,
    DLC_LOAD_FILENAME,
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
    apply_order_move,
    load_saved_order,
    persist_load_order,
    workshop_launcher_id,
)
from tests.helpers.identity import patch_library_get_db, write_info_sidecar

CK3 = 1158310
STELLARIS = 281990
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "ck3_sort_mode.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _shutdown_library_views(qapp):
    yield
    from PySide6.QtWidgets import QApplication

    from services.mod_library_cache import reset_library_cache
    from ui.library_view import ModLibraryView

    for widget in list(qapp.topLevelWidgets()):
        if isinstance(widget, ModLibraryView):
            try:
                widget.shutdown_workers()
            except Exception:  # noqa: BLE001
                pass
            widget.hide()
            widget.deleteLater()
    QApplication.processEvents()
    reset_library_cache()



def _configure_ck3(db: DatabaseManager, tmp_path: Path) -> tuple[Path, Path, Path]:
    user_dir = tmp_path / "Paradox" / "Crusader Kings III"
    workshop = tmp_path / "workshop" / "content" / str(CK3)
    library = tmp_path / "mod"
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / DLC_LOAD_FILENAME).write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    db.upsert_game(
        GameInfo(app_id=CK3, name="Crusader Kings III", folder_name="十字军之王Ⅲ")
    )
    db.update_game_deploy_config(
        CK3,
        name="Crusader Kings III",
        install_path=str(tmp_path / "CK3Install"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    return user_dir, workshop, library


def _configure_stellaris(db: DatabaseManager, tmp_path: Path) -> Path:
    user_dir = tmp_path / "Paradox" / "Stellaris"
    workshop = tmp_path / "workshop" / "content" / str(STELLARIS)
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / DLC_LOAD_FILENAME).write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    db.upsert_game(GameInfo(app_id=STELLARIS, name="Stellaris", folder_name="Stellaris"))
    db.update_game_deploy_config(
        STELLARIS,
        name="Stellaris",
        install_path=str(tmp_path / "StellarisInstall"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    return user_dir


def _write_ugc(user_dir: Path, workshop_id: str, *, name: str) -> Path:
    mod_dir = user_dir / "mod"
    mod_dir.mkdir(parents=True, exist_ok=True)
    path = mod_dir / f"ugc_{workshop_id}.mod"
    path.write_text(
        f'version="1.0"\nname="{name}"\n'
        f'path="C:/workshop/{workshop_id}"\n'
        f'remote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )
    return path


def _seed_ck3_mod(
    library: Path,
    db: DatabaseManager,
    *,
    workshop_id: str,
    folder: str,
    deployed: bool = True,
) -> tuple[str, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=folder,
            app_id=CK3,
            game_name="Crusader Kings III",
            operation="import",
        )
    pk = str(created.mod_id)
    entity = str(created.internal_id or "")
    mod_dir = library / "十字军之王Ⅲ" / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / "descriptor.mod").write_text(
        f'name="{folder}"\nremote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )
    write_info_sidecar(
        mod_dir,
        internal_id=entity,
        title=folder,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=CK3,
        game_name="Crusader Kings III",
    )
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(mod_dir),
        folder_present=True,
    )
    if deployed:
        db.update_mod_deploy_status(
            pk,
            deploy_status=DEPLOY_STATUS_DEPLOYED,
            deploy_path=str(mod_dir),
            app_id=CK3,
        )
    return pk, entity


def _filtered_internal_ids(view) -> list[str]:
    return [
        str(getattr(index, "internal_id", "") or "")
        for index, _payload in view._filtered_row_entries
    ]


def _ck3_order_path() -> Path:
    return load_order_dir() / CK3_ORDER_FILENAME


def test_ck3_sort_mode_static_contract() -> None:
    from ui.library_view import ModLibraryView

    view_src = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    card_src = (ROOT / "ui" / "mod_card.py").read_text(encoding="utf-8")
    ui_py = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "ui").glob("*.py")
    )
    assert "CK3SortWidget" not in ui_py
    assert "class CK3Sort" not in ui_py
    assert "dlc_load.json" not in view_src
    assert "dlc_load.json" not in card_src
    assert "launcher-v2.sqlite" not in view_src
    assert "launcher-v2.sqlite" not in card_src
    assert "apply_order_move" in view_src
    assert "_is_paradox_current_game" in view_src
    load_src = inspect.getsource(ModLibraryView._is_load_order_sort_game)
    assert "get_order_backend" in load_src or "_order_backend" in load_src
    assert "_is_paradox_current_game" not in load_src
    assert "_is_wh3_current_game" not in load_src
    move_src = inspect.getsource(ModLibraryView._on_wh3_sort_move)
    assert "apply_order_move" in move_src
    assert "_dal_mod_pk" not in move_src
    assert "_is_paradox_current_game" not in move_src
    drop_src = inspect.getsource(ModLibraryView._on_wh3_sort_drop)
    assert "_dal_mod_pk" not in drop_src
    assert "apply_card_drop" in drop_src
    assert "from services.wh3_activation" not in drop_src


def test_ck3_sort_mode_visible(qapp, tmp_path: Path, db: DatabaseManager) -> None:
    from ui.library_view import ModLibraryView

    _configure_ck3(db, tmp_path)
    view = ModLibraryView()
    view._set_current_game_context("十字军之王Ⅲ", game_id=CK3)
    assert view.btn_wh3_sort_mode.isHidden() is False
    assert not hasattr(view, "btn_ck3_sort_mode")
    assert not hasattr(view, "CK3SortWidget")
    view.deleteLater()


def test_stellaris_sort_mode_still_visible(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from ui.library_view import ModLibraryView

    _configure_stellaris(db, tmp_path)
    view = ModLibraryView()
    view._set_current_game_context("Stellaris", game_id=STELLARIS)
    assert view.btn_wh3_sort_mode.isHidden() is False
    view.deleteLater()


def test_unsupported_game_sort_mode_hidden(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    view = ModLibraryView()
    view._set_current_game_context("Palworld", game_id=1623730)
    assert view.btn_wh3_sort_mode.isHidden()
    view.deleteLater()


def _open_ck3_sort_view(
    qapp,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 3,
    ugc: set[int] | None = None,
):
    from PySide6.QtWidgets import QApplication
    from ui.library_view import ModLibraryView

    patch_library_get_db(monkeypatch, db)
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pks: list[str] = []
    entities: list[str] = []
    workshop_ids: list[str] = []
    write_ugc = set(range(count) if ugc is None else ugc)
    for i in range(count):
        wid = str(9100 + i)
        pk, entity = _seed_ck3_mod(
            library,
            db,
            workshop_id=wid,
            folder=f"CK3Sort{i}",
            deployed=True,
        )
        pks.append(pk)
        entities.append(entity)
        workshop_ids.append(wid)
        if i in write_ugc:
            _write_ugc(user_dir, wid, name=f"CK3Sort{i}")
    persist_load_order(entities, db, app_id=CK3)
    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter("十字军之王Ⅲ")
    view.refresh()
    QApplication.processEvents()
    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    return view, user_dir, pks, entities, workshop_ids


def test_ck3_sort_mode_moves_call_generic_order_api(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication

    recorded: list[tuple[str, str, int]] = []
    real = apply_order_move

    def _spy(token, action, db_arg=None, *, app_id):
        recorded.append((str(token), str(action), int(app_id)))
        return real(token, action, db_arg, app_id=app_id)

    monkeypatch.setattr("services.paradox_activation.apply_order_move", _spy)
    view, user_dir, pks, entities, workshop_ids = _open_ck3_sort_view(
        qapp, tmp_path, db, monkeypatch
    )
    assert view._wh3_sort_mode is True
    assert view.btn_wh3_sort_mode.isHidden() is False
    assert _filtered_internal_ids(view) == entities
    for pk in pks:
        assert pk not in _filtered_internal_ids(view)

    persist_load_order(entities, db, app_id=CK3)
    view._on_wh3_sort_move(entities[1], ORDER_MOVE_TOP)
    QApplication.processEvents()
    assert load_saved_order(app_id=CK3)[:3] == [entities[1], entities[0], entities[2]]

    persist_load_order(entities, db, app_id=CK3)
    view._on_wh3_sort_move(entities[1], ORDER_MOVE_UP)
    QApplication.processEvents()
    assert load_saved_order(app_id=CK3)[:3] == [entities[1], entities[0], entities[2]]

    persist_load_order(entities, db, app_id=CK3)
    view._on_wh3_sort_move(entities[1], ORDER_MOVE_DOWN)
    QApplication.processEvents()
    assert load_saved_order(app_id=CK3)[:3] == [entities[0], entities[2], entities[1]]

    persist_load_order(entities, db, app_id=CK3)
    view._on_wh3_sort_move(entities[0], ORDER_MOVE_BOTTOM)
    QApplication.processEvents()
    assert load_saved_order(app_id=CK3)[:3] == [entities[1], entities[2], entities[0]]

    assert [row[1] for row in recorded] == [
        ORDER_MOVE_TOP,
        ORDER_MOVE_UP,
        ORDER_MOVE_DOWN,
        ORDER_MOVE_BOTTOM,
    ]
    for token, _action, app_id in recorded:
        assert is_frozen_internal_uuid(token)
        assert token not in pks
        assert app_id == CK3_APP_ID == CK3
    saved = json.loads(_ck3_order_path().read_text(encoding="utf-8"))
    assert saved["order"][:3] == [entities[1], entities[2], entities[0]]
    payload = json.loads((user_dir / DLC_LOAD_FILENAME).read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(workshop_ids[1]),
        workshop_launcher_id(workshop_ids[2]),
        workshop_launcher_id(workshop_ids[0]),
    ]
    view.deleteLater()


def test_ck3_sort_writes_ck3_json_and_launcher_order(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication

    view, user_dir, _pks, entities, workshop_ids = _open_ck3_sort_view(
        qapp, tmp_path, db, monkeypatch
    )
    view._on_wh3_sort_move(entities[2], ORDER_MOVE_TOP)
    QApplication.processEvents()
    path = _ck3_order_path()
    assert path.name == "ck3.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["order"][:3] == [entities[2], entities[0], entities[1]]
    payload = json.loads((user_dir / DLC_LOAD_FILENAME).read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(workshop_ids[2]),
        workshop_launcher_id(workshop_ids[0]),
        workshop_launcher_id(workshop_ids[1]),
    ]
    view2 = type(view)()
    view2.set_target_root(view._target_root)
    view2.set_preferred_filter("十字军之王Ⅲ")
    view2.refresh()
    QApplication.processEvents()
    view2.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    assert _filtered_internal_ids(view2) == [entities[2], entities[0], entities[1]]
    view.deleteLater()
    view2.deleteLater()


def test_ck3_unresolved_ugc_stays_in_sort_membership(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``ugc_*.mod`` is an unresolved launcher projection, not exclusion."""
    from services.paradox_activation import sync_paradox_launcher

    view, _user_dir, pks, entities, _wids = _open_ck3_sort_view(
        qapp, tmp_path, db, monkeypatch, count=3, ugc={0, 1}
    )
    shown = _filtered_internal_ids(view)
    assert shown == entities
    assert entities[2] in shown
    assert pks[2] not in shown
    assert load_saved_order(app_id=CK3)[:3] == entities
    report = sync_paradox_launcher(db, app_id=CK3)
    assert entities[2] in report.unresolved
    assert entities[0] not in report.unresolved
    assert entities[1] not in report.unresolved
    view.deleteLater()


def test_ck3_fresh_deploy_without_ugc_appears_in_both_views(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New Deploy with no launcher descriptor is in both deployed filter and Sorting Mode."""
    from PySide6.QtWidgets import QApplication

    from services.deploy import ModDeployer
    from services.paradox_activation import sync_paradox_launcher
    from ui.library_query import FILTER_DEPLOYED
    from ui.library_view import ModLibraryView

    patch_library_get_db(monkeypatch, db)
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    _pk, entity = _seed_ck3_mod(
        library,
        db,
        workshop_id="99001",
        folder="FreshCK3",
        deployed=False,
    )
    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter("十字军之王Ⅲ")
    view.refresh()
    QApplication.processEvents()
    view._set_library_status_filter(FILTER_DEPLOYED)
    QApplication.processEvents()
    assert entity not in _filtered_internal_ids(view)

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    assert not (user_dir / "mod" / "ugc_99001.mod").exists()

    view.refresh()
    QApplication.processEvents()
    view._set_library_status_filter(FILTER_DEPLOYED)
    QApplication.processEvents()
    deployed_ids = _filtered_internal_ids(view)
    assert deployed_ids == [entity]

    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    assert view._wh3_sort_mode is True
    sorting_ids = _filtered_internal_ids(view)
    from services.deploy_identity import is_frozen_internal_uuid

    assert sorting_ids and all(is_frozen_internal_uuid(item) for item in sorting_ids)
    missing = [item for item in deployed_ids if item not in sorting_ids]
    extra = [item for item in sorting_ids if item not in deployed_ids]
    print(f"DEPLOYED_IDS={deployed_ids}")
    print(f"SORTING_IDS={sorting_ids}")
    print(f"MISSING_FROM_SORTING={missing}")
    print(f"EXTRA_IN_SORTING={extra}")
    assert missing == []
    assert extra == []
    assert sorting_ids == deployed_ids
    assert entity in load_saved_order(app_id=CK3)
    report = sync_paradox_launcher(db, app_id=CK3)
    assert entity in report.unresolved
    assert not (user_dir / "mod" / "ugc_99001.mod").exists()
    view.deleteLater()
