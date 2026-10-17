"""New frontend persistence seam contracts, always using pytest isolated roots."""
import sqlite3
from uuid import uuid4
import pytest
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter


@pytest.fixture
def database(tmp_path):
    db = DatabaseManager(tmp_path / 'next.db')
    db._conn.execute("INSERT INTO games(app_id,name,updated_at) VALUES(42,'Synthetic','2026-01-01')")
    for pk in (101, 102):
        db._conn.execute("INSERT INTO mods(mod_id,app_id,internal_id,workspace_id,title,updated_at) VALUES(?,?,?,?,?,?)",
                         (pk, 42, str(uuid4()), 'external-' + str(pk), 'Synthetic ' + str(pk), '2026-01-01'))
    db._conn.commit()
    yield db
    db.close()


def test_open_existing_never_initializes(database, monkeypatch):
    def refuse(*args):
        raise AssertionError('schema init called')
    monkeypatch.setattr(DatabaseManager, '_init_schema', refuse)
    db = DatabaseManager.open_existing(database.db_path)
    assert len(db.list_mod_list_items(game_id=42)) == 2
    with pytest.raises(sqlite3.DatabaseError):
        db._conn.execute('DELETE FROM mods')
    db.close()
    assert db.is_closed()


def test_missing_database_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    with pytest.raises(FileNotFoundError):
        DatabaseManager.open_existing(path)
    assert not path.exists()


def test_invalid_schema_not_repaired(tmp_path):
    path = tmp_path / 'invalid.db'
    sqlite3.connect(path).close()
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        DatabaseManager.open_existing(path)
    assert path.read_bytes() == before


def test_adapter_identity_and_favorite(database):
    adapter = LibraryAdapter(database, writable=True)
    asset = adapter.assets(42)[0]
    assert asset.internal_id != asset.workspace_id != str(asset.mod_pk)
    database.update_mod_user_metadata(asset.mod_pk, {'display_name': 'Custom',
        'custom_description': 'Keep description', 'user_notes': 'Keep notes', 'favorite': False})
    adapter.favorite(asset)
    info = adapter.detail(asset)
    assert info.favorite and info.user_display_name == 'Custom'
    assert info.custom_description == 'Keep description' and info.user_notes == 'Keep notes'


def test_collections_are_fk_membership_only(database):
    adapter = LibraryAdapter(database, writable=True)
    assets = adapter.assets(42)
    collection = adapter.create_collection(42, 'Collection')
    assert adapter.add_members(collection.collection_id, assets) == 2
    assert adapter.members(collection.collection_id) == {str(a.mod_pk) for a in assets}
    assert all(not a.deployed for a in adapter.assets(42))


def test_readonly_adapter_rejects_mutation(database):
    adapter = LibraryAdapter(database)
    with pytest.raises(PermissionError):
        adapter.favorite(adapter.assets(42)[0])


def test_stale_identity_refused(database):
    adapter = LibraryAdapter(database, writable=True)
    asset = adapter.assets(42)[0]
    database._conn.execute('UPDATE mods SET internal_id=? WHERE mod_id=?', (str(uuid4()), asset.mod_pk))
    database._conn.commit()
    with pytest.raises(RuntimeError):
        adapter.favorite(asset)


def test_next_entry_uses_same_lock_and_releases_on_failure(monkeypatch, tmp_path):
    import main
    from services import app_instance
    path = tmp_path / 'shared.lock'
    monkeypatch.setattr(app_instance, 'instance_lock_path', lambda: path)
    monkeypatch.setattr('frontend.admission.require_production_admission', lambda **kw: None)
    def fail(**kwargs):
        raise RuntimeError('construction failed')
    monkeypatch.setattr(main, '_run_gui', fail)
    with pytest.raises(RuntimeError):
        main.launch_gui(next_frontend=True)
    assert app_instance.acquire_instance_lock(path=path)
    app_instance.release_instance_lock()
