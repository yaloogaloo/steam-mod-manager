"""Type binding and real user tags remain independent, including equal names."""
import sqlite3
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.organization_validation import initialize


@pytest.mark.parametrize('has_type,has_tag',[(True,False),(False,True),(True,True),(False,False)])
def test_type_and_user_tag_fields_are_independent(tmp_path,has_type,has_tag):
    app=QApplication.instance() or QApplication([])
    resources,ids=initialize(tmp_path/'independence')
    bridge=LibraryBridge(LibraryAdapter(resources.db,writable=True),resources=resources)
    db=resources.db;pk=ids[0].mod_id
    try:
        for tag in db.get_category_tags(pk):db.remove_mod_tag(pk,'category',tag)
        if has_tag:db.add_mod_tag(pk,'category','前置')
        snapshot=bridge.execute('library',{'game':1142710});item=next(a for a in snapshot['assets'] if a['internal_id']==ids[0].internal_id)
        args={'game':'1142710','snapshot':snapshot['snapshot'],'token':item['token'],'internal_id':item['internal_id']}
        catalog=bridge.execute('mod-types',args)
        catalog=bridge.execute_command('type-create',{**args,'version':catalog['version'],'name':'前置'})
        tid=next(t['type_id'] for t in catalog['types'] if t['name']=='前置')
        db.set_mod_type_id(pk,tid if has_type else None)
        before_tags=db.get_category_tags(pk);category=db.get_mod_display_info(pk).category
        details=bridge.execute('detail',{'token':item['token']})
        assert details['tags']==before_tags
        assert details['type_id']==(tid if has_type else None)
        bridge.execute_command('type-set',{**args,'version':catalog['version'],'type_id':None if has_type else tid})
        assert db.get_category_tags(pk)==before_tags
        assert db.get_mod_display_info(pk).category==category
        current_type=db.get_mod_type_id(pk)
        tags=bridge.execute('tags',args)
        bridge.execute_command('tag-add',{**args,'version':tags['version'],'label':'Independent user tag'})
        assert db.get_mod_type_id(pk)==current_type
        assert set(db.get_category_tags(pk))==set(before_tags+['Independent user tag'])
        with sqlite3.connect(db.db_path) as reopened:
            assert reopened.execute('SELECT type_id FROM mods WHERE mod_id=?',(pk,)).fetchone()[0]==current_type
    finally:bridge.shutdown();db.close()


def test_card_adapter_never_turns_type_into_user_tags(monkeypatch):
    from services.mod_list_item import ModListItem
    from services.mod_library_cache import list_item_to_card_data
    import services.mod_type_catalog as types
    from types import SimpleNamespace
    monkeypatch.setattr(types,'get_mod_type_catalog',lambda:SimpleNamespace(resolve_name=lambda *a:'内容'))
    for tags in ('','内容','My own label'):
        item=ModListItem(internal_id='sample',workspace_id='1',game_id=916440,game_folder='Anno',name='Sample',type_id=10,category_tags=tags)
        card=list_item_to_card_data(item)
        assert card.tags==tags and card.category_tags==tags
