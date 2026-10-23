"""Real WH3 deployment/order fixture and interactive WebEngine verification."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def initialize(root):
    from tools.wh3_deploy_validation import initialize as wh3_initialize
    from services.identity_service import create_mod_identity,identity_create_scope
    from services.deploy import ModDeployer
    resources,source,target,first=wh3_initialize(root)
    identities=[first]
    with resources.bind():
        resources.db.update_game_deploy_config(43,name='无排序 Backend 的隔离游戏')
        for wid,title in [('902101','Campaign UI'),('902102','Undeployed Collection')]:
            with identity_create_scope():
                identity=create_mod_identity(resources.db,platform='Steam',external_id=wid,workshop_id=wid,
                    title=title,app_id=1142710,game_name='Warhammer3',operation='import')
            folder=resources.path('mod/Warhammer3')/title.replace(' ','')
            (folder/'.info').mkdir(parents=True)
            (folder/'library-only.pack').write_bytes(b'LIBRARY-PRESERVE')
            (folder/'.info/metadata.json').write_text(json.dumps({'internal_id':identity.internal_id,
                'workspace_id':wid,'published_file_id':wid,'platform':'Steam','title':title,'app_id':1142710}),encoding='utf8')
            resources.db.update_mod_identity_fields(identity.mod_id,last_known_path=str(folder),folder_present=True,title=title,game_name='Warhammer3')
            native=resources.path('games/steamapps/workshop/content/1142710')/wid
            native.mkdir()
            (native/('campaign.pack' if wid=='902101' else 'undeployed.pack')).write_bytes(('WORKSHOP-'+wid).encode())
            identities.append(identity)
        resources.path('.smm-order-sandbox').write_text('v1',encoding='utf8')
    deployer=ModDeployer(library_root=resources.path('mod'),db=resources.db,resources=resources)
    for identity in identities[:2]:assert deployer.deploy_mod(identity.internal_id)['success']
    return resources


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
    json_path=resources.path('config/load_order/wh3.json')
    projection=resources.path('games/Warhammer3/used_mods.txt')
    native=resources.path('games/steamapps/workshop/content/1142710')
    packs={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in native.rglob('*.pack')}
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
        raise TimeoutError(condition+': '+str(js("$('deployment-status').textContent")))
    def capture(name):
        window.grab().save(str(output/(name+'.png')))
        report['stages'][name]={'order':json.loads(json_path.read_text('utf8')),'projection':projection.read_text('utf8')}
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load();")
        wait("S.assets.length===3 && !$('refresh').disabled")
        js("$('deployment-nav').click()")
        wait('deploymentData?.rows.length===2')
        capture('01-workbench')
        first=js('deploymentData.order[0]');second=js('deploymentData.order[1]')
        js("document.querySelector('[data-order-down]').click()")
        wait("!orderSaving && $('deployment-status').textContent.includes('完成游戏投影')")
        assert json.loads(json_path.read_text('utf8'))['order']==[second,first]
        text=projection.read_text('utf8');assert text.index('mod "campaign.pack"')<text.index('mod "native.pack"')
        assert 'add_working_directory' in text and 'undeployed.pack' not in text
        capture('02-moved-and-projected')
        js("$('library-nav').click();$('deployment-nav').click()")
        wait('deploymentData.order[0]==='+json.dumps(second))
        capture('03-reentered')
        # Drive the real HTML drag/drop handlers, then verify the Backend files.
        js("{const rows=document.querySelectorAll('[data-order-id]');const transfer=new DataTransfer();rows[0].querySelector('.drag-handle').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));rows[1].dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:transfer}));rows[1].dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}));}")
        wait('!orderSaving && deploymentData.order[0]==='+json.dumps(first))
        assert json.loads(json_path.read_text('utf8'))['order']==[first,second]
        capture('04-drag-roundtrip')
        # A real OS read-only file makes the actual projection write fail.
        import os,stat
        os.chmod(projection,stat.S_IREAD)
        js("document.querySelector('[data-order-down]').click()")
        wait("!orderSaving && !!deploymentData.projection_error && !$('order-retry').disabled")
        assert json.loads(json_path.read_text('utf8'))['order']==[second,first]
        assert projection.read_text('utf8').index('mod "native.pack"')<projection.read_text('utf8').index('mod "campaign.pack"')
        capture('05-projection-failed')
        os.chmod(projection,stat.S_IREAD|stat.S_IWRITE)
        js("$('order-retry').click()")
        wait("!orderSaving && !deploymentData.projection_error")
        assert projection.read_text('utf8').index('mod "campaign.pack"')<projection.read_text('utf8').index('mod "native.pack"')
        capture('06-projection-recovered')
        # External pack loss never removes canonical deployed membership.
        missing=native/'902100/native.pack'
        missing.rename(missing.with_suffix('.missing'))
        js('refreshDeployment()')
        wait("deploymentData.rows.length===2 && deploymentData.rows.some(r=>r.warning)")
        capture('07-missing-pack-member')
        missing.with_suffix('.missing').rename(missing)
        js('refreshDeployment()')
        wait('deploymentData.rows.every(r=>!r.warning)')
        os.chmod(json_path,stat.S_IREAD)
        js("document.querySelector('[data-order-down]').click()")
        wait("!orderSaving && deploymentData.projection_error.startsWith('排序保存失败')")
        capture('08-json-save-failed')
        assert json.loads(json_path.read_text('utf8'))['order']==[second,first]
        os.chmod(json_path,stat.S_IREAD|stat.S_IWRITE)
        js("document.querySelector('[data-order-down]').click()")
        wait("!orderSaving && !deploymentData.projection_error && deploymentData.order[0]==="+json.dumps(first))
        # Library commands use the already-admitted real DeployWorker.
        js("$('library-nav').click();inspect(S.assets.find(a=>a.internal_id==="+json.dumps(second)+"));")
        wait("!!S.details && !!document.querySelector('[data-command=undeploy]')")
        js("document.querySelector('[data-command=undeploy]').click()")
        wait('!S.busy && S.assets.filter(a=>a.deployed).length===1')
        js("$('deployment-nav').click()")
        wait('deploymentData.rows.length===1')
        capture('09-library-undeploy-membership')
        js("$('library-nav').click();inspect(S.assets.find(a=>a.internal_id==="+json.dumps(second)+"));")
        wait("!!S.details && !!document.querySelector('[data-command=deploy]')")
        js("document.querySelector('[data-command=deploy]').click()")
        wait('!S.busy && S.assets.filter(a=>a.deployed).length===2')
        js("$('deployment-nav').click()")
        wait('deploymentData.rows.length===2')
        capture('10-library-deploy-membership')
        js("window.rapidResults=[];window.oldOrderContext={game:$('game').value,snapshot:S.snapshot,version:deploymentData.version,order:deploymentData.order.slice().reverse()};for(let i=0;i<2;i++)rpc('order-save',{...window.oldOrderContext,order:i===0?window.oldOrderContext.order:deploymentData.order},true).then(r=>window.rapidResults.push({success:true}),e=>window.rapidResults.push({error:e.message}));")
        wait('window.rapidResults.length===2')
        assert js('window.rapidResults.filter(r=>r.success).length')==1
        report['rapid_results']=js('window.rapidResults')
        js('refreshDeployment()')
        wait('deploymentData.order[0]==='+json.dumps(second))
        js("$('game').value='43';load();")
        wait("!!deploymentData && deploymentData.game==='43'")
        js("window.staleResult=null;rpc('order-save',window.oldOrderContext,true).then(()=>window.staleResult='BAD',e=>window.staleResult=e.message)")
        wait('window.staleResult!==null')
        assert js('window.staleResult')!='BAD'
        report['cross_game_refused']=js('window.staleResult')
        assert not js('deploymentData.enabled')
        js("$('game').value='1142710';load();")
        wait("deploymentData.game==='1142710' && deploymentData.rows.length===2")
        capture('11-final-reentered')
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in packs.items())
        report['workshop_packs_unchanged']=packs
        report['business_write_targets']=sorted({path for scope in scopes for event,path in scope.writes})
        assert all(resources.validate(path).is_relative_to(resources.root) for path in report['business_write_targets'])
        assert not any(scope.violations for scope in scopes)
        report['window']={'logical_width':window.width(),'logical_height':window.height(),'dpr':window.devicePixelRatioF()}
        report['success']=True
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
        if args.verify or not checked_resource_path(root,root/'.smm-order-sandbox').is_file():raise PermissionError('需要独立的新隔离目录或已有工作台标记')
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
                timer.stop();window.page.runJavaScript("$('game').value='1142710';load().then(()=>showSpace('deployment'))")
        timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined' && !!S.snapshot",ready));timer.start(250)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():app.processEvents();time.sleep(.05)
        resources.db.close();lock.unlock();reset_mutation_context(token)
    return code


if __name__=='__main__':raise SystemExit(main())
