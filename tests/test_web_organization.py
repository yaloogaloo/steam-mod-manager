"""Organisation commands, real SQLite failures and collection cover ownership."""
import json
import os
import subprocess
from threading import Event
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRunnable
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge, ORGANIZE_ACTIONS
from services import collection
from tools.organization_validation import initialize, protected_state


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def organization(tmp_path,qapp):
    resources, identities=initialize(tmp_path/'organization')
    bridge=LibraryBridge(LibraryAdapter(resources.db,writable=True),resources=resources)
    yield bridge, resources, identities
    assert bridge.shutdown()
    resources.db.close()


def context(bridge,game=1142710):
    snapshot=bridge.execute('library',{'game':game})
    return {'game':str(game),'snapshot':snapshot['snapshot']},snapshot


def create(bridge):
    args,_=context(bridge)
    cid=bridge.execute_command('collection-create',{**args,'name':'Daily'})['collection_id']
    return current(bridge,cid)


def current(bridge,cid):
    args,snapshot=context(bridge)
    record=next(c for c in snapshot['collections'] if c['id']==str(cid))
    return {**args,'collection_id':str(cid),'version':record['version']}


def install_cover(bridge,resources,identities,cid):
    source=Path(resources.db.get_mod_backup_row(identities[0].mod_id)['last_known_path'])/'.info/user-cover.png'
    with resources.bind():
        collection.set_collection_cover(cid,source,db=resources.db)
    return resources.path(f'data/collection_covers/{cid}.png')


def test_tags_real_dal_duplicate_validation_and_failure(organization):
    bridge,resources,identities=organization
    before=protected_state(resources)
    args,snapshot=context(bridge)
    asset=next(a for a in snapshot['assets'] if a['internal_id']==identities[0].internal_id)
    args.update(token=asset['token'],internal_id=asset['internal_id'])
    state=bridge.execute('tags',args)
    assert 'Graphics' in state['available']
    added=bridge.execute_command('tag-add',{**args,'version':state['version'],'label':'Gameplay'})
    assert resources.db.get_category_tags(identities[0].mod_id)==['Gameplay']
    assert bridge.execute_command('tag-add',{**args,'version':added['version'],'label':'Gameplay'})['changed']==0
    with pytest.raises(RuntimeError,match='标签已经变化'):
        bridge.execute_command('tag-remove',{**args,'version':state['version'],'label':'Gameplay'})
    for invalid in ('','x'*81,'bad\nlabel'):
        with pytest.raises(ValueError):
            bridge.execute_command('tag-add',{**args,'version':added['version'],'label':invalid})
    resources.db._conn.execute("CREATE TEMP TRIGGER reject_tag BEFORE INSERT ON mod_tags BEGIN SELECT RAISE(ABORT,'tag write refused'); END")
    with pytest.raises(Exception,match='tag write refused'):
        bridge.execute_command('tag-add',{**args,'version':added['version'],'label':'NoWrite'})
    resources.db._conn.execute('DROP TRIGGER reject_tag')
    bridge.execute_command('tag-remove',{**args,'version':added['version'],'label':'Gameplay'})
    assert resources.db.get_category_tags(identities[0].mod_id)==[]
    assert protected_state(resources)==before


def test_collection_crud_game_boundary_and_versions(organization):
    bridge,resources,identities=organization
    before=protected_state(resources)
    args=create(bridge)
    cid=args['collection_id']
    with pytest.raises(ValueError,match='其他游戏'):
        bridge.execute_command('collection-add',{**args,'internal_ids':[identities[2].internal_id]})
    bridge.execute_command('collection-add',{**args,'internal_ids':[i.internal_id for i in identities[:2]]})
    assert len(resources.db.list_collection_member_ids(cid))==2
    with pytest.raises(RuntimeError,match='合集已经变化'):
        bridge.execute_command('collection-delete',{**args,'confirm':True})
    args=current(bridge,cid)
    bridge.execute_command('collection-remove',{**args,'internal_ids':[identities[1].internal_id]})
    args=current(bridge,cid)
    bridge.execute_command('collection-rename',{**args,'name':'Renamed'})
    args=current(bridge,cid)
    assert resources.db.get_collection(cid).name=='Renamed'
    with pytest.raises(ValueError,match='确认'):
        bridge.execute_command('collection-delete',args)
    bridge.execute_command('collection-delete',{**args,'confirm':True})
    assert resources.db.get_collection(cid) is None
    assert protected_state(resources)==before


def test_collection_delete_db_error_restores_cover_and_members(organization):
    bridge,resources,identities=organization
    args=create(bridge)
    cid=args['collection_id']
    bridge.execute_command('collection-add',{**args,'internal_ids':[identities[0].internal_id]})
    cover=install_cover(bridge,resources,identities,cid)
    original=cover.read_bytes()
    args=current(bridge,cid)
    resources.db._conn.execute("CREATE TEMP TRIGGER reject_delete BEFORE DELETE ON collections BEGIN SELECT RAISE(ABORT,'delete write refused'); END")
    with pytest.raises(Exception,match='delete write refused'):
        bridge.execute_command('collection-delete',{**args,'confirm':True})
    assert cover.read_bytes()==original
    assert resources.db.get_collection(cid) is not None
    assert resources.db.list_collection_member_ids(cid)==[str(identities[0].mod_id)]
    resources.db._conn.execute('DROP TRIGGER reject_delete')
    bridge.execute_command('collection-delete',{**args,'confirm':True})
    assert not cover.exists()


def test_cover_cleanup_failure_preserves_collection(organization,monkeypatch):
    bridge,resources,identities=organization
    cid=create(bridge)['collection_id']
    cover=install_cover(bridge,resources,identities,cid)
    original=Path.unlink
    def denied(path,*args,**kwargs):
        if path==cover:raise PermissionError('collection cover is locked')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',denied)
    with pytest.raises(PermissionError,match='locked'):
        bridge.execute_command('collection-delete',{**current(bridge,cid),'confirm':True})
    assert resources.db.get_collection(cid) is not None and cover.exists()


def test_foreign_cover_never_deleted(organization):
    bridge,resources,identities=organization
    cid=create(bridge)['collection_id']
    foreign=resources.path('data/asset_store/not-owned.png')
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b'PRESERVE')
    resources.db.update_collection_cover_path(cid,'asset_store/not-owned.png')
    with pytest.raises(ValueError,match='不属于当前合集'):
        bridge.execute_command('collection-delete',{**current(bridge,cid),'confirm':True})
    assert foreign.read_bytes()==b'PRESERVE' and resources.db.get_collection(cid) is not None
    source=Path(resources.db.get_mod_backup_row(identities[0].mod_id)['last_known_path'])/'.info/user-cover.png'
    with resources.bind(), pytest.raises(ValueError,match='不属于当前合集'):
        collection.set_collection_cover(cid,source,db=resources.db)
    assert foreign.read_bytes()==b'PRESERVE'


def test_collection_cover_junction_refused(organization,tmp_path):
    bridge,resources,identities=organization
    cid=create(bridge)['collection_id']
    external=tmp_path/'foreign-covers'
    external.mkdir()
    marker=external/f'{cid}.png'
    marker.write_bytes(b'FOREIGN')
    link=resources.path('data/collection_covers')
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(external)],capture_output=True)
        assert result.returncode==0,result.stderr
    else:link.symlink_to(external,target_is_directory=True)
    with pytest.raises(PermissionError):
        bridge.execute_command('collection-delete',{**current(bridge,cid),'confirm':True})
    assert marker.read_bytes()==b'FOREIGN' and resources.db.get_collection(cid) is not None


def test_queued_game_change_duplicate_close_and_readonly(organization):
    bridge,resources,identities=organization
    entered,release=Event(),Event()
    class Blocker(QRunnable):
        def run(self):entered.set();release.wait(5)
    bridge.pool.setMaxThreadCount(1)
    bridge.pool.start(Blocker())
    assert entered.wait(2)
    args,_=context(bridge)
    messages=[]
    bridge.response.connect(lambda raw:messages.append(json.loads(raw)))
    bridge.command('create','collection-create',json.dumps({**args,'name':'Queued'}))
    bridge.command('duplicate','collection-create',json.dumps({**args,'name':'Queued'}))
    assert any(m['request']=='duplicate' and 'error' in m for m in messages)
    context(bridge,43)
    release.set()
    assert bridge.pool.waitForDone(5000)
    QApplication.instance().processEvents()
    assert any(m['request']=='create' and 'error' in m for m in messages)
    assert not resources.db.list_collections(1142710)
    ro=DatabaseManager.open_existing(resources.db.db_path)
    readonly=LibraryBridge(LibraryAdapter(ro))
    received=[]
    readonly.response.connect(lambda raw:received.append(json.loads(raw)))
    try:
        for action in ORGANIZE_ACTIONS:readonly.command(action,action,json.dumps(args))
        assert len(received)==len(ORGANIZE_ACTIONS) and all('error' in m for m in received)
        assert not readonly._busy
    finally:
        assert readonly.shutdown()
        ro.close()


def test_invalid_mod_cannot_create_stub(organization):
    bridge,resources,_=organization
    args,_=context(bridge)
    count=len(resources.db.list_mod_list_items(game_id=1142710))
    with pytest.raises(RuntimeError):
        bridge.execute_command('tag-add',{**args,'token':'unknown','internal_id':'bad','version':'','label':'Tag'})
    assert len(resources.db.list_mod_list_items(game_id=1142710))==count


def test_collection_cover_symlink_refused(organization):
    bridge,resources,identities=organization
    cid=create(bridge)['collection_id']
    original=Path(resources.db.get_mod_backup_row(identities[0].mod_id)['last_known_path'])/'.info/user-cover.png'
    cover=resources.path(f'data/collection_covers/{cid}.png')
    cover.parent.mkdir(parents=True)
    try:
        cover.symlink_to(original)
    except OSError as exc:
        if getattr(exc,'winerror',None)==1314:pytest.skip('Windows 未授予创建符号链接权限')
        raise
    before=original.read_bytes()
    with pytest.raises(PermissionError):
        bridge.execute_command('collection-delete',{**current(bridge,cid),'confirm':True})
    assert original.read_bytes()==before and resources.db.get_collection(cid) is not None
