"""Phase 6.1: Deploy must keep Library selection + Detail on Frozen UUID."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from services.deploy_identity import is_frozen_internal_uuid
from services.deploy_result import DeployResult, DeployStatus
from services.mod_library_cache import reset_library_cache
from services.mod_projection_events import reset_mod_changed_listeners
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    manager = DatabaseManager.instance(tmp_path / "deploy_sel.db")
    manager.upsert_game(GameInfo(app_id=99, name="TestGame", folder_name="TestGame"))
    yield manager
    reset_mod_changed_listeners()
    reset_library_cache()
    manager.close()
    DatabaseManager.reset_instance()


def _pump() -> None:
    for _ in range(8):
        QCoreApplication.processEvents()


def _seed_mod(
    db: DatabaseManager, library: Path
) -> tuple[Path, str, str]:
    mod_dir = library / "TestGame" / "DeployMe"
    mod_dir.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id="8001", title="DeployMe", app_id=99, game_name="TestGame"
    )
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title="DeployMe",
        app_id=99,
        game_name="TestGame",
    )
    uuid = str(created.internal_id)
    assert is_frozen_internal_uuid(uuid)
    assert str(pk).isdigit()
    return mod_dir, uuid, str(pk)


def test_deploy_keeps_detail_and_library_selection(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    library = tmp_path / "mod"
    mod_dir, uuid, pk = _seed_mod(db, library)
    db.update_game_deploy_config(99, name="TestGame", mod_path=str(tmp_path / "GameMods"))

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.resize(900, 480)
    view.show()
    view.refresh()
    _pump()

    card = view._card_for_internal_id(uuid)
    assert card is not None
    view._select_card(card, show_panel=True)
    _pump()
    assert view.detail_panel.current_internal_id() == uuid
    assert view._selected_mod_id == uuid

    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "GameMods" / "DeployMe"),
        deploy_time="2026-01-01T00:00:00+00:00",
    )

    view._deploy_mod_id = uuid
    with caplog.at_level("INFO"):
        view._on_deploy_finished(
            {
                "success": True,
                "internal_id": uuid,
                "mod_pk": int(pk),
                "target": str(tmp_path / "GameMods" / "DeployMe"),
                "copied_files": 1,
            }
        )
        _pump()

    assert view.detail_panel.current_internal_id() == uuid
    assert view._selected_mod_id == uuid
    selected = view._selected_card
    assert selected is not None
    assert selected._mod_id() == uuid
    assert view.detail_panel._mode != 0  # not MODE_EMPTY
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert f"internal_id={uuid}" in joined
    assert f"mod_pk={pk}" in joined
    assert f"internal_id={pk}" not in joined


def test_offscreen_deploy_signal_uses_uuid_not_pk(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "mod"
    mod_dir, uuid, pk = _seed_mod(db, library)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id=uuid)
    _pump()
    assert panel.current_internal_id() == uuid
    assert str(panel.current_mod_pk()) == pk

    seen: list[str] = []
    panel.deploy_requested.connect(seen.append)
    panel._request_deploy()
    assert seen == [uuid]
    assert is_frozen_internal_uuid(seen[0])
    assert not seen[0].isdigit()

    payload = DeployResult(
        status=DeployStatus.SUCCESS,
        internal_id=uuid,
        mod_pk=int(pk),
        target=str(tmp_path / "GameMods" / "DeployMe"),
        copied_files=1,
    ).to_dict()
    assert payload["internal_id"] == uuid
    assert int(payload["mod_pk"]) == int(pk)
    assert not str(payload["internal_id"]).isdigit()

    panel.set_deploy_busy(True)
    panel.apply_deploy_result(payload)
    assert panel.current_internal_id() == uuid
    assert panel._mode != 0
