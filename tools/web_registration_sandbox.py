"""Interactive unified registration and same-Mod content completion evidence."""
import argparse
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def initialize(root):
    from tools.web_order_sandbox import initialize as existing
    r=existing(root)
    with r.bind():
        r.db.update_game_deploy_config(916440,name='Anno 1800')
        r.path('incoming').mkdir()
        with zipfile.ZipFile(r.path('incoming/content.zip'),'w') as z:z.writestr('data/unit.pack',b'ACTUAL SAME MOD CONTENT')
        r.path('incoming/nexus.html').write_text('<html><head><link rel="canonical" href="https://www.nexusmods.com/totalwarwarhammer3/mods/92002"><meta property="og:title" content="Nexus Offline Registered"><meta property="og:description" content="A real locally parsed source description."></head><body><script>document.body.innerHTML="UNTRUSTED SCRIPT";</script></body></html>',encoding='utf8')
        r.path('.smm-registration-sandbox').write_text('v1',encoding='utf8')
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
        for _ in range(250):
            if js(condition):return
            QTest.qWait(15);time.sleep(.01)
        raise TimeoutError(condition+': '+str(js("$('registration-error').textContent")))
    def capture(name):
        QTest.qWait(180);window.grab().save(str(output/(name+'.png')))
        report['stages'][name]=[dict(row) for row in resources.db._conn.execute('SELECT mod_id,internal_id,workspace_id,platform,external_id,app_id,content_status,deploy_status,last_known_path FROM mods ORDER BY mod_id')]
    def add_url(platform,url,name):
        wait("!$('library-add').disabled")
        js("$('library-add').click();$('registration-platform').value="+json.dumps(platform)+";$('registration-url').value="+json.dumps(url)+";$('registration-name').value="+json.dumps(name)+";$('import-form').requestSubmit()")
        wait('!registrationBusy && !!registrationDraft')
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load()")
        wait("S.assets.length===3 && !$('refresh').disabled")
        js("$('library-add').click()")
        wait("$('registration-dialog').open")
        capture('01-unified-add-entry')
        js("$('registration-platform').value='Nexus';$('registration-url').value='https://www.nexusmods.com/totalwarwarhammer3/mods/92001';$('registration-name').value='Registered Before Download';$('import-form').requestSubmit()")
        wait('!registrationBusy && !!registrationDraft');capture('02-url-preview')
        js("$('registration-confirm').click()")
        wait("!registrationBusy && !$('registration-dialog').open && S.assets.length===4 && !!S.details")
        iid=js('S.current.internal_id');pk=resources.db.find_mod_by_internal_id(iid);row=resources.db.get_mod_backup_row(pk);managed=resources.validate(row['last_known_path'])
        assert resources.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(pk,)).fetchone()[0]=='content_missing'
        assert js("$('inspector').textContent.includes('待补齐内容')")
        assert not js("S.details.actions.primary.enabled")
        capture('03-no-content-library-inspector')
        # User edits through the already-admitted real UI before content completion.
        js("document.querySelector('[data-command=edit]').click();$('edit-name').value='My protected title';$('edit-notes').value='Preserve my note';$('edit-form').requestSubmit()")
        wait("!S.busy && !$('editor').open && S.current?.name==='My protected title'")
        saved=resources.db.get_mod_display_info(pk)
        js("$('inspector-more').click();document.querySelector('[data-action=content-add]').click();$('registration-content-source').value="+json.dumps(str(resources.path('incoming/content.zip')))+";$('import-form').requestSubmit()")
        wait("!registrationBusy && !$('registration-dialog').open && !!S.details?.actions")
        assert (managed/'content.zip').read_bytes()==resources.path('incoming/content.zip').read_bytes()
        assert resources.db.find_mod_by_internal_id(iid)==pk
        assert resources.db.get_mod_display_info(pk).user_display_name==saved.user_display_name
        assert resources.db.get_mod_display_info(pk).user_notes==saved.user_notes
        assert resources.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(pk,)).fetchone()[0]=='healthy'
        capture('04-same-mod-content-completed')
        js("$('inspector-more').click();document.querySelector('[data-action=content-rescan]').click()")
        wait("!registrationBusy && $('game-caption').textContent.includes('内容已识别')")
        capture('05-content-recognized-deployment-policy')
        # Second restored non-Steam path: offline Nexus HTML metadata -> identity registration.
        js("$('acquisition-nav').click();$('acquisition-add').click();$('registration-kind').value='offline';updateRegistrationFields();$('registration-offline').value="+json.dumps(str(resources.path('incoming/nexus.html')))+";$('import-form').requestSubmit()")
        wait('!registrationBusy && registrationDraft?.preview.platform==="Nexus"')
        assert js("registrationDraft.preview.description.includes('locally parsed')")
        capture('06-offline-metadata-preview')
        js("$('registration-confirm').click()")
        wait("!registrationBusy && !$('registration-dialog').open && S.assets.length===5 && S.current?.name==='Nexus Offline Registered'")
        offline_iid=js('S.current.internal_id');capture('07-offline-source-registered')
        add_url('Nexus','https://www.nexusmods.com/totalwarwarhammer3/mods/92001','Duplicate must not overwrite')
        assert js('registrationDraft.preview.duplicate');capture('08-duplicate-preview')
        js("$('registration-confirm').click()")
        wait("!registrationBusy && !$('registration-dialog').open && S.current?.internal_id==="+json.dumps(iid))
        assert resources.db.get_mod_display_info(pk).user_display_name=='My protected title'
        # mod.io URL identity registration is separate from credential-bound API metadata.
        js("$('game').value='916440';load()")
        wait("S.assets.length===0 && !$('refresh').disabled")
        add_url('mod.io','https://mod.io/g/anno-1800/m/isolated-registration','Modio Identity Only')
        js("$('registration-confirm').click()")
        wait("!registrationBusy && !$('registration-dialog').open && S.assets.length===1 && S.current?.name==='Modio Identity Only'")
        modio_iid=js('S.current.internal_id');modio_pk=resources.db.find_mod_by_internal_id(modio_iid)
        path=resources.validate(resources.db.get_mod_backup_row(modio_pk)['last_known_path'])
        (path/'saved.html').write_text('<html>metadata only</html>',encoding='utf8');(path/'cover.png').write_bytes(b'cover only')
        js("$('inspector-more').click();document.querySelector('[data-action=content-rescan]').click()")
        wait("!registrationBusy && $('game-caption').textContent.includes('待补齐')")
        assert resources.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(modio_pk,)).fetchone()[0]=='content_missing'
        # Explicit user copying into the authority-resolved same managed directory.
        (path/'manual.pack').write_bytes(b'MANUALLY COPIED CONTENT')
        js("$('inspector-more').click();document.querySelector('[data-action=content-rescan]').click()")
        wait("!registrationBusy && $('game-caption').textContent.includes('内容已识别')")
        capture('09-manual-content-recognized')
        assert resources.db.find_mod_by_internal_id(modio_iid)==modio_pk
        js("$('game').value='1142710';load()")
        wait("S.assets.length===5 && !$('refresh').disabled")
        add_url('Nexus','https://www.nexusmods.com/totalwarwarhammer3/mods/92003','Failure example')
        resources.db._conn.execute("CREATE TEMP TRIGGER fail_registered_content BEFORE UPDATE OF content_status ON mods WHEN NEW.external_id='92003' AND NEW.content_status='content_missing' BEGIN SELECT RAISE(ABORT,'injected registration persistence failure'); END")
        before_failed=resources.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]
        js("$('registration-confirm').click()")
        wait("!registrationBusy && $('registration-error').textContent.includes('操作失败')")
        assert resources.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]==before_failed
        assert list(resources.path('cache/registration_failed').rglob('metadata.json'))
        capture('10a-real-database-failure-and-compensation')
        resources.db._conn.execute('DROP TRIGGER fail_registered_content')
        js("$('registration-close').click();$('library-add').click();$('registration-url').value='https://www.nexusmods.com.evil/game/mods/1';$('registration-name').value='Invalid host';$('import-form').requestSubmit()")
        wait("!registrationBusy && $('registration-error').textContent.includes('预览失败')")
        capture('10-real-input-failure')
        assert len(resources.db.list_mod_list_items(game_id=1142710))==5
        js("$('registration-close').click();$('library-add').click();$('registration-kind').value='local';updateRegistrationFields();$('import-kind').value='zip';$('import-kind').dispatchEvent(new Event('change'));$('import-source').value="+json.dumps(str(resources.path('incoming/content.zip')))+";$('import-workshop').value='92010';$('import-title').value='Unified Steam Local';$('import-form').requestSubmit()")
        wait("!registrationBusy && registrationDraft?.preview.platform==='Steam'")
        capture('11-unified-local-content-preview')
        js("$('registration-confirm').click()")
        wait("!registrationBusy && !$('registration-dialog').open && S.assets.length===6 && S.current?.name==='Unified Steam Local'")
        capture('12-existing-import-worker-retained')
        imported=resources.validate(resources.db.get_mod_backup_row(resources.db.find_mod_by_internal_id(js('S.current.internal_id')))['last_known_path'])
        assert any(p.read_bytes()==b'ACTUAL SAME MOD CONTENT' for p in imported.rglob('unit.pack'))
        js("$('inspector-more').click();document.querySelector('[data-action=open-folder]').click()")
        wait("!registrationBusy && $('game-caption').textContent.includes('已打开')")
        # Actual readonly WebEngine still rejects registration and association commands.
        from core.db_manager import DatabaseManager
        from frontend.library_adapter import LibraryAdapter
        from frontend.web_window import WebLibraryWindow
        readonly_db=DatabaseManager.open_existing(resources.db.db_path,read_only=True)
        original=window;window=WebLibraryWindow(LibraryAdapter(readonly_db));window.show()
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("window.roResults=[];for(const action of ['registration-save','content-add','content-rescan','content-open'])rpc(action,{game:$('game').value,snapshot:S.snapshot},true).then(()=>window.roResults.push('BAD'),e=>window.roResults.push(e.message))")
        wait('window.roResults.length===4')
        report['readonly_refused']=json.loads(js('JSON.stringify(window.roResults)'))
        assert all('只读' in e for e in report['readonly_refused'])
        capture('13-readonly-entry-disabled')
        window.bridge.shutdown();window.close();readonly_db.close();window=original
        report['same_identity']={'internal_id':iid,'mod_pk':str(pk),'archive_sha256':hashlib.sha256((managed/'content.zip').read_bytes()).hexdigest(),'managed_path':str(managed)}
        report['offline_identity']=offline_iid;report['manual_modio_identity']=modio_iid
        report['business_write_targets']=sorted({path for scope in scopes for event,path in scope.writes})
        assert not any(scope.violations for scope in scopes)
        assert all(resources.validate(path).is_relative_to(resources.root) for path in report['business_write_targets'])
        from core.db_manager import DatabaseManager
        reopened=DatabaseManager.open_existing(resources.db.db_path,read_only=True)
        report['reopened']=[dict(row) for row in reopened._conn.execute('SELECT mod_id,internal_id,platform,external_id,content_status,deploy_status FROM mods ORDER BY mod_id')]
        reopened.close()
        report['window']={'width':window.width(),'height':window.height(),'dpr':window.devicePixelRatioF()};report['success']=True
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
        if args.verify or not checked_resource_path(root,root/'.smm-registration-sandbox').is_file():raise PermissionError('需要独立的新隔离目录或已有工作台标记')
        resources=BusinessResources(root,DatabaseManager.open_existing(checked_resource_path(root,root/'data/mod_manager.db'),read_only=False))
    else:resources=initialize(root)
    lock=QLockFile(str(resources.path('data/app_instance.lock')))
    if not lock.tryLock(0):raise RuntimeError('该隔离窗口已运行')
    window=WebLibraryWindow(LibraryAdapter(resources.db,writable=True),resources=resources);window.setWindowTitle('SMM · 统一注册与内容补齐 · 隔离验收');window.show();code=0
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
                timer.stop();window.page.runJavaScript("$('game').value='1142710';load().then(()=>showSpace('acquire'))")
        timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined' && !!S.snapshot",ready));timer.start(250)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():app.processEvents();time.sleep(.05)
        resources.db.close();lock.unlock();reset_mutation_context(token)
    return code


if __name__=='__main__':raise SystemExit(main())
