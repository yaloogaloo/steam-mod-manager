"""Real WebEngine snapshot CRUD verification in the admitted WH3 sandbox."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def initialize(root):
    from tools.web_order_sandbox import initialize as order_initialize
    from services.deployment_record import create_or_update_record
    r=order_initialize(root)
    with r.bind():
        create_or_update_record(43,'Other game untouched',mod_ids=[],db=r.db)
        r.path('.smm-snapshot-sandbox').write_text('v1',encoding='utf8')
    return r

def verify(window,resources):
    from PySide6.QtTest import QTest
    from services.business_resources import _scope
    scopes=[]
    def audit(event,args):
        scope=_scope.get()
        if scope is not None and scope.resources is resources and not any(s is scope for s in scopes):scopes.append(scope)
    sys.addaudithook(audit)
    output=resources.path('evidence');output.mkdir()
    report={'stages':{},'responses':[]}
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    def js(code):
        values=[];window.page.runJavaScript(code,values.append)
        for _ in range(300):
            if values:return values[0]
            QTest.qWait(10);time.sleep(.005)
        raise TimeoutError(code)
    def wait(condition):
        for _ in range(600):
            if js(condition):return
            QTest.qWait(15);time.sleep(.01)
        raise TimeoutError(condition+': '+str(js("$('snapshot-status').textContent+' / '+$('snapshot-error').textContent")))
    def files():
        return {str(p.relative_to(resources.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in resources.root.rglob('*') if p.is_file() and 'evidence' not in p.relative_to(resources.root).parts and p.name not in ('mod_manager.db','mod_manager.db-wal','mod_manager.db-shm','app_instance.lock')}
    def mods():return [tuple(row) for row in resources.db._conn.execute('SELECT mod_id,internal_id,deploy_status FROM mods ORDER BY mod_id')]
    def capture(name):
        QTest.qWait(150);window.grab().save(str(output/(name+'.png')))
        report['stages'][name]={'view':json.loads(js('JSON.stringify(snapshotData)')),'resource_hashes':files(),'mods':mods()}
    def submit(action,name='',id=None):
        protected=files();before=mods()
        js('openSnapshotEditor('+json.dumps(action)+','+json.dumps(id)+')')
        if name:js("$('snapshot-name').value="+json.dumps(name))
        js("$('snapshot-form').requestSubmit()")
        wait("!snapshotBusy && !$('snapshot-dialog').open")
        assert files()==protected and mods()==before
        report.setdefault('snapshot_resources_unchanged',[]).append(action)
    def deploy(iid,action,count):
        js("$('library-nav').click();inspect(S.assets.find(a=>a.internal_id==="+json.dumps(iid)+"))")
        wait("!!document.querySelector('[data-command="+action+"]')")
        js("document.querySelector('[data-command="+action+"]').click()")
        wait('!S.busy && S.assets.filter(a=>a.deployed).length==='+str(count))
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load()")
        wait("S.assets.length===3 && !$('refresh').disabled")
        js("$('deployment-nav').click();$('snapshot-tab').click()")
        wait('snapshotData?.game==="1142710"')
        baseline_files=files();baseline_mods=mods()
        js("$('snapshot-create').click();$('snapshot-name').value='WH3 双成员基线'")
        capture('01-create-snapshot')
        js("$('snapshot-form').requestSubmit()")
        wait("!snapshotBusy && snapshotData.records.length===1 && snapshotData.comparison?.common.length===2")
        assert files()==baseline_files and mods()==baseline_mods
        rid=js('snapshotSelected');capture('02-snapshot-list-and-equal')
        ids=json.loads(js('JSON.stringify(S.assets.map(a=>({id:a.internal_id,deployed:a.deployed})))'))
        deployed=[a['id'] for a in ids if a['deployed']];third=next(a['id'] for a in ids if not a['deployed'])
        # Real admitted deployment actions produce all three comparison groups.
        deploy(deployed[1],'undeploy',1);deploy(third,'deploy',2)
        js("$('deployment-nav').click()")
        wait('snapshotData.comparison?.common.length===1 && snapshotData.comparison.snapshot_only.length===1 && snapshotData.comparison.current_only.length===1')
        capture('03-real-member-differences')
        report['changed_members']=mods()
        deploy(third,'undeploy',1);deploy(deployed[1],'deploy',2)
        js("$('deployment-nav').click()")
        wait('snapshotData.comparison?.common.length===2 && snapshotData.comparison.snapshot_only.length===0 && snapshotData.comparison.current_only.length===0')
        capture('04-restored-no-difference')
        submit('snapshot-rename','WH3 重命名快照',rid)
        wait("snapshotData.records[0].name==='WH3 重命名快照'")
        # Actual SQLite failure after record INSERT must rollback, including UI feedback.
        resources.db._conn.execute("CREATE TEMP TRIGGER fail_snapshot BEFORE INSERT ON deployment_record_items BEGIN SELECT RAISE(ABORT,'snapshot persistence denied'); END")
        js("$('snapshot-create').click();$('snapshot-name').value='不应残留的快照';$('snapshot-form').requestSubmit()")
        wait("!snapshotBusy && $('snapshot-error').textContent.includes('persistence denied')")
        assert len(resources.db.list_deployment_records(1142710))==1
        capture('05-database-failure')
        resources.db._conn.execute('DROP TRIGGER fail_snapshot');js("$('snapshot-cancel').click()")
        # Duplicate and stale/cross-game commands still travel through WebChannel.
        js("window.oldSnapshotContext={game:$('game').value,snapshot:S.snapshot,version:snapshotData.version,record_id:snapshotSelected};window.rapidSnapshots=[];for(let i=0;i<2;i++)rpc('snapshot-rename',{...window.oldSnapshotContext,name:'快速请求'+i},true).then(r=>window.rapidSnapshots.push({success:true}),e=>window.rapidSnapshots.push({error:e.message}));")
        wait('window.rapidSnapshots.length===2');assert js('window.rapidSnapshots.filter(x=>x.success).length')==1
        report['duplicate_results']=json.loads(js('JSON.stringify(window.rapidSnapshots)'));js('refreshSnapshots()')
        wait("snapshotData.records[0].name.startsWith('快速请求')")
        js("$('game').value='43';load()")
        wait("snapshotData?.game==='43'")
        js("window.staleSnapshot=null;rpc('snapshot-delete',window.oldSnapshotContext,true).then(()=>window.staleSnapshot='BAD',e=>window.staleSnapshot=e.message)")
        wait('window.staleSnapshot!==null');assert js('window.staleSnapshot')!='BAD';report['cross_game_refused']=js('window.staleSnapshot')
        assert len(resources.db.list_deployment_records(43))==1
        js("$('game').value='1142710';load()")
        wait("snapshotData?.game==='1142710' && snapshotData.records.length===1")
        js('snapshotSelected='+str(rid)+';refreshSnapshots()')
        wait('snapshotData.comparison?.common.length===2')
        capture('06-persistent-reentered')
        js('openSnapshotEditor("snapshot-delete",'+str(rid)+')');capture('07-delete-confirmation')
        protected=files();before=mods();js("$('snapshot-form').requestSubmit()")
        wait("!snapshotBusy && snapshotData.records.length===0")
        assert files()==protected and mods()==before and mods()==baseline_mods
        capture('08-deleted-empty')
        # Persist a separate empty snapshot via the actual UI, then delete it.
        js("$('game').value='43';load()")
        wait("snapshotData?.game==='43'")
        submit('snapshot-save','空快照验收');wait('snapshotData.comparison?.common.length===0')
        empty=js('snapshotSelected');assert js('snapshotData.records.find(r=>r.id===snapshotSelected).count')==0
        submit('snapshot-delete',id=empty)
        assert resources.db.list_deployment_records(43)[0].name=='Other game untouched'
        report['other_game_unchanged']=True
        from core.db_manager import DatabaseManager
        from frontend.library_adapter import LibraryAdapter
        from frontend.web_window import WebLibraryWindow
        reopened=DatabaseManager.open_existing(resources.db.db_path,read_only=True)
        report['reopened']={'wh3_records':[r.as_dict() for r in reopened.list_deployment_records(1142710)],
            'other_records':[r.as_dict() for r in reopened.list_deployment_records(43)],
            'members':[tuple(row) for row in reopened._conn.execute('SELECT record_id,mod_id FROM deployment_record_items')]}
        assert not report['reopened']['wh3_records'] and len(report['reopened']['other_records'])==1
        original=window
        window=WebLibraryWindow(LibraryAdapter(reopened));window.show()
        window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load()")
        wait("S.assets.length===3 && !$('refresh').disabled")
        js("$('deployment-nav').click();$('snapshot-tab').click()")
        wait("snapshotData?.game==='1142710' && !snapshotData.enabled")
        js("window.readonlySnapshotResults=[];for(const action of ['snapshot-save','snapshot-rename','snapshot-delete'])rpc(action,{game:$('game').value,snapshot:S.snapshot,name:'拒绝',record_id:1},true).then(()=>window.readonlySnapshotResults.push('BAD'),e=>window.readonlySnapshotResults.push(e.message))")
        wait('window.readonlySnapshotResults.length===3')
        report['readonly_refused']=json.loads(js('JSON.stringify(window.readonlySnapshotResults)'))
        assert all('只读' in message for message in report['readonly_refused'])
        capture('09-readonly-refused')
        window.bridge.shutdown();window.close();reopened.close();window=original
        report['business_write_targets']=sorted({path for scope in scopes for event,path in scope.writes})
        assert all(resources.validate(path).is_relative_to(resources.root) for path in report['business_write_targets'])
        assert not any(scope.violations for scope in scopes)
        report['window']={'logical_width':window.width(),'logical_height':window.height(),'dpr':window.devicePixelRatioF()}
        report['final_mods']=mods();report['success']=True
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

def main():
    from tools.agent_workspace import require_workspace
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=require_workspace, required=True);parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QLockFile,QTimer
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources,checked_sandbox_root,checked_resource_path
    from services.mutation_context import activate_test_context,reset_mutation_context
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    root=checked_sandbox_root(args.root);token=activate_test_context([root]);app=QApplication(sys.argv[:1])
    if root.exists() and any(root.iterdir()):
        if args.verify or not checked_resource_path(root,root/'.smm-snapshot-sandbox').is_file():raise PermissionError('需要独立的新隔离目录或已有工作台标记')
        resources=BusinessResources(root,DatabaseManager.open_existing(checked_resource_path(root,root/'data/mod_manager.db'),read_only=False))
    else:resources=initialize(root)
    lock=QLockFile(str(resources.path('data/app_instance.lock')))
    if not lock.tryLock(0):raise RuntimeError('该隔离窗口已运行')
    window=WebLibraryWindow(LibraryAdapter(resources.db,writable=True),resources=resources);window.show();code=0
    if args.verify:
        def run():
            nonlocal code
            try:verify(window,resources)
            except Exception:
                import traceback;traceback.print_exc();code=1
            app.quit()
        QTimer.singleShot(100,run)
    else:
        timer=QTimer(window)
        def ready(value):
            if value:
                timer.stop();window.page.runJavaScript("$('game').value='1142710';load().then(()=>{showSpace('deployment');selectWorkbench('snapshots');})")
        timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined' && !!S.snapshot",ready));timer.start(250)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():app.processEvents();time.sleep(.05)
        resources.db.close();lock.unlock();reset_mutation_context(token)
    return code


if __name__=='__main__':raise SystemExit(main())
