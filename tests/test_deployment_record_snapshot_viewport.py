"""Deployment Record snapshot must not use viewport ``_card_entries``."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services import deployment_record as dr
from ui.library_query import ModFilterIndex


CIV6 = 289070
GAME_FOLDER = "Civilization VI"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deployment_record_viewport.db")
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


def _index(
    mod_id: str,
    *,
    deployed: bool = True,
    workspace_id: str = "",
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=str(mod_id),
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name=GAME_FOLDER,
        favorite=False,
        deployed=deployed,
        has_offline=True,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
        workspace_id=str(workspace_id or ""),
    )


def _seed_deployed(db: DatabaseManager, mod_id: int, *, workspace_id: str | None = None) -> None:
    ws = workspace_id if workspace_id is not None else str(mod_id)
    db.upsert_mod(
        ModMetadata(
            published_file_id=str(mod_id),
            title=f"Mod {mod_id}",
            app_id=CIV6,
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        internal_id=str(mod_id),
        workspace_id=ws,
        folder_present=True,
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=f"/fake/{mod_id}",
    )


def test_case1_viewport_window_does_not_truncate_snapshot(
    qapp, db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """24 deployed entities; viewport binds only first 5 — snapshot must keep all 24."""
    from PySide6.QtWidgets import QMessageBox

    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    db.upsert_game(GameInfo(app_id=CIV6, name=GAME_FOLDER, folder_name=GAME_FOLDER))
    pks = list(range(101, 125))  # 24 mods
    assert len(pks) == 24
    for mid in pks:
        _seed_deployed(db, mid)

    # Pre-existing record with a subset — update must expand to full deployed set.
    record = dr.create_or_update_record(
        CIV6, "和而不同·优化", mod_ids=pks[:20], db=db
    )
    assert len(dr.get_record_mod_ids(record.id, db=db)) == 20

    view = ModLibraryView()
    view.current_game_id = CIV6
    view._current_game_filter = GAME_FOLDER
    view._target_root = tmp_path / "mod"

    view._game_row_entries = [
        (_index(str(mid), deployed=True), object()) for mid in pks
    ]
    # Viewport binds only the top 5 cards (the bug surface).
    cards = [ModCardWidget(tmp_path / f"c{i}", None) for i in range(5)]
    view._card_entries = [
        (_index(str(pks[i]), deployed=True), cards[i]) for i in range(5)
    ]
    # Filtered rows must also be ignored if someone regresses to them.
    view._filtered_row_entries = view._game_row_entries[:10]

    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    with caplog.at_level("INFO", logger="ui.library_view"):
        view._on_update_deployment_record(record.name)

    recorded = dr.get_record_mod_ids(record.id, db=db)
    assert recorded == {str(m) for m in pks}
    assert len(recorded) == 24

    snap_logs = [
        r.message
        for r in caplog.records
        if "[DEPLOYMENT_RECORD_SNAPSHOT]" in r.message
    ]
    assert snap_logs
    assert "source=game_row_entries" in snap_logs[-1]
    assert "total_candidates=24" in snap_logs[-1]
    assert "deployed_count=24" in snap_logs[-1]
    assert "viewport_count=5" in snap_logs[-1]


def test_case2_writes_mod_id_pk_not_workspace_id(
    qapp, db: DatabaseManager, tmp_path: Path
) -> None:
    """workspace_id != mod_id PK — items store PK only."""
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=CIV6, name=GAME_FOLDER, folder_name=GAME_FOLDER))
    pk = 466
    workspace = "3793097715"
    assert str(pk) != workspace
    _seed_deployed(db, pk, workspace_id=workspace)

    view = ModLibraryView()
    view.current_game_id = CIV6
    view._current_game_filter = GAME_FOLDER
    view._target_root = tmp_path / "mod"
    view._game_row_entries = [
        (_index(str(pk), deployed=True, workspace_id=workspace), object())
    ]
    view._card_entries = []  # empty viewport must not matter

    ids = view._snapshot_mod_ids_for_deployment_record()
    assert ids == [str(pk)]
    assert workspace not in (ids or [])

    record = dr.create_or_update_record(
        CIV6, "pk-only", mod_ids=ids, db=db
    )
    stored = dr.get_record_mod_ids(record.id, db=db)
    assert stored == {str(pk)}
    assert workspace not in stored

    # Raw table must hold integer PK.
    with db._lock:
        rows = db._conn.execute(
            "SELECT mod_id FROM deployment_record_items WHERE record_id = ?",
            (int(record.id),),
        ).fetchall()
    assert [int(r["mod_id"]) for r in rows] == [pk]


def test_case3_empty_card_entries_still_snapshots_game_rows(
    qapp, db: DatabaseManager, tmp_path: Path, caplog
) -> None:
    """``_card_entries=[]`` with populated ``_game_row_entries`` → full snapshot."""
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=CIV6, name=GAME_FOLDER, folder_name=GAME_FOLDER))
    pks = [764, 616, 604, 601]
    for mid in pks:
        _seed_deployed(db, mid)

    view = ModLibraryView()
    view.current_game_id = CIV6
    view._game_row_entries = [
        (_index(str(mid), deployed=True), object()) for mid in pks
    ]
    view._card_entries = []
    view._filtered_row_entries = []

    with caplog.at_level("INFO", logger="ui.library_view"):
        ids = view._snapshot_mod_ids_for_deployment_record()

    assert ids == [str(m) for m in pks]
    assert any(
        "[DEPLOYMENT_RECORD_SNAPSHOT]" in r.message
        and "source=game_row_entries" in r.message
        and "viewport_count=0" in r.message
        for r in caplog.records
    )

    # Empty game rows → DB fallback (None).
    view._game_row_entries = []
    with caplog.at_level("INFO", logger="ui.library_view"):
        assert view._snapshot_mod_ids_for_deployment_record() is None
    assert any(
        "source=database_fallback" in r.message for r in caplog.records
    )


def test_clear_record_overlay_still_exists_on_card(qapp) -> None:
    """Card-pool cleanup API must remain; overlays restored via LibraryView sync."""
    from ui.mod_card import ModCardWidget

    assert hasattr(ModCardWidget, "clear_record_overlay")
    assert not hasattr(ModCardWidget, "reset_record_overlay")
