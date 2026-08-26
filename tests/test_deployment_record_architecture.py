"""Architecture guards: Deployment Record is a Filter peer, not a mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services import deployment_record as dr
from ui.library_query import (
    FILTER_ALL,
    FILTER_DEPLOYED,
    FILTER_DEPLOYMENT_RECORD,
    ModFilterIndex,
    compute_record_relative_status,
    filter_and_sort,
)


STARDEW = 413150


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deployment_record_arch.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _game(db: DatabaseManager) -> None:
    db.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew Valley")
    )


def _mod(db: DatabaseManager, mod_id: int, *, deployed: bool) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=str(mod_id),
            title=f"Mod {mod_id}",
            app_id=STARDEW,
        )
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=(
            DEPLOY_STATUS_DEPLOYED if deployed else DEPLOY_STATUS_NOT_DEPLOYED
        ),
        deploy_path="" if not deployed else f"/fake/{mod_id}",
    )


def _index(mod_id: str, *, deployed: bool) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="Stardew",
        favorite=False,
        deployed=deployed,
        has_offline=True,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
    )


def test_save_record_does_not_modify_deploy_status(db: DatabaseManager) -> None:
    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    before = {
        mid: db.get_mod_deploy_info(mid).deploy_status for mid in (1, 2, 3)
    }
    dr.create_or_update_record(STARDEW, "存档1", db=db)
    after = {
        mid: db.get_mod_deploy_info(mid).deploy_status for mid in (1, 2, 3)
    }
    assert before == after


def test_delete_record_does_not_delete_mods(db: DatabaseManager) -> None:
    _game(db)
    _mod(db, 10, deployed=True)
    record = dr.create_or_update_record(STARDEW, "del", db=db)
    assert dr.delete_record(record.id, db=db) is True
    assert db.get_mod(10) is not None
    assert db.get_mod_deploy_info(10).deploy_status == DEPLOY_STATUS_DEPLOYED


def test_relative_status_not_written_to_database(db: DatabaseManager) -> None:
    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    record = dr.create_or_update_record(STARDEW, "存档1", db=db)
    _mod(db, 4, deployed=True)
    recorded = frozenset(dr.get_record_mod_ids(record.id, db=db))
    status = compute_record_relative_status(_index("4", deployed=True), recorded)
    assert status is not None and status.not_recorded_deployed

    forbidden = ("extra_deployed", "record_missing", "relative_status")
    with db._lock:
        cols_mods = {str(r[1]) for r in db._conn.execute("PRAGMA table_info(mods)")}
        cols_items = {
            str(r[1])
            for r in db._conn.execute("PRAGMA table_info(deployment_record_items)")
        }
        cols_recs = {
            str(r[1])
            for r in db._conn.execute("PRAGMA table_info(deployment_records)")
        }
    for name in forbidden:
        assert name not in cols_mods
        assert name not in cols_items
        assert name not in cols_recs


def test_all_filter_ignores_record_mod_ids() -> None:
    recorded = frozenset({"1"})
    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=False), "B"),
    ]
    assert filter_and_sort(entries, filter_key=FILTER_ALL, record_mod_ids=recorded) == [
        "A",
        "B",
    ]
    assert filter_and_sort(
        entries, filter_key=FILTER_DEPLOYED, record_mod_ids=recorded
    ) == ["A"]
    assert filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    ) == ["A"]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_no_enter_leave_apis(qapp) -> None:
    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    assert not hasattr(view, "enter_deployment_record")
    assert not hasattr(view, "leave_deployment_record")
    assert not hasattr(view, "_record_context")
    assert not hasattr(view, "_active_record_name")
    assert view._status_filter == FILTER_ALL
    assert view._deployment_record_id is None
