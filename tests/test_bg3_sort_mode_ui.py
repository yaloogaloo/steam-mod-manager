"""BG3 Sort Mode uses the common Library Sort UI + order-backend registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from services.bg3_activation import BG3_APP_ID, load_saved_order
from services.bg3_modsettings import GUSTAVX_UUID, parse_bg3_modsettings
from services.deploy_identity import is_frozen_internal_uuid
from services.file_ops import INFO_DIR_NAME
from services.order_backend import (
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
)
from services.paradox_activation import CK3_APP_ID, STELLARIS_APP_ID
from tests.helpers.bg3_lspk import meta_lsx_bytes, write_lspk_v18
from tests.helpers.identity import (
    create_other_test_mod,
    patch_library_get_db,
    write_info_sidecar,
)
from tests.test_bg3_sort_stage2 import UUID_A, UUID_B, UUID_UNMANAGED, _lsx, _short

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "bg3_sort_ui.db")
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


def _configure_bg3(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    library = tmp_path / "mod"
    order = tmp_path / "bg3.json"
    lsx = tmp_path / "modsettings.lsx"
    monkeypatch.setenv("SMM_BG3_ORDER", str(order))
    monkeypatch.setenv("SMM_BG3_MODSETTINGS", str(lsx))
    db.upsert_game(
        GameInfo(app_id=BG3_APP_ID, name="Baldur's Gate 3", folder_name="博德之门Ⅲ")
    )
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        install_path=str(tmp_path / "BG3Install"),
        mod_path=str(tmp_path / "GameMods"),
    )
    return library, order, lsx


def _seed(
    library: Path,
    db: DatabaseManager,
    tmp_path: Path,
    *,
    folder: str,
    uuid: str,
    name: str,
) -> str:
    created = create_other_test_mod(
        db,
        title=name,
        external_id=f"nexus-{folder}",
        app_id=BG3_APP_ID,
        game_name="Baldur's Gate 3",
    )
    pk = str(created.mod_id)
    iid = str(created.internal_id or "")
    mod_dir = library / "博德之门Ⅲ" / folder
    mod_dir.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        mod_dir,
        internal_id=iid,
        title=name,
        external_id=f"nexus-{folder}",
        workspace_id=str(created.workspace_id or ""),
        app_id=BG3_APP_ID,
        game_name="Baldur's Gate 3",
    )
    pak = tmp_path / "game_mods" / f"{folder}.pak"
    pak.parent.mkdir(parents=True, exist_ok=True)
    write_lspk_v18(
        pak,
        {f"Mods/{folder}/meta.lsx": meta_lsx_bytes(uuid=uuid, name=name, folder=folder)},
    )
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / "deploy_manifest.json").write_text(
        json.dumps(
            {
                "mod_id": pk,
                "internal_id": iid,
                "deploy_time": "2026-01-01T00:00:00",
                "deploy_type": "pak_mod_path",
                "files": [{"source": str(pak), "target": str(pak), "type": "pak"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(pk, last_known_path=str(mod_dir), folder_present=True)
    db.update_mod_deploy_status(
        pk, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path=str(pak)
    )
    return iid


def _filtered_internal_ids(view) -> list[str]:
    return [
        str(getattr(index, "internal_id", "") or "")
        for index, _payload in view._filtered_row_entries
    ]


def test_bg3_sort_mode_visible(qapp, tmp_path: Path, db: DatabaseManager, monkeypatch):
    from ui.library_view import ModLibraryView

    _configure_bg3(db, tmp_path, monkeypatch)
    view = ModLibraryView()
    view._set_current_game_context("博德之门Ⅲ", game_id=BG3_APP_ID)
    assert view.btn_wh3_sort_mode.isHidden() is False
    assert not hasattr(view, "btn_bg3_sort_mode")
    view.deleteLater()


def test_stellaris_and_ck3_still_visible(qapp, tmp_path: Path, db: DatabaseManager):
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=STELLARIS_APP_ID, name="Stellaris", folder_name="Stellaris"))
    db.upsert_game(
        GameInfo(app_id=CK3_APP_ID, name="Crusader Kings III", folder_name="十字军之王Ⅲ")
    )
    view = ModLibraryView()
    view._set_current_game_context("Stellaris", game_id=STELLARIS_APP_ID)
    assert view.btn_wh3_sort_mode.isHidden() is False
    view._set_current_game_context("十字军之王Ⅲ", game_id=CK3_APP_ID)
    assert view.btn_wh3_sort_mode.isHidden() is False
    view.deleteLater()


def test_unsupported_game_sort_mode_hidden(qapp, tmp_path: Path, db: DatabaseManager):
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    view = ModLibraryView()
    view._set_current_game_context("Palworld", game_id=1623730)
    assert view.btn_wh3_sort_mode.isHidden()
    view.deleteLater()


def _open_bg3_sort(qapp, tmp_path, db, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from ui.library_view import ModLibraryView

    patch_library_get_db(monkeypatch, db)
    library, order_path, lsx_path = _configure_bg3(db, tmp_path, monkeypatch)
    iid_a = _seed(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    iid_b = _seed(library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B")
    lsx_path.write_text(
        _lsx(
            _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
            _short(UUID_B, folder="ModB", name="B"),
        ),
        encoding="utf-8",
    )
    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter("博德之门Ⅲ")
    view.refresh()
    QApplication.processEvents()
    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    return view, order_path, lsx_path, iid_a, iid_b


def test_bg3_top_up_down_bottom_and_drop(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication

    view, order_path, lsx_path, iid_a, iid_b = _open_bg3_sort(
        qapp, tmp_path, db, monkeypatch
    )
    assert view._wh3_sort_mode is True
    shown = _filtered_internal_ids(view)
    assert shown == [iid_a, iid_b]
    assert all(is_frozen_internal_uuid(token) for token in shown)

    view._on_wh3_sort_move(iid_b, ORDER_MOVE_TOP)
    QApplication.processEvents()
    assert load_saved_order() == [iid_b, iid_a]
    assert _filtered_internal_ids(view) == [iid_b, iid_a]

    view._on_wh3_sort_move(iid_b, ORDER_MOVE_DOWN)
    QApplication.processEvents()
    assert load_saved_order() == [iid_a, iid_b]

    view._on_wh3_sort_move(iid_b, ORDER_MOVE_UP)
    QApplication.processEvents()
    assert load_saved_order() == [iid_b, iid_a]

    view._on_wh3_sort_move(iid_b, ORDER_MOVE_BOTTOM)
    QApplication.processEvents()
    assert load_saved_order() == [iid_a, iid_b]

    view._on_wh3_sort_drop(iid_b, iid_a)
    QApplication.processEvents()
    assert load_saved_order() == [iid_b, iid_a]
    payload = json.loads(order_path.read_text(encoding="utf-8"))
    assert payload["order"] == [iid_b, iid_a]
    assert UUID_A not in order_path.read_text(encoding="utf-8")
    _, nodes = parse_bg3_modsettings(lsx_path.read_text(encoding="utf-8"))
    uuids = [n.uuid for n in nodes]
    assert uuids[0] == GUSTAVX_UUID
    assert uuids.index(UUID_B) < uuids.index(UUID_A)
    assert UUID_UNMANAGED in uuids
    view.deleteLater()


def test_bg3_token_is_internal_id_not_pk() -> None:
    card_src = (ROOT / "ui" / "mod_card.py").read_text(encoding="utf-8")
    assert "application/x-smm-load-order-token" in card_src
    assert "_entity_internal_id" in card_src
    assert "application/x-smm-bg3" not in card_src
    start = card_src.split("def _start_wh3_sort_drag", 1)[1]
    assert "mod_pk" not in start.split("def dragEnterEvent", 1)[0]
