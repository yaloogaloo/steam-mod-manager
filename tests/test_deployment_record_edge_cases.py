"""Deployment Record edge cases — delete/switch/empty/ghost/normal-filter clear."""

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
    FILTER_ANOMALY,
    FILTER_CONTENT_MISSING,
    FILTER_DEPLOYED,
    FILTER_DEPLOYMENT_RECORD,
    FILTER_FAVORITE,
    RECORD_STATUS_LABEL_EXTRA,
    ModFilterIndex,
    compute_record_relative_status,
    filter_and_sort,
    matches_record_visibility,
    record_relative_badge_label,
)


STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deployment_record_edge.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _game(db: DatabaseManager, app_id: int, name: str) -> None:
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))


def _mod(
    db: DatabaseManager,
    mod_id: int,
    *,
    app_id: int,
    deployed: bool = False,
) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=str(mod_id),
            title=f"Mod {mod_id}",
            app_id=app_id,
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


def test_case1_delete_active_record_returns_to_all(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the record currently filtered clears filter + overlays."""
    from PySide6.QtWidgets import QMessageBox

    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "当前", db=db)

    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    view = ModLibraryView()
    card = ModCardWidget(tmp_path / "extra", None)
    view.current_game_id = STARDEW
    view._card_entries = [(_index("9", deployed=True), card)]
    view._card_cache = {"9": card}
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name=record.name,
    )
    view._cached_record_mod_ids = frozenset({"1", "2"})
    view._sync_record_overlays()
    assert view._status_filter == FILTER_DEPLOYMENT_RECORD
    assert card.record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    view._on_delete_deployment_record(int(record.id), record.name)
    assert view._status_filter == FILTER_ALL
    assert view._deployment_record_id is None
    assert getattr(card, "_record_relative", None) is None
    assert card.record_badge.isHidden()
    assert dr.find_record_by_name(STARDEW, "当前", db=db) is None


def test_case2_switch_game_clears_record_filter(qapp, db: DatabaseManager) -> None:
    from ui.library_view import ModLibraryView

    _game(db, STARDEW, "Stardew Valley")
    _game(db, BG3, "Baldur's Gate 3")
    _mod(db, 1, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "A档", db=db)

    view = ModLibraryView()
    view.current_game_id = STARDEW
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name="A档",
    )
    assert view._status_filter == FILTER_DEPLOYMENT_RECORD
    assert view._deployment_record_id == int(record.id)

    view._set_current_game_context("Baldur's Gate 3", game_id=BG3)
    assert view._status_filter == FILTER_ALL
    assert view._deployment_record_id is None


def test_case3_empty_record_items_no_crash() -> None:
    recorded = frozenset()
    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=False), "hidden"),
    ]
    visible = filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    )
    assert visible == ["A"]
    status = compute_record_relative_status(entries[0][0], recorded)
    assert status is not None
    assert status.not_recorded_deployed
    assert record_relative_badge_label(status) == RECORD_STATUS_LABEL_EXTRA


def test_case4_ghost_record_mod_ignored_in_visibility() -> None:
    """Record contains X with no library card — only real cards matter."""
    recorded = frozenset({"1", "999999"})  # 999999 is ghost
    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=True), "B"),
    ]
    visible = filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    )
    assert visible == ["A", "B"]
    assert matches_record_visibility(entries[0][0], recorded)
    assert matches_record_visibility(entries[1][0], recorded)
    assert "999999" not in [str(p) for p in visible]
    assert record_relative_badge_label(
        compute_record_relative_status(entries[0][0], recorded)
    ) is None
    assert record_relative_badge_label(
        compute_record_relative_status(entries[1][0], recorded)
    ) == RECORD_STATUS_LABEL_EXTRA


def test_case5_ordinary_filters_clear_record_badge(qapp, tmp_path: Path) -> None:
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    view = ModLibraryView()
    card = ModCardWidget(tmp_path / "m", None)
    view._card_entries = [(_index("4", deployed=True), card)]
    view._card_cache = {"4": card}

    for key in (
        FILTER_ALL,
        FILTER_FAVORITE,
        FILTER_CONTENT_MISSING,
        FILTER_ANOMALY,
        FILTER_DEPLOYED,
    ):
        view._set_library_status_filter(
            FILTER_DEPLOYMENT_RECORD, record_id=1, record_name="x"
        )
        view._cached_record_mod_ids = frozenset({"1"})
        view._sync_record_overlays()
        assert not card.record_badge.isHidden()
        view._set_library_status_filter(key)
        assert view._deployment_record_id is None
        assert view._status_filter == key
        assert getattr(card, "_record_relative", None) is None
        assert card.record_badge.isHidden()


def test_no_active_record_apis_in_production(qapp) -> None:
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    view = ModLibraryView()
    assert not hasattr(view, "enter_deployment_record")
    assert not hasattr(view, "leave_deployment_record")
    assert not hasattr(view, "_record_context")
    assert not hasattr(view, "_active_record_name")
    assert not hasattr(ModCardWidget, "reset_record_overlay")
    assert hasattr(ModCardWidget, "clear_record_overlay")
