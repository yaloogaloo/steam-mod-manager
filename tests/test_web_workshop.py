"""Real SyncWorker, HTTP source, isolation and identity/partial-result boundaries."""
import json
import time
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_workshop_sandbox import initialize,source_server

@pytest.fixture
def setup(tmp_path):
    app=QApplication.instance() or QApplication([])
    r=initialize(tmp_path/'workshop');r,server,requests=source_server(r)
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    snap=b.execute('library',{'game':1142710});a={'game':'1142710','snapshot':snap['snapshot']}
    yield app,r,b,a,requests
    while not b.shutdown():app.processEvents();time.sleep(.01)
    r.db.close();server.shutdown();server.server_close()


def run(app,b,args,ids):
    preview=b.execute('workshop-scan',args)
    messages=[];b.response.connect(lambda raw:messages.append(json.loads(raw)))
    rid='sync-'+str(time.monotonic_ns())
    b.command(rid,'workshop-sync',json.dumps({**args,'scan':preview['scan'],'ids':ids}))
    deadline=time.monotonic()+20
    while not any(m['request']==rid for m in messages):
        app.processEvents();time.sleep(.01)
        assert time.monotonic()<deadline,messages
    return next(m for m in messages if m['request']==rid)


def test_real_batch_registration_and_idempotency(setup):
    app,r,b,a,requests=setup
    preview=b.execute('workshop-scan',a)
    assert any(row['status']=='protected' for row in preview['rows'])
    assert any(row['status']=='source_missing' for row in preview['rows'])
    assert any(row['status']=='invalid' for row in preview['rows'])
    response=run(app,b,a,['940001','940002'])
    assert 'result' in response,response
    result=response['result'];assert result['state']=='success',result
    assert len(result['items'])==2 and all(i['state']=='success' for i in result['items'])
    identities={a.workspace_id:(a.mod_pk,a.internal_id,a.managed_path) for a in b.adapter.assets(1142710) if a.workspace_id in ('940001','940002')}
    for wid,(pk,iid,path) in identities.items():
        from pathlib import Path
        assert (Path(path)/'payload.pack').read_bytes()==('WORKSHOP CONTENT '+wid).encode()
    repeat=run(app,b,a,['940001','940002'])['result']
    assert all(i['state']=='skipped' for i in repeat['items']),repeat
    assert identities=={a.workspace_id:(a.mod_pk,a.internal_id,a.managed_path) for a in b.adapter.assets(1142710) if a.workspace_id in identities}
    assert requests


def test_partial_failure_and_missing_content_replenish(setup):
    app,r,b,a,requests=setup
    result=run(app,b,a,['940001','940003'])['result']
    assert result['state']=='partial' and [i['state'] for i in result['items']]==['success','failed'],result
    stub=next(asset for asset in b.adapter.assets(1142710) if asset.workspace_id=='940010')
    source=r.path('games/steamapps/workshop/content/1142710/940010');source.mkdir();(source/'new.pack').write_bytes(b'REPLENISHED')
    filled=run(app,b,a,['940010'])['result'];assert filled['state']=='success',filled
    after=next(asset for asset in b.adapter.assets(1142710) if asset.workspace_id=='940010')
    assert (after.mod_pk,after.internal_id)==(stub.mod_pk,stub.internal_id)
    from pathlib import Path
    assert (Path(after.managed_path)/'new.pack').read_bytes()==b'REPLENISHED'

def test_real_worker_close_and_duplicate_submission(setup, monkeypatch):
    import threading
    from PySide6.QtTest import QTest
    from frontend.web_window import WebLibraryWindow
    from services.sync import ModSyncService
    app,r,b,a,requests=setup
    window=WebLibraryWindow(LibraryAdapter(r.db,writable=True),resources=r)
    window.page.setHtml("<html><body>Worker lifecycle regression</body></html>");window.show()
    QTest.qWait(1000)
    bridge=window.bridge
    snapshot=bridge.execute('library',{'game':1142710})
    args={'game':'1142710','snapshot':snapshot['snapshot']}
    preview=bridge.execute('workshop-scan',args)
    entered,release=threading.Event(),threading.Event()
    original=ModSyncService._copy_only
    def gated(service,*values):
        entered.set()
        if not release.wait(10):raise TimeoutError('test release timeout')
        return original(service,*values)
    monkeypatch.setattr(ModSyncService,'_copy_only',gated)
    replies=[];bridge.response.connect(lambda raw:replies.append(json.loads(raw)))
    payload=json.dumps({**args,'scan':preview['scan'],'ids':['940001']})
    try:
        bridge.command('first','workshop-sync',payload)
        deadline=time.monotonic()+10
        while not entered.is_set():
            QTest.qWait(10);time.sleep(.01)
            assert time.monotonic()<deadline,replies
        bridge.command('duplicate','workshop-sync',payload)
        QTest.qWait(30)
        assert any(m['request']=='duplicate' and 'error' in m for m in replies),replies
        window.close()
        assert window.isVisible() and bridge._jobs
        release.set()
        while window.isVisible() or bridge._jobs:
            QTest.qWait(20);time.sleep(.01)
            assert time.monotonic()<deadline,replies
        asset=next(asset for asset in bridge.adapter.assets(1142710) if asset.workspace_id=='940001')
        from pathlib import Path
        assert (Path(asset.managed_path)/'payload.pack').read_bytes()==b'WORKSHOP CONTENT 940001'
    finally:
        release.set()
        while not bridge.shutdown():QTest.qWait(20);time.sleep(.01)
        window.close();window.deleteLater();QTest.qWait(50)


def test_readonly_stale_and_missing_selected_source(setup):
    app,r,b,a,requests=setup
    from core.db_manager import DatabaseManager
    rodb=DatabaseManager.open_existing(r.db.db_path,read_only=True)
    readonly=LibraryBridge(LibraryAdapter(rodb,writable=False),resources=None)
    snapshot=readonly.execute('library',{'game':1142710})
    messages=[];readonly.response.connect(lambda raw:messages.append(json.loads(raw)))
    readonly.command('readonly','workshop-sync',json.dumps({'game':'1142710','snapshot':snapshot['snapshot'],'scan':'x','ids':['940001']}))
    assert messages and 'error' in messages[0]
    readonly.shutdown();rodb.close()
    preview=b.execute('workshop-scan',a)
    # Remove one input file, preserving directories; never remove a source through the business service.
    (r.path('games/steamapps/workshop/content/1142710/940002/payload.pack')).unlink()
    # Empty inputs fail preflight rather than entering destructive copying.
    messages=[];b.response.connect(lambda raw:messages.append(json.loads(raw)))
    b.command('changed','workshop-sync',json.dumps({**a,'scan':preview['scan'],'ids':['940002']}))
    deadline=time.monotonic()+10
    while not any(m['request']=='changed' for m in messages):
        app.processEvents();time.sleep(.01);assert time.monotonic()<deadline
    assert 'error' in next(m for m in messages if m['request']=='changed')
    assert not any(asset.workspace_id=='940002' for asset in b.adapter.assets(1142710))
