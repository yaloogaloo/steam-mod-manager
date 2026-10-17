"""Restored UI capabilities use the existing DAL and retain source/identity boundaries."""
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.organization_validation import initialize


def test_conflict_query_reuses_detector_without_mutating_annotations(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'conflict-query')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710})
        item=snapshot['assets'][0]
        args={'game':'1142710','snapshot':snapshot['snapshot'],'token':item['token'],'internal_id':item['internal_id']}
        before=r.db._conn.total_changes
        result=b.execute('conflict-report',args)
        assert str(result['mod_id'])==str(b._assets[item['token']].mod_pk)
        assert isinstance(result['conflicts'],list)
        assert r.db._conn.total_changes==before
        assert not list(r.path('mod').rglob('conflict_trace.json'))
        with pytest.raises(RuntimeError):b.execute('conflict-report',{**args,'game':'281990'})
    finally:b.shutdown();r.db.close()


def test_library_search_keeps_original_source_notes_and_tags(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'search-contract')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        pk=ids[0].mod_id
        info=r.db.get_mod_display_info(pk)
        r.db.update_mod_user_metadata(pk,{'display_name':'Visible alias','custom_description':info.custom_description,
            'user_notes':'Unique personal note','favorite':info.favorite})
        r.db.add_mod_tag(pk,'category','A user tag')
        snapshot=b.execute('library',{'game':1142710})
        asset=next(a for a in snapshot['assets'] if a['internal_id']==ids[0].internal_id)
        fields=asset['search_fields']
        assert 'Visible alias' in fields and 'Unique personal note' in fields
        assert any('A user tag' in value for value in fields)
        assert info.steam_name in fields and info.source_url in fields
        assert asset['workspace_id'] in fields
        assert all(isinstance(value,str) for value in fields)
    finally:b.shutdown();r.db.close()


def test_create_game_and_source_actions(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'library')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710})
        context={'game':'1142710','snapshot':snapshot['snapshot']}
        values={**context,'app_id':'1234567','name':'New managed game','deploy_type':'folder_copy','paths':{'install_path':'','mod_path':'','workshop_path':''}}
        assert b.execute_command('game-create',values)['game']=='1234567'
        assert r.db.get_game(1234567).name=='New managed game'
        created=b.execute('library',{'game':1234567})
        game_context={'game':'1234567','snapshot':created['snapshot']}
        config=b.execute('game-config',game_context)
        result=b.execute_command('game-config-save',{**game_context,'version':config['version'],'name':'Renamed game','deploy_type':'palworld_pak','paths':values['paths']})
        assert result['config']['name']=='Renamed game'
        assert r.db.get_game_deploy_config(1234567).deploy_type=='palworld_pak'
        refreshed=b.execute('library',{'game':1142710})
        values['snapshot']=refreshed['snapshot']
        with pytest.raises(ValueError,match='已存在'):b.execute_command('game-create',values)
        with pytest.raises(ValueError):b.execute_command('game-create',{**values,'app_id':'-7'})
        b.execute('library',{'game':1234567})
        with pytest.raises(RuntimeError):b.execute_command('game-create',{**values,'app_id':'1234568'})
        snapshot=b.execute('library',{'game':1142710})
        asset=b.adapter.assets(1142710)[0]
        from frontend.mod_operations import source_url
        assert source_url(b,asset).startswith('https://steamcommunity.com/')
        policy=b.inspector_actions(asset,'')
        menu={a['id']:a for a in policy['menu']}
        assert menu['open-source']['enabled'] and menu['cover']['enabled']
        assert not menu['open-offline']['enabled']
        assert menu['open-offline']['reason']=='未保存离线页面'
    finally:b.shutdown();r.db.close()


def test_type_catalog_uses_original_ids_and_keeps_category_tags_separate(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'types-library')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710})
        item=snapshot['assets'][0]
        context={'game':'1142710','snapshot':snapshot['snapshot'],'token':item['token'],'internal_id':item['internal_id']}
        asset=b._assets[item['token']]
        tags=r.db.get_category_tags(asset.mod_pk)
        category=r.db.get_mod_display_info(asset.mod_pk).category
        types=b.execute('mod-types',context)
        types=b.execute_command('type-create',{**context,'version':types['version'],'name':'User-defined type'})
        tid=types['types'][-1]['type_id']
        b.execute_command('type-set',{**context,'version':types['version'],'type_id':tid})
        assert r.db.get_mod_type_id(asset.mod_pk)==tid
        assert r.db.get_category_tags(asset.mod_pk)==tags
        assert r.db.get_mod_display_info(asset.mod_pk).category==category
        with pytest.raises(ValueError):b.execute_command('type-set',{**context,'version':types['version'],'type_id':999999})
        with pytest.raises(RuntimeError):b.execute_command('type-delete',{**context,'version':'stale','type_id':tid,'confirm':True})
        b.execute_command('type-delete',{**context,'version':types['version'],'type_id':tid,'confirm':True})
        assert r.db.get_mod_type_id(asset.mod_pk) is None
        assert r.db.get_mod_display_info(asset.mod_pk) is not None
    finally:b.shutdown();r.db.close()


def test_asset_management_original_services_and_stale_version(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'asset-management')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710});item=snapshot['assets'][0]
        context={'game':'1142710','snapshot':snapshot['snapshot'],'token':item['token'],'internal_id':item['internal_id']}
        asset=b._assets[item['token']]
        before=r.db.get_mod_deploy_info(asset.mod_pk).deploy_status
        state=b.execute('asset-management',context)
        target=next(a for a in b.adapter.assets(1142710) if a.internal_id!=asset.internal_id)
        state=b.execute_command('relation-add',{**context,'version':state['version'],'kind':'dependency','target':target.internal_id})
        assert len(state['relations']['dependencies'])==1
        rid=state['relations']['dependencies'][0]['id']
        state=b.execute_command('relation-remove',{**context,'version':state['version'],'relationship_id':rid})
        assert not state['relations']['dependencies']
        with pytest.raises(ValueError):b.execute_command('relation-add',{**context,'version':state['version'],'kind':'dependency','target':ids[2].internal_id})
        for field in ('invalid','conflict','abandoned'):
            state=b.execute_command('annotation-set',{**context,'version':state['version'],'field':field,'value':True})
            assert state['abandoned'] if field=='abandoned' else state['status']['invalid'] if field=='invalid' else state['status']['conflict_status']=='conflict'
            state=b.execute_command('annotation-set',{**context,'version':state['version'],'field':field,'value':False})
        from core.mod_platform import ModFileEntry,ModFilesBundle
        r.db.set_mod_files(asset.mod_pk,ModFilesBundle(files=[ModFileEntry.from_dict({'id':'file-one','filename':'one.pack','path':'one.pack','selected_for_deploy':True})]))
        state=b.execute('asset-management',context)
        state=b.execute_command('file-select',{**context,'version':state['version'],'file_id':'file-one','value':False})
        assert state['files']['files'][0]['selected_for_deploy'] is False
        with pytest.raises(RuntimeError):b.execute_command('file-select',{**context,'version':'stale','file_id':'file-one','value':True})
        assert r.db.get_mod_deploy_info(asset.mod_pk).deploy_status==before
    finally:b.shutdown();r.db.close()


def test_metadata_edit_persists_original_sidecar_and_rejects_unpicked_target(tmp_path):
    import json
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'editor-contract')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710});item=snapshot['assets'][0]
        context={'game':'1142710','snapshot':snapshot['snapshot'],'token':item['token'],'internal_id':item['internal_id']}
        asset=b._assets[item['token']];info=r.db.get_mod_display_info(asset.mod_pk)
        fields={'display_name':'User metadata','custom_description':'User description','user_notes':'Preserve notes','category':'Independent category','platform':info.platform,'source_url':info.source_url or '', 'custom_deploy_path':''}
        assert b.execute_command('metadata-save',{**context,'fields':fields})['success']
        data=json.loads((__import__('pathlib').Path(asset.managed_path)/'.info/metadata.json').read_text('utf-8'))
        assert data['display_name']=='User metadata'
        assert data['internal_id']==asset.internal_id
        with pytest.raises(ValueError):b.execute_command('metadata-save',{**context,'fields':{**fields,'source_url':'javascript:alert(1)'}})
        with pytest.raises(ValueError):b.execute_command('metadata-save',{**context,'fields':{**fields,'game_version':'original'}})
        with pytest.raises(ValueError):b.execute_command('metadata-save',{**context,'fields':{**fields,'custom_deploy_path':str(tmp_path)}})
        assert r.db.get_mod_display_info(asset.mod_pk).custom_deploy_path==''
    finally:b.shutdown();r.db.close()


def test_batch_edits_and_collection_cover_order_use_original_services(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'batch-contract');b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1142710})
        context={'game':'1142710','snapshot':snapshot['snapshot'],'internal_ids':[a['internal_id'] for a in snapshot['assets']]}
        state=b.execute('batch-management',context)
        first=b.execute_command('collection-create',{**context,'name':'First collection'})['collection_id']
        second=b.execute_command('collection-create',{**context,'name':'Second collection'})['collection_id']
        with pytest.raises(RuntimeError):b.execute_command('batch-memberships',{**context,'version':state['version'],'add':[first],'remove':[]})
        state=b.execute('batch-management',context)
        b.execute_command('batch-memberships',{**context,'version':state['version'],'add':[first],'remove':[]})
        assert len(r.db.list_collection_member_ids(first))==2
        state=b.execute('batch-management',context)
        b.execute_command('batch-platform-set',{**context,'version':state['version'],'platform':'Other'})
        assert all(r.db.get_mod_display_info(identity.mod_id).platform.lower()=='other' for identity in ids[:2])
        first_record=r.db.get_collection(first)
        b.execute_command('collection-cover-member',{**context,'collection_id':first,'version':b.collection_version(first_record),'internal_id':ids[0].internal_id})
        first_record=r.db.get_collection(first)
        assert b.collection_cover_preview(first_record).startswith('data:image/jpeg;base64,')
        records=r.db.list_collections(1142710)
        b.execute_command('collection-move',{**context,'source':second,'target':first,'versions':[[str(c.collection_id),b.collection_version(c)] for c in records]})
        assert str(r.db.list_collections(1142710)[0].collection_id)==second
        with pytest.raises(ValueError):b.execute('batch-management',{**context,'internal_ids':[ids[2].internal_id]})
    finally:b.shutdown();r.db.close()


def test_first_game_creation_does_not_require_an_existing_game(tmp_path):
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources
    from services.mutation_context import activate_test_context,reset_mutation_context
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'empty';(root/'data').mkdir(parents=True)
    db=DatabaseManager(root/'data/mod_manager.db');token=activate_test_context([root])
    b=None
    try:
        r=BusinessResources(root,db);b=LibraryBridge(LibraryAdapter(db,writable=True),resources=r)
        assert all(g.key=='0' for g in b.adapter.games())
        empty=b.execute('library',{'game':0})
        result=b.execute_command('game-create',{'game':'0','snapshot':empty['snapshot'],'app_id':'123456','name':'First game','deploy_type':'folder_copy','paths':{'install_path':'','mod_path':'','workshop_path':''}})
        assert result['game']=='123456' and db.get_game(123456).name=='First game'
        with pytest.raises(RuntimeError):b.execute_command('game-create',{'game':'','snapshot':0,'app_id':'123457','name':'Stale empty context','paths':{'install_path':'','mod_path':'','workshop_path':''}})
    finally:
        if b:b.shutdown()
        db.close();reset_mutation_context(token)


def test_settings_save_does_not_probe_system_processes_inside_write_scope(tmp_path,monkeypatch):
    from PySide6.QtCore import QSettings
    from frontend import app_settings
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'settings-library')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    def settings_factory(*a):return settings
    settings_factory.NoError=QSettings.NoError
    monkeypatch.setattr(app_settings,'QSettings',settings_factory)
    monkeypatch.setattr('services.proxy_resolver.resolve_proxy',lambda **kw:(_ for _ in ()).throw(AssertionError('No system probe during save')))
    cleared=[]
    monkeypatch.setattr('services.proxy_resolver.clear_proxy_cache',lambda:cleared.append(True))
    try:
        snap=b.execute('library',{'game':1142710})
        result=b.execute_command('settings-save',{'game':'1142710','snapshot':snap['snapshot'],'proxy_mode':'auto','proxy_url':''})
        assert result['success'] and settings.value('network/proxy_mode')=='auto' and cleared
        assert result['library']==str(r.path('mod'))
    finally:b.shutdown();r.db.close()

