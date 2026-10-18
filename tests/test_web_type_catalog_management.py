"""Catalog CRUD works at game scope with no selected asset, preserving tags/IDs."""
import json
import sqlite3
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.organization_validation import initialize


def test_catalog_crud_and_restart_without_asset_context(tmp_path):
    app=QApplication.instance() or QApplication([])
    resources,ids=initialize(tmp_path/'catalog')
    db=resources.db
    b=LibraryBridge(LibraryAdapter(db,writable=True),resources=resources)
    try:
        snap=b.execute('library',{'game':1142710})
        args={'game':'1142710','snapshot':snap['snapshot']}
        before_tags=db.get_category_tags(ids[1].mod_id)
        catalog=b.execute('mod-types',args)
        catalog=b.execute_command('type-create',{**args,'version':catalog['version'],'name':'Role'})
        tid=next(t['type_id'] for t in catalog['types'] if t['name']=='Role')
        db.set_mod_type_id(ids[1].mod_id,tid)
        catalog=b.execute_command('type-rename',{**args,'version':catalog['version'],'type_id':tid,'name':'Character'})
        assert db.get_mod_type_id(ids[1].mod_id)==tid
        assert db.get_category_tags(ids[1].mod_id)==before_tags
        snap=b.execute('library',{'game':1142710})
        assert next(t for t in snap['types'] if t['type_id']==tid)['name']=='Character'
        assert next(a for a in snap['assets'] if a['internal_id']==ids[1].internal_id)['type']=='Character'
        persisted=json.loads(resources.path('data/mod_types.json').read_text(encoding='utf8'))
        assert any(t['id']==tid and t['name']=='Character' for t in persisted['games']['1142710']['types'])
        args['snapshot']=snap['snapshot']
        catalog=b.execute('mod-types',args)
        b.execute_command('type-delete',{**args,'version':catalog['version'],'type_id':tid,'confirm':True})
        assert db.get_mod_type_id(ids[1].mod_id) is None
        assert db.get_category_tags(ids[1].mod_id)==before_tags
        with sqlite3.connect(db.db_path) as reopened:
            assert reopened.execute('SELECT type_id FROM mods WHERE mod_id=?',(ids[1].mod_id,)).fetchone()[0] is None
        assert list(resources.path('data/type_catalog_recovery').glob('*/mod_types.json'))
    finally:
        b.shutdown();db.close()
