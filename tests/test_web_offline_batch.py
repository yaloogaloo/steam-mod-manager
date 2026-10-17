"""Real browser samples, ImportWorker and persistent offline/CAS registration."""
import json
import time
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_nexus_sandbox import initialize


@pytest.fixture
def batch(tmp_path):
    app=QApplication.instance() or QApplication([]);r=initialize(tmp_path/'nexus')
    bridge=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    snapshot=bridge.execute('library',{'game':413150});context={'game':'413150','snapshot':snapshot['snapshot']}
    yield app,r,bridge,context
    while not bridge.shutdown():app.processEvents();time.sleep(.01)
    r.db.close()


def submit(app,b,context,preview,ids):
    replies=[];b.response.connect(lambda raw:replies.append(json.loads(raw)))
    request=str(time.monotonic_ns());b.command(request,'offline-register',json.dumps({**context,'preview':preview,'ids':ids}))
    deadline=time.monotonic()+30
    while not any(m['request']==request for m in replies):
        app.processEvents();time.sleep(.01);assert time.monotonic()<deadline,replies
    return next(m for m in replies if m['request']==request)


def test_real_html_mhtml_batch_and_repeat(batch):
    app,r,b,context=batch
    paths=[str(p) for p in r.path('incoming').iterdir() if p.is_file()]
    preview=b.execute('offline-preview',{**context,'paths':paths})
    assert sum(row['state']=='new' for row in preview['rows'])==3,preview
    assert sum(row['state']=='repeated' for row in preview['rows'])==1
    assert sum(row['state']=='failed' for row in preview['rows'])==3
    ids=[row['id'] for row in preview['rows'] if row['state']=='new']
    repeated=next(row['id'] for row in preview['rows'] if row['state']=='repeated')
    assert 'error' in submit(app,b,context,preview['preview'],[ids[0],repeated])
    ids[0]=repeated  # Explicitly choose the alternative snapshot for the same official identity.
    response=submit(app,b,context,preview['preview'],ids)
    assert 'result' in response,response
    assert [i['state'] for i in response['result']['items']]==['success']*3,response
    assets=b.adapter.assets(413150);assert len(assets)==3
    for asset in assets:
        assert (Path(asset.managed_path)/'.info/offline/index.html').is_file()
        assert (Path(asset.managed_path)/'.info/offline/manifest.json').is_file()
    before={a.internal_id:a.mod_pk for a in assets}
    again=b.execute('offline-preview',{**context,'paths':paths})
    result=submit(app,b,context,again['preview'],[row['id'] for row in again['rows'] if row['state']=='existing'])
    assert all(i['state']=='skipped' for i in result['result']['items']),result
    assert {a.internal_id:a.mod_pk for a in b.adapter.assets(413150)}==before


def test_item_changed_failure_keeps_other_success(batch):
    app,r,b,context=batch
    paths=[str(p) for p in sorted(r.path('incoming').glob('0[12]-*.mhtml'))]
    preview=b.execute('offline-preview',{**context,'paths':paths})
    Path(paths[0]).write_bytes(b'Changed after preview')
    result=submit(app,b,context,preview['preview'],[row['id'] for row in preview['rows']])['result']
    assert [i['state'] for i in result['items']]==['failed','success'],result
    assert len(b.adapter.assets(413150))==1


def test_archive_failure_compensates_only_owned_item(batch,monkeypatch):
    import ui.import_thread as module
    app,r,b,context=batch
    original=module.attach_nexus_offline_page;calls=[]
    def fail_once(*args,**kwargs):
        calls.append(args[0])
        if len(calls)==1:raise OSError('Injected offline archive failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'attach_nexus_offline_page',fail_once)
    paths=[str(p) for p in sorted(r.path('incoming').glob('0[12]-*.mhtml'))]
    preview=b.execute('offline-preview',{**context,'paths':paths})
    result=submit(app,b,context,preview['preview'],[row['id'] for row in preview['rows']])['result']
    assert [i['state'] for i in result['items']]==['failed','success'],result
    assert len(b.adapter.assets(413150))==1
    assert list(r.path('cache/offline_failed').iterdir())
    assert all(Path(p).is_file() for p in paths)


def test_descriptor_boundary_and_readonly(batch):
    import os
    from core.db_manager import DatabaseManager
    app,r,b,context=batch
    own=r.path('cache/own-fd');own.parent.mkdir(exist_ok=True)
    descriptor=os.open(own,os.O_CREAT|os.O_RDWR)
    try:
        with r.bind():
            with open(descriptor,'wb',closefd=False) as target:target.write(b'isolated')
    finally:os.close(descriptor)
    assert own.read_bytes()==b'isolated'
    outside=r.root.parent/'outside-fd';descriptor=os.open(outside,os.O_CREAT|os.O_RDWR)
    try:
        with pytest.raises(PermissionError):
            with r.bind():open(descriptor,'wb',closefd=False)
    finally:os.close(descriptor)
    assert outside.read_bytes()==b''
    rodb=DatabaseManager.open_existing(r.db.db_path,read_only=True)
    readonly=LibraryBridge(LibraryAdapter(rodb,writable=False));replies=[]
    readonly.response.connect(lambda raw:replies.append(json.loads(raw)))
    readonly.command('readonly','offline-register',json.dumps(context))
    assert replies and 'error' in replies[0]
    readonly.shutdown();rodb.close()


def test_concurrent_duplicate_does_not_attach_or_overwrite(batch,monkeypatch):
    from ui.import_thread import ImportWorker
    from services.importers.nexus import NexusImporter
    app,r,b,context=batch
    path=next(r.path('incoming').glob('01-*.mhtml'))
    preview=b.execute('offline-preview',{**context,'paths':[str(path)]})
    page=next(iter(b._offline_previews[preview['preview']]['expected'].values()))
    original=ImportWorker._import_one_folder
    created=[]
    def race(worker,folder,**kwargs):
        result=NexusImporter(db=r.db).import_mod(source_folder=folder,title='Concurrent Mod',nexus_url=page['url'],
            nexus_id=page['external_id'],app_id=413150,game_name='Stardew Valley',library_root=r.path('mod'))
        assert result.success
        r.db.update_mod_user_metadata(result.mod_id,{'display_name':'Concurrent user name','custom_description':'Keep me','user_notes':'Keep notes','favorite':True})
        created.append(result)
        return original(worker,folder,**kwargs)
    monkeypatch.setattr(ImportWorker,'_import_one_folder',race)
    result=submit(app,b,context,preview['preview'],['0'])['result']
    assert result['items'][0]['state']=='skipped',result
    assert len(b.adapter.assets(413150))==1
    assert not (Path(created[0].managed_path)/'.info/offline').exists()
    info=r.db.get_mod_display_info(created[0].mod_id)
    assert info.user_display_name=='Concurrent user name' and info.custom_description=='Keep me' and info.favorite


def test_batch_game_switch_duplicate_and_window_close(batch,monkeypatch):
    import threading
    from PySide6.QtTest import QTest
    from frontend.web_window import WebLibraryWindow
    from ui.import_thread import ImportWorker
    from services.business_resources import current_resources
    app,r,unused,unused_context=batch
    window=WebLibraryWindow(LibraryAdapter(r.db,writable=True),resources=r)
    window.page.setHtml('<html><body>Batch worker lifecycle</body></html>');window.show()
    QTest.qWait(1000);b=window.bridge
    snapshot=b.execute('library',{'game':413150});context={'game':'413150','snapshot':snapshot['snapshot']}
    paths=[str(p) for p in sorted(r.path('incoming').glob('0[12]-*.mhtml'))]
    preview=b.execute('offline-preview',{**context,'paths':paths})
    entered,release=threading.Event(),threading.Event();observed=[]
    original=ImportWorker._import_one_folder
    def gated(worker,*args,**kwargs):
        observed.append(current_resources() is r);entered.set()
        if not release.wait(15):raise TimeoutError('test gate')
        return original(worker,*args,**kwargs)
    monkeypatch.setattr(ImportWorker,'_import_one_folder',gated)
    payload=json.dumps({**context,'preview':preview['preview'],'ids':[row['id'] for row in preview['rows']]})
    replies=[];b.response.connect(lambda raw:replies.append(json.loads(raw)))
    deadline=time.monotonic()+35
    try:
        b.command('first','offline-register',payload)
        while not entered.is_set():QTest.qWait(10);time.sleep(.01);assert time.monotonic()<deadline,replies
        b.command('duplicate','offline-register',payload)
        assert any(m['request']=='duplicate' and 'error' in m for m in replies)
        b.execute('library',{'game':43})
        b.command('stale','offline-register',payload)
        assert any(m['request']=='stale' and 'error' in m for m in replies)
        window.close();assert window.isVisible() and b._jobs
        release.set()
        while window.isVisible() or b._jobs:QTest.qWait(20);time.sleep(.01);assert time.monotonic()<deadline,replies
        assert len(b.adapter.assets(413150))==2 and all(observed)
        assert not b.adapter.assets(43)
    finally:
        release.set()
        while not b.shutdown():QTest.qWait(20);time.sleep(.01)
        window.close();window.deleteLater();QTest.qWait(50)
