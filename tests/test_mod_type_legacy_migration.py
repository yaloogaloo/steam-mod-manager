"""Legacy game_categories / category tags → Type Definition + mods.type_id."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.mod_type_catalog import reset_mod_type_catalog
from services.mod_type_legacy_migration import migrate_legacy_mod_types
from tests.helpers.identity import create_steam_test_mod
from ui.library_query import FILTER_CATEGORY_ALL, ModFilterIndex, matches_category_filter


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "legacy_types.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def catalog(tmp_path: Path):
    return reset_mod_type_catalog(tmp_path / "mod_types.json")


def _mod(db: DatabaseManager, *, external_id: str, app_id: int, title: str):
    db.update_game_deploy_config(app_id, name=f"Game{app_id}", mod_path="")
    return create_steam_test_mod(
        db,
        external_id=external_id,
        title=title,
        app_id=app_id,
        game_name=f"Game{app_id}",
    )


def test_game_categories_become_type_definitions(catalog, db: DatabaseManager) -> None:
    db.add_game_category(1142710, "装备")
    db.add_game_category(1142710, "法术")
    report = migrate_legacy_mod_types(catalog, db)
    names = {t.name: t.type_id for t in catalog.list_types(1142710)}
    assert names["装备"] >= 1
    assert names["法术"] >= 1
    assert names["装备"] != names["法术"]
    assert report.created_types == 2
    assert catalog.is_legacy_migrated() is True


def test_mod_tags_bind_type_id(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="11", app_id=42, title="A")
    b = _mod(db, external_id="12", app_id=42, title="B")
    db.set_mod_category(str(a.mod_id), "装备")
    db.set_mod_category(str(b.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    assert db.get_mod_type_id(str(a.mod_id)) == tid
    assert db.get_mod_type_id(str(b.mod_id)) == tid


def test_same_game_same_name_one_type_id(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="21", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    report = migrate_legacy_mod_types(catalog, db)
    types = catalog.list_types(42)
    assert len(types) == 1
    assert types[0].name == "装备"
    assert report.created_types == 1


def test_different_games_same_name_independent(catalog, db: DatabaseManager) -> None:
    db.add_game_category(1142710, "装备")
    db.add_game_category(1086940, "装备")
    a = _mod(db, external_id="31", app_id=1142710, title="WH3")
    b = _mod(db, external_id="32", app_id=1086940, title="BG3")
    db.set_mod_category(str(a.mod_id), "装备")
    db.set_mod_category(str(b.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    ta = catalog.find_type_by_name(1142710, "装备")
    tb = catalog.find_type_by_name(1086940, "装备")
    assert ta is not None and tb is not None
    assert db.get_mod_type_id(str(a.mod_id)) == ta.type_id
    assert db.get_mod_type_id(str(b.mod_id)) == tb.type_id
    catalog.delete_type(1142710, ta.type_id, db)
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert db.get_mod_type_id(str(b.mod_id)) == tb.type_id
    assert catalog.find_type_by_name(1086940, "装备") is not None


def test_migration_preserves_mod_counts(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    db.add_game_category(42, "法术")
    for i in range(3):
        created = _mod(db, external_id=str(40 + i), app_id=42, title=f"G{i}")
        db.set_mod_category(str(created.mod_id), "装备")
    for i in range(2):
        created = _mod(db, external_id=str(50 + i), app_id=42, title=f"S{i}")
        db.set_mod_category(str(created.mod_id), "法术")
    report = migrate_legacy_mod_types(catalog, db)
    by_name = {row.legacy_name: row for row in report.per_game if row.app_id == 42}
    assert by_name["装备"].affected_mods == 3
    assert by_name["法术"].affected_mods == 2
    assert len(db.list_mod_ids_with_type(42, by_name["装备"].type_id)) == 3
    assert len(db.list_mod_ids_with_type(42, by_name["法术"].type_id)) == 2


def test_filter_restores_all_legacy_types(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    db.add_game_category(42, "拓展")
    a = _mod(db, external_id="61", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    types = catalog.list_types(42)
    assert {t.name for t in types} == {"装备", "拓展"}
    gear = catalog.find_type_by_name(42, "装备")
    index = ModFilterIndex(
        mod_id=str(a.mod_id),
        display_name="A",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1,
        sort_name="A",
        type_id=gear.type_id,
    )
    assert matches_category_filter(index, str(gear.type_id))
    assert matches_category_filter(index, FILTER_CATEGORY_ALL)
    assert not matches_category_filter(index, "装备")


def test_rename_json_does_not_change_mods(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="71", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    payload["games"]["42"]["types"][0]["name"] = "武器装备"
    catalog.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    catalog.reload()
    assert db.get_mod_type_id(str(a.mod_id)) == tid
    assert catalog.resolve_name(42, tid) == "武器装备"
    assert db.get_category_tags(str(a.mod_id)) == ["装备"]


def test_delete_unbinds_and_reload_does_not_resurrect(
    catalog, db: DatabaseManager
) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="81", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    assert catalog.delete_type(42, tid, db) is True
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert db.get_category_tags(str(a.mod_id)) == ["装备"]
    assert "装备" in db.list_game_categories(42)

    catalog.reload(db=db, reconcile=True)
    assert catalog.find_type_by_name(42, "装备") is None
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert catalog.is_legacy_migrated() is True


def test_untagged_mod_stays_null(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="91", app_id=42, title="A")
    b = _mod(db, external_id="92", app_id=42, title="B")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    assert db.get_mod_type_id(str(a.mod_id)) is not None
    assert db.get_mod_type_id(str(b.mod_id)) is None


def test_migration_is_idempotent(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="101", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    first = migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    second = migrate_legacy_mod_types(catalog, db)
    assert second.already_migrated is True
    assert catalog.find_type_by_name(42, "装备").type_id == tid
    assert db.get_mod_type_id(str(a.mod_id)) == tid
    assert len(catalog.list_types(42)) == 1
    assert first.created_types == 1
    assert second.created_types == 0


def test_existing_json_ids_are_preserved(catalog, db: DatabaseManager) -> None:
    preexisting = catalog.add_type(42, "装备")
    db.add_game_category(42, "装备")
    db.add_game_category(42, "法术")
    a = _mod(db, external_id="111", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    assert catalog.find_type_by_name(42, "装备").type_id == preexisting.type_id
    assert db.get_mod_type_id(str(a.mod_id)) == preexisting.type_id
    spell = catalog.find_type_by_name(42, "法术")
    assert spell is not None
    assert spell.type_id != preexisting.type_id


def test_legacy_tables_are_not_deleted(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="121", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    before_names = db.list_legacy_type_names_by_game()
    before_tags = db.list_legacy_mod_type_names()
    migrate_legacy_mod_types(catalog, db)
    assert db.list_legacy_type_names_by_game() == before_names
    assert db.list_legacy_mod_type_names() == before_tags
    assert "装备" in db.list_game_categories(42)
    assert db.get_category_tags(str(a.mod_id)) == ["装备"]


def test_migration_does_not_touch_identity(catalog, db: DatabaseManager) -> None:
    a = _mod(db, external_id="131", app_id=42, title="A")
    db.add_game_category(42, "装备")
    db.set_mod_category(str(a.mod_id), "装备")
    pk = str(a.mod_id)
    before = db.get_mod_display_info(pk)
    assert before is not None
    migrate_legacy_mod_types(catalog, db)
    after = db.get_mod_display_info(pk)
    assert after is not None
    assert after.workspace_id == before.workspace_id == a.workspace_id
    assert after.external_id == before.external_id
    assert db.find_mod_by_internal_id(a.internal_id) == pk
    assert db.get_mod_type_id(pk) is not None


def test_deleted_json_does_not_resurrect_from_legacy(
    catalog, db: DatabaseManager
) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="141", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    catalog.delete_type(42, tid, db)
    catalog.path.unlink()
    again = reset_mod_type_catalog(catalog.path)
    again.reload(db=db, reconcile=True)
    assert again.find_type_by_name(42, "装备") is None
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert again.is_legacy_migrated() is True


def test_reload_keeps_ids_idempotent(catalog, db: DatabaseManager) -> None:
    db.add_game_category(42, "装备")
    a = _mod(db, external_id="151", app_id=42, title="A")
    db.set_mod_category(str(a.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    tid = catalog.find_type_by_name(42, "装备").type_id
    again = reset_mod_type_catalog(catalog.path)
    again.reload(db=db, reconcile=True)
    assert again.find_type_by_name(42, "装备").type_id == tid
    assert db.get_mod_type_id(str(a.mod_id)) == tid
    assert len(again.list_types(42)) == 1


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_filter_combo_restores_migrated_types(
    qapp, catalog, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
    from ui.library_view import GAME_ROLE, ModLibraryView

    library = tmp_path / "mod"
    folder = library / "Game42" / "ModA"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"161","title":"ModA","app_id":42,"game_name":"Game42"}',
        encoding="utf-8",
    )
    db.add_game_category(42, "装备")
    db.add_game_category(42, "法术")
    created = _mod(db, external_id="161", app_id=42, title="ModA")
    db.set_mod_category(str(created.mod_id), "装备")
    migrate_legacy_mod_types(catalog, db)
    gear = catalog.find_type_by_name(42, "装备")
    spell = catalog.find_type_by_name(42, "法术")
    assert gear is not None and spell is not None

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    from tests.qt_test_lifecycle import settle_qt_events

    view = ModLibraryView()
    try:
        view.set_target_root(str(library))
        view.refresh()
        for i in range(view.game_list.count()):
            item = view.game_list.item(i)
            if item is not None and str(item.data(GAME_ROLE) or "") == "Game42":
                view.game_list.setCurrentRow(i)
                break
        else:
            raise AssertionError("Game42 missing from sidebar")
        qapp.processEvents()
        view._refresh_category_combo()

        labels = [
            view.category_combo.itemText(i) for i in range(view.category_combo.count())
        ]
        assert "装备" in labels
        assert "法术" in labels
        assert view.category_combo.findData(str(gear.type_id)) >= 0
        assert view.category_combo.findData(str(spell.type_id)) >= 0
    finally:
        try:
            view.close()
            view.deleteLater()
        except RuntimeError:
            pass
        settle_qt_events()
