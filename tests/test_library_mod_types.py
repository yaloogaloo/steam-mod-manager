"""Phase 11.3: per-game Mod type catalog without restoring the sidebar tree."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_query import merge_category_labels
from ui.library_view import GAME_CATEGORY_ROLE, GAME_ROLE, ModLibraryView
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mod_types.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(library: Path, db: DatabaseManager) -> None:
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    folder = library / "GameX" / "ModA"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"7101","title":"ModA","app_id":42,"game_name":"GameX"}',
        encoding="utf-8",
    )
    create_steam_test_mod(db, external_id="7101", title="ModA", app_id=42)



def _select_game(view: ModLibraryView, folder: str) -> None:
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item is None:
            continue
        if str(item.data(GAME_ROLE) or "") == folder:
            view.game_list.setCurrentRow(i)
            return
    raise AssertionError(f"game {folder!r} not in list")


def test_merge_category_labels_unions_defined_and_used() -> None:
    assert set(merge_category_labels(["角色"], ["美化", "角色"])) == {"角色", "美化"}


def test_defined_type_appears_in_combo_not_game_tree(
    qapp, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from services.mod_type_catalog import get_mod_type_catalog

    library = tmp_path / "mod"
    _seed(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    _select_game(view, "GameX")
    qapp.processEvents()

    created = get_mod_type_catalog().add_type(42, "角色")
    view._refresh_category_combo()
    view._apply_category_options_to_cards()

    labels = [
        view.category_combo.itemText(i) for i in range(view.category_combo.count())
    ]
    assert "角色" in labels
    assert view.category_combo.findData(str(created.type_id)) >= 0

    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        widget = view.game_list.itemWidget(item)
        assert widget is not None
        assert widget.objectName() == "GameTreeItem"
        assert not str(item.data(GAME_CATEGORY_ROLE) or "").strip()
        assert widget.name_label.text() != "角色"

    assert view._cards
    assert (created.type_id, "角色") in view._cards[0]._category_options


def test_delete_type_removes_definition_and_unbinds_mod(
    qapp, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox
    from services.mod_type_catalog import get_mod_type_catalog

    library = tmp_path / "mod"
    _seed(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    _select_game(view, "GameX")
    qapp.processEvents()

    created = get_mod_type_catalog().add_type(42, "美化")
    db.set_mod_type_id("7101", created.type_id)
    from dataclasses import replace

    index, card = view._card_entries[0]
    view._card_entries[0] = (replace(index, type_id=created.type_id), card)
    view._refresh_category_combo()
    idx = view.category_combo.findData(str(created.type_id))
    assert idx >= 0
    view.category_combo.setCurrentIndex(idx)
    assert view.btn_delete_game_type.isEnabled()
    view._on_delete_game_type()
    assert get_mod_type_catalog().get(42, created.type_id) is None
    assert db.get_mod_type_id("7101") is None
    assert view.category_combo.findData(str(created.type_id)) < 0


def test_type_helpers_do_not_scan_disk() -> None:
    from ui import library_view as lv

    src = (
        inspect.getsource(lv.ModLibraryView._current_game_type_catalog)
        + inspect.getsource(lv.ModLibraryView._merged_category_options)
        + inspect.getsource(lv.ModLibraryView._on_add_game_type)
        + inspect.getsource(lv.ModLibraryView._refresh_category_combo)
    )
    assert "list_visible_mods" not in src
    assert "load_snapshot" not in inspect.getsource(
        lv.ModLibraryView._current_game_type_catalog
    )


def test_add_type_disabled_without_game(qapp) -> None:
    view = ModLibraryView()
    view.current_game_id = None
    view._sync_type_manage_buttons()
    assert not view.btn_add_game_type.isEnabled()
    assert not view.btn_delete_game_type.isEnabled()
