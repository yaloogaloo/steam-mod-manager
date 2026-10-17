"""Collection Phase 1 — schema, CRUD, membership, and order persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, get_db
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services import collection as coll
from services.identity_service import create_mod_identity, identity_create_scope

STARDEW = 413150
PALWORLD = 1623730


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "collection_phase1.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _game(db: DatabaseManager, app_id: int, name: str) -> None:
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))


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


def test_create_collection(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "  Vanilla+  ", db=db)
    assert rec.collection_id > 0
    assert rec.app_id == STARDEW
    assert rec.name == "Vanilla+"
    assert rec.cover_path == ""
    assert rec.mod_count == 0
    listed = coll.list_collections(STARDEW, db=db)
    assert [r.name for r in listed] == ["Vanilla+"]


def test_empty_name_rejected(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    with pytest.raises(ValueError, match="non-empty"):
        coll.create_collection(STARDEW, "   ", db=db)
    with pytest.raises(ValueError, match="non-empty"):
        coll.create_collection(STARDEW, "", db=db)


def test_duplicate_name_per_game_case_insensitive(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    coll.create_collection(STARDEW, "Core", db=db)
    with pytest.raises(ValueError, match="already exists"):
        coll.create_collection(STARDEW, "core", db=db)
    rec = coll.create_collection(STARDEW, "Core 2", db=db)
    with pytest.raises(ValueError, match="already exists"):
        coll.rename_collection(rec.collection_id, "CORE", db=db)


def test_collections_isolated_by_game(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    _game(db, PALWORLD, "Palworld")
    coll.create_collection(STARDEW, "Shared Name", db=db)
    pal = coll.create_collection(PALWORLD, "Shared Name", db=db)
    assert [r.name for r in coll.list_collections(STARDEW, db=db)] == ["Shared Name"]
    assert [r.collection_id for r in coll.list_collections(PALWORLD, db=db)] == [
        pal.collection_id
    ]
    assert coll.list_collections(STARDEW, db=db)[0].collection_id != pal.collection_id


def test_delete_collection_does_not_delete_mods(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    mid = _mod(db, STARDEW, "88001", "Mod A")
    rec = coll.create_collection(STARDEW, "Pack", db=db)
    coll.add_mod_to_collection(rec.collection_id, mid, db=db)
    assert db.get_mod(mid) is not None
    assert coll.delete_collection(rec.collection_id, db=db) is True
    assert coll.get_collection(rec.collection_id, db=db) is None
    assert db.get_mod(mid) is not None
    with db._lock:
        left = db._conn.execute(
            "SELECT COUNT(*) AS n FROM collection_mods WHERE collection_id = ?",
            (rec.collection_id,),
        ).fetchone()
    assert int(left["n"]) == 0


def test_mod_can_belong_to_multiple_collections(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    mid = _mod(db, STARDEW, "88002", "Shared Mod")
    a = coll.create_collection(STARDEW, "A", db=db)
    b = coll.create_collection(STARDEW, "B", db=db)
    assert coll.add_mod_to_collection(a.collection_id, mid, db=db) is True
    assert coll.add_mod_to_collection(b.collection_id, mid, db=db) is True
    assert coll.list_collection_member_ids(a.collection_id, db=db) == [mid]
    assert coll.list_collection_member_ids(b.collection_id, db=db) == [mid]
    assert coll.get_collection(a.collection_id, db=db).mod_count == 1
    assert coll.get_collection(b.collection_id, db=db).mod_count == 1


def test_collection_can_hold_multiple_mods(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88003", "One")
    m2 = _mod(db, STARDEW, "88004", "Two")
    rec = coll.create_collection(STARDEW, "Many", db=db)
    added = coll.add_mods_to_collection(rec.collection_id, [m1, m2], db=db)
    assert added == 2
    members = coll.list_collection_member_ids(rec.collection_id, db=db)
    assert set(members) == {m1, m2}
    assert coll.get_collection(rec.collection_id, db=db).mod_count == 2


def test_membership_unique_and_internal_id_pk(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    mid = _mod(db, STARDEW, "88005", "Once")
    rec = coll.create_collection(STARDEW, "Unique", db=db)
    assert coll.add_mod_to_collection(rec.collection_id, mid, db=db) is True
    assert coll.add_mod_to_collection(rec.collection_id, mid, db=db) is False
    assert coll.list_collection_member_ids(rec.collection_id, db=db) == [mid]
    with db._lock:
        row = db._conn.execute(
            "SELECT mod_id FROM collection_mods WHERE collection_id = ?",
            (rec.collection_id,),
        ).fetchone()
        cols = {
            str(r[1])
            for r in db._conn.execute("PRAGMA table_info(collection_mods)").fetchall()
        }
        fk = db._conn.execute("PRAGMA foreign_key_list(collection_mods)").fetchall()
    assert int(row["mod_id"]) == int(mid)
    assert "workspace_id" not in cols
    fk_tables = {str(r["table"]) for r in fk}
    assert "collections" in fk_tables
    assert "mods" in fk_tables
    cascade = [
        str(r["table"])
        for r in fk
        if str(r["on_delete"] or "").upper() == "CASCADE"
    ]
    assert cascade == ["collections"]


def test_collection_order_persists(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    a = coll.create_collection(STARDEW, "A", db=db)
    b = coll.create_collection(STARDEW, "B", db=db)
    c = coll.create_collection(STARDEW, "C", db=db)
    assert [r.name for r in coll.list_collections(STARDEW, db=db)] == ["A", "B", "C"]
    next_order = coll.move_collection_in_order(
        [a.collection_id, b.collection_id, c.collection_id],
        b.collection_id,
        a.collection_id,
    )
    coll.reorder_collections(STARDEW, next_order, db=db)
    assert [r.name for r in coll.list_collections(STARDEW, db=db)] == ["B", "A", "C"]
    assert [r.sort_order for r in coll.list_collections(STARDEW, db=db)] == [0, 1, 2]


def test_membership_rejects_mod_from_other_game(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    _game(db, PALWORLD, "Palworld")
    pal_mod = _mod(db, PALWORLD, "99001", "Pal Mod")
    rec = coll.create_collection(STARDEW, "SDV Only", db=db)
    with pytest.raises(ValueError, match="not in this game"):
        coll.add_mod_to_collection(rec.collection_id, pal_mod, db=db)


def test_future_content_must_reuse_library_sort() -> None:
    """Collection Content (later phase) must call filter_sort_entries, not a new sorter."""
    assert "filter_sort_entries" in (coll.__doc__ or "")
    assert "collection_sort_entries" in (coll.__doc__ or "")
    assert "Do not add" in (coll.__doc__ or "")
    import ui.library_query as lq

    assert not hasattr(lq, "collection_sort_entries")
    assert hasattr(lq, "filter_sort_entries")


def test_get_db_service_path_uses_same_tables(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Via Service", db=db)
    assert get_db() is db
    assert coll.get_collection(rec.collection_id).name == "Via Service"


def test_apply_memberships_one_transaction_add_and_remove(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88101", "M1")
    m2 = _mod(db, STARDEW, "88102", "M2")
    a = coll.create_collection(STARDEW, "AddMe", db=db)
    b = coll.create_collection(STARDEW, "RemoveMe", db=db)
    coll.add_mods_to_collection(b.collection_id, [m1, m2], db=db)
    import inspect

    body = inspect.getsource(DatabaseManager.apply_collection_memberships)
    assert body.count("self._conn.commit()") == 1
    inserted, removed = coll.apply_collection_memberships(
        STARDEW, [m1, m2], [a.collection_id], [b.collection_id], db=db
    )
    assert inserted == 2
    assert removed == 2
    assert set(coll.list_collection_member_ids(a.collection_id, db=db)) == {m1, m2}
    assert coll.list_collection_member_ids(b.collection_id, db=db) == []
    assert db.get_mod(m1) is not None
    assert db.get_mod(m2) is not None


def test_membership_check_states_mixed_and_edits(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    m1 = _mod(db, STARDEW, "88111", "One")
    m2 = _mod(db, STARDEW, "88112", "Two")
    both = coll.create_collection(STARDEW, "Both", db=db)
    only = coll.create_collection(STARDEW, "OnlyOne", db=db)
    none = coll.create_collection(STARDEW, "None", db=db)
    coll.add_mods_to_collection(both.collection_id, [m1, m2], db=db)
    coll.add_mod_to_collection(only.collection_id, m1, db=db)
    states = {rec.collection_id: st for rec, st in coll.membership_check_states(STARDEW, [m1, m2], db=db)}
    assert states[both.collection_id] == "all"
    assert states[only.collection_id] == "mixed"
    assert states[none.collection_id] == "none"
    add_ids, remove_ids = coll.compute_membership_edits(
        states,
        {
            both.collection_id: "none",
            only.collection_id: "all",
            none.collection_id: "none",
        },
    )
    assert both.collection_id in remove_ids
    assert only.collection_id in add_ids
    assert none.collection_id not in add_ids
    assert none.collection_id not in remove_ids


def test_cover_path_persists_and_survives_rename(db: DatabaseManager) -> None:
    _game(db, STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Art", db=db)
    assert rec.cover_path == ""
    updated = db.update_collection_cover_path(
        rec.collection_id, "collection_covers/1.png"
    )
    assert updated.cover_path == "collection_covers/1.png"
    renamed = coll.rename_collection(rec.collection_id, "Art 2", db=db)
    assert renamed.cover_path == "collection_covers/1.png"
    assert renamed.collection_id == rec.collection_id
    assert coll.delete_collection(rec.collection_id, db=db) is True
    assert coll.get_collection(rec.collection_id, db=db) is None

