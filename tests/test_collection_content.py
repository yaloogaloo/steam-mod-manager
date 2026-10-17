"""Collection Phase 2 — Content mode, sort reuse, membership UI."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from core.db_manager import DatabaseManager, get_db
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services import collection as coll
from services.identity_service import create_mod_identity, identity_create_scope
from services.mod_library_cache import ModCardData
from ui.collection_membership_dialog import CollectionMembershipDialog
from ui.library_query import (
    FILTER_ALL,
    FILTER_DEPLOYED,
    SORT_MTIME,
    SORT_NAME,
    SORT_SIZE,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
)
from ui.library_view import (
    COLLECTION_MODE_CONTENT,
    COLLECTION_MODE_LIST,
    COLLECTION_MODE_NORMAL,
    EMPTY_COLLECTION,
    ModLibraryView,
)
from ui.mod_card import ModCardWidget

STARDEW = 413150
PALWORLD = 1623730
WH3 = 1142710


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _seed_game(app_id: int, name: str) -> DatabaseManager:
    db = get_db()
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))
    return db


def _mod(db: DatabaseManager, app_id: int, workshop_id: str, title: str) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=app_id,
            game_name=title,
            operation="import",
        )
    return str(created.mod_id)


def _index(
    mid: str,
    name: str,
    mtime: float,
    *,
    local_size_bytes: int | None = None,
    local_size_status: str = "unknown",
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=str(mid),
        display_name=name,
        steam_name=name,
        notes="",
        game_name="Stardew Valley",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=mtime,
        sort_name=name.lower(),
        local_size_bytes=local_size_bytes,
        local_size_status=local_size_status,
    )


def _payload(mid: str, name: str, mtime: float, folder: Path) -> ModCardData:
    return ModCardData(
        id=str(mid),
        title=name,
        platform=PLATFORM_STEAM,
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=mtime,
        managed_path=str(folder),
        game_folder="Stardew Valley",
        steam_name=name,
        game_name="Stardew Valley",
    )


def _install_rows(view: ModLibraryView, rows: list[tuple[str, str, float]], tmp_path: Path) -> None:
    entries = []
    for mid, name, mtime in rows:
        folder = tmp_path / "Stardew Valley" / name
        folder.mkdir(parents=True, exist_ok=True)
        entries.append((_index(mid, name, mtime), _payload(mid, name, mtime, folder)))
    view._game_row_entries = entries
    view._filtered_row_entries = list(entries)


def _enter_list(view: ModLibraryView, app_id: int, name: str) -> None:
    view._set_current_game_context(name, game_id=app_id)
    view.btn_collection_mode.setEnabled(True)
    view.btn_collection_mode.setChecked(True)


def test_open_collection_enters_content_with_member_mods(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88201", "Alpha")
    m2 = _mod(db, STARDEW, "88202", "Beta")
    outsider = _mod(db, STARDEW, "88203", "Gamma")
    rec = coll.create_collection(STARDEW, "Pack", db=db)
    coll.add_mods_to_collection(rec.collection_id, [m1, m2], db=db)
    view = ModLibraryView()
    _install_rows(
        view,
        [(m1, "Alpha", 30.0), (m2, "Beta", 10.0), (outsider, "Gamma", 20.0)],
        tmp_path,
    )
    _enter_list(view, STARDEW, "Stardew Valley")
    view._open_collection_content(rec.collection_id)
    assert view._collection_mode == COLLECTION_MODE_CONTENT
    assert view._is_collection_content_mode() is True
    assert view._current_collection_id == rec.collection_id
    ids = [str(index.mod_id) for index, _p in view._filtered_row_entries]
    assert set(ids) == {m1, m2}
    assert outsider not in ids
    assert not view.btn_collection_back.isHidden()
    assert "Pack" in view._page_title.text()
    assert all(isinstance(c, ModCardWidget) for c in view._cards)
    assert view.btn_collection_mode.isChecked() is True
    for key, _label in STATUS_FILTER_LABELS:
        assert view._filter_buttons[key].isChecked() is False
    view.deleteLater()


def test_empty_collection_content_keeps_back_and_custom_empty(
    qapp: QApplication,
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Empty", db=db)
    view = ModLibraryView()
    view._game_row_entries = []
    _enter_list(view, STARDEW, "Stardew Valley")
    view._open_collection_content(rec.collection_id)
    assert view._collection_mode == COLLECTION_MODE_CONTENT
    assert view._empty_kind == EMPTY_COLLECTION
    assert "暂无 Mod" in view.empty_title.text()
    assert view.empty_action_btn.isHidden()
    assert not view.btn_collection_back.isHidden()
    view.deleteLater()


def test_return_to_collection_list(qapp: QApplication, tmp_path: Path) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Back", db=db)
    view = ModLibraryView()
    _install_rows(view, [], tmp_path)
    _enter_list(view, STARDEW, "Stardew Valley")
    view._selected_mod_ids = ["99"]
    view._open_collection_content(rec.collection_id)
    view._selected_mod_ids = ["99"]
    view._return_to_collection_list()
    assert view._collection_mode == COLLECTION_MODE_LIST
    assert view._current_collection_id is None
    assert view._selected_mod_ids == []
    assert view.btn_collection_back.isHidden()
    assert view.btn_collection_mode.isChecked() is True
    assert view._status_filter == FILTER_ALL
    assert view._wh3_sort_mode is False
    view.deleteLater()


def test_content_uses_filter_sort_entries_name_and_mtime(
    qapp: QApplication, tmp_path: Path
) -> None:
    src = inspect.getsource(ModLibraryView._prepare_collection_content_entries)
    assert "filter_sort_entries(" in src
    assert "collection_sort_entries(" not in src
    assert "_apply_wh3_sort_order(" not in src
    db = _seed_game(STARDEW, "Stardew Valley")
    zebra = _mod(db, STARDEW, "88301", "Zebra")
    apple = _mod(db, STARDEW, "88302", "Apple")
    rec = coll.create_collection(STARDEW, "Sorted", db=db)
    coll.add_mods_to_collection(rec.collection_id, [zebra, apple], db=db)
    view = ModLibraryView()
    _install_rows(
        view, [(zebra, "Zebra", 1.0), (apple, "Apple", 9.0)], tmp_path
    )
    _enter_list(view, STARDEW, "Stardew Valley")
    view._sort_mode = SORT_NAME
    view._open_collection_content(rec.collection_id)
    names = [index.display_name for index, _p in view._filtered_row_entries]
    assert names == ["Apple", "Zebra"]
    view._sort_mode = SORT_MTIME
    view._last_filter_sig = None
    view._apply_view_filter()
    names = [index.display_name for index, _p in view._filtered_row_entries]
    assert names == ["Apple", "Zebra"] or names[0] == "Apple"
    # mtime desc via sort_key: higher mtime first for SORT_MTIME?
    from ui.library_query import sort_key

    ordered = sorted(
        view._game_row_entries,
        key=lambda pair: sort_key(pair[0], SORT_MTIME),
    )
    expected = [p[0].display_name for p in ordered if p[0].mod_id in {apple, zebra}]
    # Content result must match filter_sort_entries on membership subset.
    from ui.library_query import filter_sort_entries

    subset = [row for row in view._game_row_entries if row[0].mod_id in {apple, zebra}]
    via_query = [
        i.display_name
        for i, _p in filter_sort_entries(subset, sort_mode=SORT_MTIME)
    ]
    via_view = [i.display_name for i, _p in view._filtered_row_entries]
    assert via_view == via_query
    view.deleteLater()


def test_content_uses_filter_sort_entries_size_sort(
    qapp: QApplication, tmp_path: Path
) -> None:
    src = inspect.getsource(ModLibraryView._prepare_collection_content_entries)
    assert "filter_sort_entries(" in src
    assert "collection_sort_entries(" not in src
    db = _seed_game(STARDEW, "Stardew Valley")
    tiny = _mod(db, STARDEW, "88601", "Tiny")
    huge = _mod(db, STARDEW, "88602", "Huge")
    gone = _mod(db, STARDEW, "88603", "Gone")
    rec = coll.create_collection(STARDEW, "Sized", db=db)
    coll.add_mods_to_collection(rec.collection_id, [tiny, huge, gone], db=db)
    view = ModLibraryView()
    folder = tmp_path / "Stardew Valley"
    entries = [
        (
            _index(tiny, "Tiny", 1.0, local_size_bytes=0, local_size_status="ok"),
            _payload(tiny, "Tiny", 1.0, folder / "Tiny"),
        ),
        (
            _index(
                huge, "Huge", 2.0, local_size_bytes=2_000_000_000, local_size_status="ok"
            ),
            _payload(huge, "Huge", 2.0, folder / "Huge"),
        ),
        (
            _index(gone, "Gone", 3.0, local_size_bytes=None, local_size_status="missing"),
            _payload(gone, "Gone", 3.0, folder / "Gone"),
        ),
    ]
    for _i, payload in entries:
        Path(payload.managed_path).mkdir(parents=True, exist_ok=True)
    view._game_row_entries = entries
    view._filtered_row_entries = list(entries)
    _enter_list(view, STARDEW, "Stardew Valley")
    view._sort_mode = SORT_SIZE
    view._open_collection_content(rec.collection_id)
    names = [index.display_name for index, _p in view._filtered_row_entries]
    from ui.library_query import filter_sort_entries

    subset = [row for row in view._game_row_entries if row[0].mod_id in {tiny, huge, gone}]
    via_query = [i.display_name for i, _p in filter_sort_entries(subset, sort_mode=SORT_SIZE)]
    assert names == ["Tiny", "Huge", "Gone"]
    assert names == via_query
    view.deleteLater()


def test_content_click_filter_exits_to_that_filter(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Leave", db=db)
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    view._open_collection_content(rec.collection_id)
    view._filter_buttons[FILTER_DEPLOYED].click()
    assert view._collection_mode == COLLECTION_MODE_NORMAL
    assert view._current_collection_id is None
    assert view._status_filter == FILTER_DEPLOYED
    assert view._filter_buttons[FILTER_DEPLOYED].isChecked() is True
    view.deleteLater()


def test_content_wh3_sorting_mutex_and_game_switch(
    qapp: QApplication,
) -> None:
    db = get_db()
    db.upsert_game(
        GameInfo(
            app_id=WH3,
            name="Total War: WARHAMMER III",
            folder_name="Warhammer3",
        )
    )
    _seed_game(PALWORLD, "Palworld")
    rec = coll.create_collection(WH3, "WH3Pack", db=db)
    view = ModLibraryView()
    view._set_current_game_context("Warhammer3", game_id=WH3)
    view.btn_collection_mode.setEnabled(True)
    view.btn_collection_mode.setChecked(True)
    view._open_collection_content(rec.collection_id)
    assert view._is_collection_content_mode() is True
    view.btn_wh3_sort_mode.setVisible(True)
    view.btn_wh3_sort_mode.setEnabled(True)
    view.btn_wh3_sort_mode.setChecked(True)
    assert view._wh3_sort_mode is True
    assert view._is_collection_workspace() is False
    assert view._current_collection_id is None

    view.btn_collection_mode.setEnabled(True)
    view.btn_collection_mode.setChecked(True)
    view._open_collection_content(rec.collection_id)
    view._selected_mod_ids = ["1"]
    view._set_current_game_context("Palworld", game_id=PALWORLD)
    assert view._collection_mode == COLLECTION_MODE_NORMAL
    assert view._current_collection_id is None
    assert view._selected_mod_ids == []
    view.deleteLater()


def test_selection_cleared_on_content_enter(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Sel", db=db)
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    view._selected_mod_ids = ["12", "34"]
    view._selection_anchor_mod_id = "12"
    view._open_collection_content(rec.collection_id)
    assert view._selected_mod_ids == []
    assert view._selection_anchor_mod_id == ""
    view.deleteLater()


def test_batch_membership_uses_selected_mod_ids_and_one_transaction(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88401", "A")
    m2 = _mod(db, STARDEW, "88402", "B")
    rec = coll.create_collection(STARDEW, "Batch", db=db)
    view = ModLibraryView()
    view._set_current_game_context("Stardew Valley", game_id=STARDEW)
    view._selected_mod_ids = [m1, m2]
    seen: dict[str, object] = {}

    def fake_exec(self: CollectionMembershipDialog) -> int:
        seen["rows"] = list(self._initial.items())
        return int(QDialog.DialogCode.Accepted)

    def fake_edits(self: CollectionMembershipDialog) -> tuple[list[int], list[int]]:
        return ([rec.collection_id], [])

    monkeypatch.setattr(CollectionMembershipDialog, "exec", fake_exec)
    monkeypatch.setattr(CollectionMembershipDialog, "edits", fake_edits)
    view._on_set_collections_requested()
    assert set(coll.list_collection_member_ids(rec.collection_id, db=db)) == {m1, m2}
    assert db.get_mod(m1) is not None
    coll.delete_collection(rec.collection_id, db=db)
    assert db.get_mod(m1) is not None
    assert db.get_mod(m2) is not None
    view.deleteLater()


def test_single_mod_remove_membership(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88411", "Solo")
    rec = coll.create_collection(STARDEW, "Drop", db=db)
    coll.add_mod_to_collection(rec.collection_id, m1, db=db)
    view = ModLibraryView()
    view._set_current_game_context("Stardew Valley", game_id=STARDEW)
    view._selected_mod_ids = [m1]
    monkeypatch.setattr(
        CollectionMembershipDialog, "exec", lambda self: int(QDialog.DialogCode.Accepted)
    )
    monkeypatch.setattr(
        CollectionMembershipDialog, "edits", lambda self: ([], [rec.collection_id])
    )
    view._on_set_collections_requested()
    assert coll.list_collection_member_ids(rec.collection_id, db=db) == []
    assert db.get_mod(m1) is not None
    view.deleteLater()


def test_membership_dialog_reflects_states(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Shown", db=db)
    rows = coll.membership_check_states(STARDEW, ["1"], db=db)
    dialog = CollectionMembershipDialog(rows)
    assert dialog._list.count() == 1
    item = dialog._list.item(0)
    assert item is not None
    assert item.checkState() == Qt.CheckState.Unchecked
    item.setCheckState(Qt.CheckState.Checked)
    add_ids, remove_ids = dialog.edits()
    assert rec.collection_id in add_ids
    assert remove_ids == []
    dialog.deleteLater()


def test_mod_belongs_to_multiple_collections_via_apply(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    mid = _mod(db, STARDEW, "88421", "Multi")
    a = coll.create_collection(STARDEW, "CA", db=db)
    b = coll.create_collection(STARDEW, "CB", db=db)
    coll.apply_collection_memberships(STARDEW, [mid], [a.collection_id, b.collection_id], [], db=db)
    assert set(coll.list_collection_ids_for_mods([mid], db=db)[mid]) == {
        a.collection_id,
        b.collection_id,
    }


def test_content_search_reuses_filter_sort_entries(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    keep = _mod(db, STARDEW, "88501", "KeepMe")
    drop = _mod(db, STARDEW, "88502", "DropMe")
    rec = coll.create_collection(STARDEW, "Search", db=db)
    coll.add_mods_to_collection(rec.collection_id, [keep, drop], db=db)
    view = ModLibraryView()
    _install_rows(
        view, [(keep, "KeepMe", 1.0), (drop, "DropMe", 2.0)], tmp_path
    )
    _enter_list(view, STARDEW, "Stardew Valley")
    view._open_collection_content(rec.collection_id)
    view.search_box.setText("Keep")
    view._last_filter_sig = None
    view._apply_view_filter()
    names = [i.display_name for i, _p in view._filtered_row_entries]
    assert names == ["KeepMe"]
    view.deleteLater()
