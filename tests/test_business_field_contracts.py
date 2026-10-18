"""Partial user edits and game-scoped labels preserve unrelated persistence."""
import sqlite3
import pytest
from core.db_manager import DatabaseManager
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture
def database(tmp_path):
    db = DatabaseManager(tmp_path / 'contracts.db')
    for app in (42, 43):
        db.update_game_deploy_config(app, name=f'Isolated {app}')
    ids = [create_steam_test_mod(db, external_id=str(901+i), app_id=app, title='Mod').mod_id
           for i, app in enumerate((42, 43, 0))]
    for pk in ids:
        db.update_mod_user_metadata(pk, {'display_name': 'Name', 'custom_description': 'Description',
                                      'user_notes': 'Notes', 'favorite': True})
        db.set_mod_type_id(pk, 7)
    yield db, ids
    db.close()


def record(db, pk):
    return dict(db._conn.execute('SELECT * FROM mods WHERE mod_id=?', (pk,)).fetchone())


@pytest.mark.parametrize('patch', [{'favorite': False}, {'user_notes': ''}, {'display_name': None},
                                  {'custom_description': ''}, {}, {'source_url': 'https://example.com'}])
def test_partial_metadata_has_no_implicit_clears(database, patch):
    db, ids = database
    before = record(db, ids[0])
    db.update_mod_user_metadata(ids[0], patch)
    after = record(db, ids[0])
    allowed = set(patch) | {'updated_at'}
    if {'display_name', 'custom_description'} & set(patch): allowed.add('user_override_fields')
    assert {k:v for k,v in before.items() if k not in allowed} == {
        k:v for k,v in after.items() if k not in allowed}


def test_type_tag_matrix_and_restart(database):
    db, ids = database
    pk = ids[0]
    before = record(db, pk)
    for label in ('First', 'Second'):
        db.add_category_tag(pk, label)
        assert db.get_mod_type_id(pk) == 7
    db.remove_category_tag(pk, 'First')
    assert db.get_category_tags(pk) == ['Second'] and db.get_mod_type_id(pk) == 7
    db.set_mod_type_id(pk, 8)
    assert db.get_category_tags(pk) == ['Second']
    db.remove_category_tag(pk, 'Second')
    assert db.get_category_tags(pk) == [] and db.get_mod_type_id(pk) == 8
    after = record(db, pk)
    assert {k:v for k,v in before.items() if k not in ('type_id','updated_at')} == {
        k:v for k,v in after.items() if k not in ('type_id','updated_at')}
    with sqlite3.connect(db.db_path) as reopened:
        assert reopened.execute('SELECT type_id FROM mods WHERE mod_id=?', (pk,)).fetchone()[0] == 8


def test_labels_are_scoped_by_authoritative_game_without_schema_migration(database):
    db, ids = database
    before = [record(db, pk) for pk in ids]
    db.add_category_tag(ids[0], 'Role')
    assert db.list_all_category_tags(app_id=42) == ['Role']
    assert db.list_all_category_tags(app_id=43) == []
    assert db.list_all_category_tags(app_id=0) == []
    db.add_category_tag(ids[1], 'Role')
    db.add_category_tag(ids[2], 'Unassigned')
    db.remove_category_tag(ids[1], 'Role')
    assert db.get_category_tags(ids[0]) == ['Role']
    assert db.list_all_category_tags(app_id=43) == []
    assert db.list_all_category_tags(app_id=0) == ['Unassigned']
    for pk, original in zip(ids, before):
        assert {k:v for k,v in original.items() if k!='updated_at'} == {
            k:v for k,v in record(db, pk).items() if k!='updated_at'}
