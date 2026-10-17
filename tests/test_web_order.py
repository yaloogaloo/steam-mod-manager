"""Authoritative WH3 order, projection failures, optimistic version and readonly."""
import json
import time
import os
import stat
import pytest
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_order_sandbox import initialize


@pytest.fixture
def fixture(tmp_path):
    app=QApplication.instance() or QApplication([])
    resources=initialize(tmp_path/'order')
    bridge=LibraryBridge(LibraryAdapter(resources.db,writable=True),resources=resources)
    snapshot=bridge.execute('library',{'game':1142710})
    args={'game':'1142710','snapshot':snapshot['snapshot']}
    yield app,resources,bridge,args
    bridge.shutdown();resources.db.close()


def test_wh3_order_and_missing_external_members(fixture):
    app,r,b,args=fixture
    before=b.execute('deployment',args)
    assert len(before['rows'])==2
    desired=before['order'][::-1]
    result=b.execute_command('order-save',{**args,'version':before['version'],'order':desired})
    assert result['success']
    assert json.loads(r.path('config/load_order/wh3.json').read_text('utf8'))['order']==desired
    text=r.path('games/Warhammer3/used_mods.txt').read_text('utf8')
    assert text.index('mod "campaign.pack"')<text.index('mod "native.pack"')
    missing=r.path('games/steamapps/workshop/content/1142710/902100/native.pack');missing.rename(missing.with_suffix('.missing'))
    state=b.execute('deployment',args)
    assert len(state['rows'])==2 and any(row['warning'] for row in state['rows'])
    assert state['order']==desired
    result=b.execute_command('order-save',{**args,'version':state['version'],'order':desired[::-1]})
    assert '成员警告' in result['message']
    assert len(json.loads(r.path('config/load_order/wh3.json').read_text('utf8'))['order'])==2


def test_version_membership_cross_game_and_no_backend(fixture):
    app,r,b,args=fixture
    current=b.execute('deployment',args)
    with pytest.raises(ValueError):b.execute_command('order-save',{**args,'version':current['version'],'order':current['order'][:1]})
    with pytest.raises(ValueError):b.execute_command('order-save',{**args,'version':current['version'],'order':[current['order'][0]]*2})
    b.execute_command('order-save',{**args,'version':current['version'],'order':current['order'][::-1]})
    with pytest.raises(RuntimeError,match='变化'):b.execute_command('order-save',{**args,'version':current['version'],'order':current['order']})
    snapshot=b.execute('library',{'game':43})
    with pytest.raises(RuntimeError):b.execute_command('order-save',{**args,'version':current['version'],'order':current['order']})
    unsupported=b.execute('deployment',{'game':'43','snapshot':snapshot['snapshot']})
    assert not unsupported['enabled'] and unsupported['backend'] is None


def test_json_failure_and_partial_projection_failure(fixture):
    app,r,b,args=fixture
    order=r.path('config/load_order/wh3.json');target=r.path('games/Warhammer3/used_mods.txt')
    current=b.execute('deployment',args);old=order.read_bytes()
    os.chmod(order,stat.S_IREAD)
    try:
        with pytest.raises(RuntimeError,match='排序保存失败'):b.execute_command('order-save',{**args,'version':current['version'],'order':current['order'][::-1]})
        assert order.read_bytes()==old
    finally:os.chmod(order,stat.S_IREAD|stat.S_IWRITE)
    current=b.execute('deployment',args);text=target.read_bytes()
    os.chmod(target,stat.S_IREAD)
    try:
        with pytest.raises(RuntimeError,match='投影失败'):b.execute_command('order-save',{**args,'version':current['version'],'order':current['order'][::-1]})
        assert target.read_bytes()==text
        assert json.loads(order.read_text('utf8'))['order']==current['order'][::-1]
        assert b.execute('deployment',args)['retry_projection']
    finally:os.chmod(target,stat.S_IREAD|stat.S_IWRITE)
    current=b.execute('deployment',args)
    assert b.execute_command('order-retry',{**args,'version':current['version']})['success']
    assert not b.execute('deployment',args)['projection_error']


def test_readonly_and_duplicate_requests(fixture):
    app,r,b,args=fixture
    rodb=DatabaseManager.open_existing(r.db.db_path,read_only=True);ro=LibraryBridge(LibraryAdapter(rodb))
    snapshot=ro.execute('library',{'game':1142710})
    readonly_state=ro.execute('deployment',{'game':'1142710','snapshot':snapshot['snapshot']})
    assert not readonly_state['enabled'] and len(readonly_state['rows'])==2
    assert readonly_state['order_path']==str(r.path('config/load_order/wh3.json'))
    messages=[];ro.response.connect(lambda raw:messages.append(json.loads(raw)))
    ro.command('readonly','order-save',json.dumps(args));assert messages[-1]['error']
    ro.shutdown();rodb.close()
    state=b.execute('deployment',args);responses=[]
    b.response.connect(lambda raw:responses.append(json.loads(raw)))
    payload=json.dumps({**args,'version':state['version'],'order':state['order'][::-1]})
    b.command('first','order-save',payload);b.command('duplicate','order-save',payload)
    deadline=time.monotonic()+10
    while len(responses)<2:
        app.processEvents();time.sleep(.01);assert time.monotonic()<deadline
    assert any(m.get('error') for m in responses)
    assert sum(bool(m.get('result',{}).get('success')) for m in responses)==1
    assert b.execute('deployment',args)['order']==state['order'][::-1]


def test_legacy_order_read_does_not_mutate_and_backend_migrates_on_write(fixture):
    app,r,b,args=fixture
    current=b.execute('deployment',args)
    ids=current['order'][::-1]
    pks=[r.db.find_mod_by_internal_id(iid) for iid in ids]
    r.path('config/load_order/wh3.json').unlink()
    legacy=r.path('data/wh3_load_order.json')
    legacy.write_text(json.dumps({'order':pks}),encoding='utf8')
    state=b.execute('deployment',args)
    assert state['order']==ids and legacy.is_file()
    assert not r.path('config/load_order/wh3.json').exists()
    b.execute_command('order-save',{**args,'version':state['version'],'order':ids})
    assert json.loads(r.path('config/load_order/wh3.json').read_text('utf8'))['order']==ids
    assert not legacy.exists()


def test_disabled_member_remains_visible_but_backend_skips_projection(fixture):
    app,r,b,args=fixture
    current=b.execute('deployment',args)
    iid=current['order'][0]
    with r.bind():r.db.disable_mod(r.db.find_mod_by_internal_id(iid))
    state=b.execute('deployment',args)
    assert len(state['rows'])==2 and any('未启用' in row['warning'] for row in state['rows'])
    result=b.execute_command('order-save',{**args,'version':state['version'],'order':state['order']})
    assert result['success'] and '成员警告' in result['message']
    assert 'mod "native.pack"' not in r.path('games/Warhammer3/used_mods.txt').read_text('utf8')


def test_order_does_not_require_new_deployment_admission(fixture,monkeypatch):
    _,r,b,args=fixture
    monkeypatch.setattr(type(r),'validate_deploy',lambda *a,**kw:(_ for _ in ()).throw(PermissionError('unapproved deployment strategy')))
    current=b.execute('deployment',args)
    assert current['enabled']
    assert b.execute_command('order-save',{**args,'version':current['version'],'order':current['order'][::-1]})['success']
