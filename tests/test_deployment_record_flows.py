"""Deployment Record — user-behavior flows (Filter peer, no enter/leave)."""

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
    FILTER_FAVORITE,
    RECORD_STATUS_LABEL_EXTRA,
    RECORD_STATUS_LABEL_MISSING,
    ModFilterIndex,
    compute_record_relative_status,
    filter_and_sort,
    record_relative_badge_label,
)


STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deployment_record_flows.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


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


def _index(mod_id: str, *, deployed: bool, favorite: bool = False) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="Stardew",
        favorite=favorite,
        deployed=deployed,
        has_offline=True,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
    )


def test_case1_save_and_select_record_filter(db: DatabaseManager) -> None:
    """Deploy ABC → save → select FILTER_DEPLOYMENT_RECORD → ABC."""
    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2, 3):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "记录1", db=db)
    recorded = frozenset(dr.get_record_mod_ids(record.id, db=db))
    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=True), "B"),
        (_index("3", deployed=True), "C"),
        (_index("9", deployed=False), "hidden"),
    ]
    assert filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    ) == ["A", "B", "C"]


def test_case2_extra_and_missing_under_record_filter(db: DatabaseManager) -> None:
    """Record ABC; live ABD → C missing, D extra."""
    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2, 3):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "记录1", db=db)
    recorded = frozenset(dr.get_record_mod_ids(record.id, db=db))

    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=True), "B"),
        (_index("3", deployed=False), "C"),
        (_index("4", deployed=True), "D"),
    ]
    assert filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    ) == ["A", "B", "C", "D"]
    assert record_relative_badge_label(
        compute_record_relative_status(entries[2][0], recorded)
    ) == RECORD_STATUS_LABEL_MISSING
    assert record_relative_badge_label(
        compute_record_relative_status(entries[3][0], recorded)
    ) == RECORD_STATUS_LABEL_EXTRA


def test_case3_all_filter_clears_overlay() -> None:
    """Switching to FILTER_ALL → no relative badges."""
    recorded = frozenset({"1", "2", "3"})
    index = _index("4", deployed=True)
    assert record_relative_badge_label(
        compute_record_relative_status(index, recorded)
    ) == RECORD_STATUS_LABEL_EXTRA
    assert compute_record_relative_status(index, None) is None
    # Ordinary ALL filter ignores record_mod_ids entirely.
    entries = [(index, "D"), (_index("9", deployed=False), "X")]
    assert filter_and_sort(
        entries, filter_key=FILTER_ALL, record_mod_ids=recorded
    ) == ["D", "X"]


def test_case4_update_record_items(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2, 3):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "记录1", db=db)
    db.update_mod_deploy_status(
        3, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED, deploy_path=""
    )
    _mod(db, 4, app_id=STARDEW, deployed=True)
    updated = dr.create_or_update_record(STARDEW, "记录1", db=db)
    assert int(updated.id) == int(record.id)
    assert dr.get_record_mod_ids(updated.id, db=db) == {"1", "2", "4"}
    recorded = frozenset(dr.get_record_mod_ids(updated.id, db=db))
    entries = [
        (_index("1", deployed=True), "A"),
        (_index("2", deployed=True), "B"),
        (_index("3", deployed=False), "C"),
        (_index("4", deployed=True), "D"),
    ]
    assert filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    ) == ["A", "B", "D"]


def test_case5_rename(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    _mod(db, 1, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "记录1", db=db)
    renamed = dr.rename_record(record.id, "新记录", db=db)
    assert renamed.name == "新记录"
    assert dr.find_record_by_name(STARDEW, "记录1", db=db) is None
    assert dr.find_record_by_name(STARDEW, "新记录", db=db) is not None


def test_case6_delete_does_not_touch_mods(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    _mod(db, 10, app_id=STARDEW, deployed=True)
    record = dr.create_or_update_record(STARDEW, "del-me", db=db)
    assert dr.delete_record(record.id, db=db) is True
    assert db.get_mod(10) is not None
    assert db.get_mod_deploy_info(10).deploy_status == DEPLOY_STATUS_DEPLOYED


def test_record_and_status_chips_are_mutex() -> None:
    """Favorite chip must not AND with a record set."""
    recorded = frozenset({"1"})
    entries = [
        (_index("1", deployed=True, favorite=False), "A"),
        (_index("2", deployed=True, favorite=True), "B"),
    ]
    # Record filter shows recorded∪deployed (both), not favorite-only.
    assert filter_and_sort(
        entries,
        filter_key=FILTER_DEPLOYMENT_RECORD,
        record_mod_ids=recorded,
    ) == ["A", "B"]
    # Favorite alone ignores record_mod_ids.
    assert filter_and_sort(
        entries,
        filter_key=FILTER_FAVORITE,
        record_mod_ids=recorded,
    ) == ["B"]


def test_cross_game_same_name(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    _game(db, BG3, "Baldur's Gate 3")
    _mod(db, 20, app_id=STARDEW, deployed=True)
    _mod(db, 21, app_id=BG3, deployed=True)
    a = dr.create_or_update_record(STARDEW, "同名", db=db)
    b = dr.create_or_update_record(BG3, "同名", db=db)
    assert a.id != b.id
    assert a.name == b.name


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_ui_filter_mutex_and_clear_overlays(qapp, tmp_path: Path) -> None:
    """Selecting a record filter then ALL clears badges; no enter/leave API."""
    from PySide6.QtWidgets import QToolButton

    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    view = ModLibraryView()
    assert not hasattr(view, "enter_deployment_record")
    assert not hasattr(view, "leave_deployment_record")
    assert isinstance(view.btn_deployment_record, QToolButton)
    assert view.btn_deployment_record.popupMode() == (
        QToolButton.ToolButtonPopupMode.InstantPopup
    )

    card = ModCardWidget(tmp_path / "m1", None)
    view._card_entries = [(_index("2", deployed=False), card)]
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD, record_id=99, record_name="宝可梦一周目"
    )
    view._cached_record_mod_ids = frozenset({"2"})
    view._last_filter_sig = None
    view._sync_record_overlays()
    assert view._status_filter == FILTER_DEPLOYMENT_RECORD
    assert view.btn_deployment_record.text() == "💾 宝可梦一周目 ▼"
    assert not card.record_badge.isHidden()
    assert card.record_badge.text() == RECORD_STATUS_LABEL_MISSING
    # Status chips visually unchecked while record filter active.
    from ui.library_query import STATUS_FILTER_LABELS

    assert not any(
        view._filter_buttons[k].isChecked()
        for k, _ in STATUS_FILTER_LABELS
        if k in view._filter_buttons
    )

    view._set_library_status_filter(FILTER_ALL)
    assert view._status_filter == FILTER_ALL
    assert view._deployment_record_id is None
    assert view.btn_deployment_record.text() == "💾 部署记录 ▼"
    assert card.record_badge.isHidden()
    assert getattr(card, "_record_relative", None) is None
    assert view._filter_buttons[FILTER_ALL].isChecked()

    # Favorite clears record filter.
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD, record_id=1, record_name="x"
    )
    view._set_library_status_filter(FILTER_FAVORITE)
    assert view._deployment_record_id is None
    assert view._status_filter == FILTER_FAVORITE
    # Record → 收藏 → 记录 again
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD, record_id=1, record_name="存档1"
    )
    assert view._status_filter == FILTER_DEPLOYMENT_RECORD
    assert not view._filter_buttons[FILTER_FAVORITE].isChecked()


def test_popup_exposes_manage_actions(qapp) -> None:
    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    view.current_game_id = STARDEW
    view._rebuild_deployment_record_menu()
    actions = list(view._deployment_record_menu.actions())
    texts = [a.text() for a in actions]
    assert "筛选记录" in texts
    assert "全部" in texts
    assert "记录管理" in texts
    assert "保存当前环境..." in texts
    assert "更新记录..." in texts
    assert "重命名记录..." in texts
    assert "删除记录..." in texts
    assert "退出记录查看" not in texts
    # First-level actions — not nested submenus.
    for label in ("更新记录...", "重命名记录...", "删除记录..."):
        act = next(a for a in actions if a.text() == label)
        assert act.menu() is None
        assert act.isEnabled()

    view._set_library_status_filter(FILTER_FAVORITE)
    view._rebuild_deployment_record_menu()
    act_all = next(
        a for a in view._deployment_record_menu.actions() if a.text() == "全部"
    )
    assert not act_all.isChecked()


def test_save_overwrite_requires_confirm(
    qapp, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from ui.library_view import ModLibraryView

    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2, 3):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    first = dr.create_or_update_record(STARDEW, "存档1", db=db)
    db.update_mod_deploy_status(
        3, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED, deploy_path=""
    )
    _mod(db, 4, app_id=STARDEW, deployed=True)

    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)
    view = ModLibraryView()
    view.current_game_id = STARDEW
    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("存档1", True))
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    view._confirm_overwrite_deployment_record = lambda label: False  # type: ignore[method-assign]
    view._on_save_deployment_record()
    assert dr.get_record_mod_ids(first.id, db=db) == {"1", "2", "3"}

    view._confirm_overwrite_deployment_record = lambda label: True  # type: ignore[method-assign]
    view._on_save_deployment_record()
    assert dr.get_record_mod_ids(first.id, db=db) == {"1", "2", "4"}
    assert len(dr.list_records(STARDEW, db=db)) == 1


def test_update_rename_delete_user_paths(
    qapp, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from ui.library_view import ModLibraryView

    _game(db, STARDEW, "Stardew Valley")
    for mid in (1, 2, 3):
        _mod(db, mid, app_id=STARDEW, deployed=True)
    rec = dr.create_or_update_record(STARDEW, "存档1", db=db)
    db.update_mod_deploy_status(
        3, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED, deploy_path=""
    )
    _mod(db, 4, app_id=STARDEW, deployed=True)

    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)
    view = ModLibraryView()
    view.current_game_id = STARDEW
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        QInputDialog, "getItem", staticmethod(lambda *a, **k: ("存档1", True))
    )
    view._on_update_deployment_record_clicked()
    assert dr.get_record_mod_ids(rec.id, db=db) == {"1", "2", "4"}

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("测试环境", True))
    )
    view._on_rename_deployment_record_clicked()
    assert dr.find_record_by_name(STARDEW, "存档1", db=db) is None
    renamed = dr.find_record_by_name(STARDEW, "测试环境", db=db)
    assert renamed is not None
    assert int(renamed.id) == int(rec.id)

    monkeypatch.setattr(
        QInputDialog, "getItem", staticmethod(lambda *a, **k: ("测试环境", True))
    )
    view._on_delete_deployment_record_clicked()
    assert dr.list_records(STARDEW, db=db) == []
    assert db.get_mod(1) is not None
    assert db.get_mod_deploy_info(1).deploy_status == DEPLOY_STATUS_DEPLOYED
