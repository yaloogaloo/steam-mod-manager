"""Type Definition file + mods.type_id binding (game-scoped)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import MOD_TYPE_BEAUTIFY, MOD_TYPE_EXTENSION
from services.mod_type_catalog import (
    ModTypeCatalog,
    ModTypeCatalogError,
    reset_mod_type_catalog,
)
from tests.helpers.identity import create_steam_test_mod
from ui.library_query import FILTER_CATEGORY_ALL, ModFilterIndex, matches_category_filter


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mod_types.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def catalog(tmp_path: Path) -> ModTypeCatalog:
    path = tmp_path / "mod_types.json"
    return reset_mod_type_catalog(path)


def _seed_mod(db: DatabaseManager, *, external_id: str, app_id: int, title: str):
    db.update_game_deploy_config(app_id, name=f"Game{app_id}", mod_path="")
    return create_steam_test_mod(
        db, external_id=external_id, title=title, app_id=app_id, game_name=f"Game{app_id}"
    )


def test_type_id_binding_not_name(catalog: ModTypeCatalog, db: DatabaseManager) -> None:
    created = catalog.add_type(1142710, "装备")
    mod = _seed_mod(db, external_id="101", app_id=1142710, title="A")
    pk = str(mod.mod_id)
    db.set_mod_type_id(pk, created.type_id)
    assert db.get_mod_type_id(pk) == created.type_id
    assert db.get_mod_display_info(pk).type_id == created.type_id
    assert catalog.resolve_name(1142710, created.type_id) == "装备"


def test_rename_by_editing_persistence_file(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    created = catalog.add_type(42, "装备")
    a = _seed_mod(db, external_id="201", app_id=42, title="A")
    b = _seed_mod(db, external_id="202", app_id=42, title="B")
    db.set_mod_type_id(str(a.mod_id), created.type_id)
    db.set_mod_type_id(str(b.mod_id), created.type_id)

    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    payload["games"]["42"]["types"][0]["name"] = "武器装备"
    catalog.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    catalog.reload()

    assert db.get_mod_type_id(str(a.mod_id)) == created.type_id
    assert db.get_mod_type_id(str(b.mod_id)) == created.type_id
    assert catalog.resolve_name(42, created.type_id) == "武器装备"


def test_delete_type_unbinds_mods_not_reassign(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    gear = catalog.add_type(42, "装备")
    spell = catalog.add_type(42, "法术")
    a = _seed_mod(db, external_id="301", app_id=42, title="A")
    b = _seed_mod(db, external_id="302", app_id=42, title="B")
    c = _seed_mod(db, external_id="303", app_id=42, title="C")
    db.set_mod_type_id(str(a.mod_id), gear.type_id)
    db.set_mod_type_id(str(b.mod_id), gear.type_id)
    db.set_mod_type_id(str(c.mod_id), spell.type_id)

    assert catalog.delete_type(42, gear.type_id, db) is True
    assert catalog.get(42, gear.type_id) is None
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert db.get_mod_type_id(str(b.mod_id)) is None
    assert db.get_mod_type_id(str(c.mod_id)) == spell.type_id
    assert catalog.resolve_name(42, spell.type_id) == "法术"


def test_delete_does_not_affect_other_game(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    wh3 = catalog.add_type(1142710, "装备")
    bg3 = catalog.add_type(1086940, "装备")
    assert wh3.type_id == bg3.type_id
    a = _seed_mod(db, external_id="401", app_id=1142710, title="WH3")
    b = _seed_mod(db, external_id="402", app_id=1086940, title="BG3")
    db.set_mod_type_id(str(a.mod_id), wh3.type_id)
    db.set_mod_type_id(str(b.mod_id), bg3.type_id)

    catalog.delete_type(1142710, wh3.type_id, db)
    assert db.get_mod_type_id(str(a.mod_id)) is None
    assert db.get_mod_type_id(str(b.mod_id)) == bg3.type_id
    assert catalog.resolve_name(1086940, bg3.type_id) == "装备"


def test_reload_persistence_keeps_delete(
    catalog: ModTypeCatalog, db: DatabaseManager, tmp_path: Path
) -> None:
    created = catalog.add_type(42, "装备")
    mod = _seed_mod(db, external_id="501", app_id=42, title="A")
    db.set_mod_type_id(str(mod.mod_id), created.type_id)
    catalog.delete_type(42, created.type_id, db)

    again = reset_mod_type_catalog(catalog.path)
    again.reload(db=db, reconcile=True)
    assert again.get(42, created.type_id) is None
    assert db.get_mod_type_id(str(mod.mod_id)) is None


def test_ghost_type_id_cleared_on_reload(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    catalog.add_type(42, "装备")
    mod = _seed_mod(db, external_id="601", app_id=42, title="Ghost")
    db.set_mod_type_id(str(mod.mod_id), 99, touch_updated_at=False)
    assert db.get_mod_type_id(str(mod.mod_id)) == 99
    catalog.reload(db=db, reconcile=True)
    assert db.get_mod_type_id(str(mod.mod_id)) is None
    assert catalog.get(42, 99) is None


def test_manual_file_delete_orphans_null(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    created = catalog.add_type(42, "装备")
    keep = catalog.add_type(42, "法术")
    mod = _seed_mod(db, external_id="701", app_id=42, title="A")
    other = _seed_mod(db, external_id="702", app_id=42, title="B")
    db.set_mod_type_id(str(mod.mod_id), created.type_id)
    db.set_mod_type_id(str(other.mod_id), keep.type_id)

    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    payload["games"]["42"]["types"] = [
        item
        for item in payload["games"]["42"]["types"]
        if int(item["id"]) != created.type_id
    ]
    catalog.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    catalog.reload(db=db, reconcile=True)
    assert db.get_mod_type_id(str(mod.mod_id)) is None
    assert db.get_mod_type_id(str(other.mod_id)) == keep.type_id


def test_duplicate_type_id_rejected(catalog: ModTypeCatalog) -> None:
    catalog.add_type(42, "装备")
    catalog.path.write_text(
        json.dumps(
            {
                "version": 1,
                "games": {
                    "42": {
                        "types": [
                            {"id": 12, "name": "装备"},
                            {"id": 12, "name": "法术"},
                        ]
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModTypeCatalogError, match="重复"):
        catalog.reload()
    # Previous valid in-memory catalog is not replaced by the corrupt file.
    assert catalog.list_types(42)[0].name == "装备"


def test_empty_name_rejected(catalog: ModTypeCatalog) -> None:
    with pytest.raises(ModTypeCatalogError):
        catalog.add_type(42, "  ")
    catalog.path.write_text(
        json.dumps(
            {
                "version": 1,
                "games": {"42": {"types": [{"id": 1, "name": ""}]}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModTypeCatalogError, match="空"):
        catalog.reload()


def test_filter_uses_type_id_not_name(catalog: ModTypeCatalog) -> None:
    created = catalog.add_type(42, "装备")
    bound = ModFilterIndex(
        mod_id="1",
        display_name="A",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1,
        sort_name="A",
        type_id=created.type_id,
        category_tags="装备",
    )
    other = ModFilterIndex(
        mod_id="2",
        display_name="B",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1,
        sort_name="B",
        type_id=created.type_id + 1,
        category_tags="装备",
    )
    assert matches_category_filter(bound, str(created.type_id))
    assert not matches_category_filter(other, str(created.type_id))
    assert not matches_category_filter(bound, "装备")
    assert matches_category_filter(bound, FILTER_CATEGORY_ALL)


def test_filter_gone_after_delete(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    created = catalog.add_type(42, "装备")
    catalog.delete_type(42, created.type_id, db)
    catalog.reload()
    assert catalog.get(42, created.type_id) is None
    assert [t.type_id for t in catalog.list_types(42)] == []


def test_duplicate_type_name_rejected(catalog: ModTypeCatalog) -> None:
    first = catalog.add_type(42, MOD_TYPE_EXTENSION)
    with pytest.raises(ModTypeCatalogError, match="已存在"):
        catalog.add_type(42, MOD_TYPE_EXTENSION)
    with pytest.raises(ModTypeCatalogError, match="已存在"):
        catalog.add_type(42, "拓展")
    assert catalog.extension_type_id(42) == first.type_id
    assert len(catalog.list_types(42)) == 1


def test_extension_type_id_survives_display_rename(
    catalog: ModTypeCatalog, db: DatabaseManager
) -> None:
    created = catalog.add_type(42, MOD_TYPE_EXTENSION)
    mod = _seed_mod(db, external_id="801", app_id=42, title="Ext")
    pk = str(mod.mod_id)
    db.set_mod_type_id(pk, created.type_id)
    assert catalog.extension_type_id(42) == created.type_id

    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    assert payload["games"]["42"]["extension_type_id"] == created.type_id
    payload["games"]["42"]["types"][0]["name"] = "扩展"
    catalog.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    catalog.reload()

    assert db.get_mod_type_id(pk) == created.type_id
    assert catalog.resolve_name(42, created.type_id) == "扩展"
    assert catalog.extension_type_id(42) == created.type_id
    assert catalog.is_extension_type(42, created.type_id)
    assert catalog.find_type_by_name(42, "拓展") is None


def test_extension_type_id_is_per_game_not_global(catalog: ModTypeCatalog) -> None:
    other = catalog.add_type(111, MOD_TYPE_BEAUTIFY)
    plain = catalog.add_type(111, "普通")
    ext_a = catalog.add_type(111, MOD_TYPE_EXTENSION)
    ext_b = catalog.add_type(222, MOD_TYPE_EXTENSION)
    assert catalog.extension_type_id(111) == ext_a.type_id
    assert catalog.extension_type_id(222) == ext_b.type_id
    assert ext_a.type_id != ext_b.type_id
    assert not catalog.is_extension_type(111, other.type_id)
    assert not catalog.is_extension_type(111, ext_b.type_id)
    assert catalog.is_extension_type(222, ext_b.type_id)
    assert catalog.unlocks_subcategory(111, other.type_id)
    assert catalog.unlocks_subcategory(111, ext_a.type_id)
    assert not catalog.unlocks_subcategory(111, plain.type_id)
    assert catalog.unlocks_subcategory(222, ext_b.type_id)


def test_extension_type_id_inferred_from_canonical_name(
    catalog: ModTypeCatalog,
) -> None:
    catalog.path.write_text(
        json.dumps(
            {
                "version": 1,
                "games": {
                    "42": {
                        "types": [
                            {"id": 3, "name": "美化"},
                            {"id": 7, "name": "拓展"},
                        ]
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog.reload()
    assert catalog.extension_type_id(42) == 7
    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    assert payload["games"]["42"]["extension_type_id"] == 7
    assert catalog.resolve_name(42, 7) == "拓展"
