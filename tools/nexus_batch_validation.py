"""Actual file chooser -> WebChannel -> batch ImportWorker -> SQLite/CAS -> library."""
import hashlib
import json
import time
from pathlib import Path


def verify(window,r):
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication,QFileDialog,QLineEdit
    from services.business_resources import _scope
    output=r.path('evidence');output.mkdir();report={'responses':[]};scopes=[]
    def audit(event,args):
        scope=_scope.get()
        if scope and scope.resources is r and not any(s is scope for s in scopes):scopes.append(scope)
    import sys
    sys.addaudithook(audit)
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    def hashes(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    sources=hashes(r.path('incoming'));protected={name:hashes(r.path(name)) for name in ('config','games','mod')}
    def js(code):
        values=[];window.page.runJavaScript(code,values.append)
        for _ in range(400):
            if values:return values[0]
            QTest.qWait(10);time.sleep(.005)
        raise TimeoutError(code)
    def wait(expression):
        for _ in range(1400):
            if js(expression):return
            QTest.qWait(15);time.sleep(.01)
        raise TimeoutError(expression+' '+str(js("$('offline-status').textContent")))
    def capture(name):QTest.qWait(100);window.grab().save(str(output/(name+'.png')))
    def choose(paths):
        errors=[]
        def accept():
            try:
                dialog=QApplication.activeModalWidget()
                assert isinstance(dialog,QFileDialog),type(dialog)
                edit=dialog.findChild(QLineEdit,'fileNameEdit');assert edit
                edit.setText(' '.join('"'+p.name+'"' for p in paths));dialog.accept()
            except Exception as exc:errors.append(repr(exc))
        QTimer.singleShot(500,accept);js("$('offline-pick').click()")
        wait("$('offline-paths').value.split('\\n').filter(Boolean).length==="+str(len(paths)))
        assert not errors,errors
    def parse():
        js("$('offline-parse').click()");wait('!!NB.data&&!NB.busy')
    def submit():
        js("$('offline-submit').click()");wait('NB.busy');capture('03-original-batch-worker-running')
        wait('!NB.busy&&!registrationBusy&&!!NB.result')
        return json.loads(js('JSON.stringify(NB.result)'))
    try:
        js("$('game').value='413150';load()")
        wait("$('game').value==='413150'&&!$('refresh').disabled")
        js("$('library-add').click();$('registration-kind').value='offline';updateRegistrationFields()")
        paths=sorted(p for p in r.path('incoming').iterdir() if p.is_file());choose(paths);capture('01-real-multiple-file-selection')
        parse();report['preview']=json.loads(js('JSON.stringify(NB.data)'));capture('02-html-mhtml-preview-and-rejections')
        assert sum(row['state']=='new' for row in report['preview']['rows'])==3
        # One input changes after preview: fail that item, while other actual pages register.
        first=paths[0];original=first.read_bytes();first.write_bytes(b'Changed after preview')
        partial=submit();report['partial']=partial
        assert partial['state']=='partial' and sum(i['state']=='success' for i in partial['items'])==2,partial
        capture('04-partial-success-resource-failure')
        first.write_bytes(original)
        js("$('offline-retry').click()");wait('!!NB.data&&!NB.busy&&!NB.result')
        js("$('offline-submit').click()");wait('!NB.busy&&!registrationBusy&&!!NB.result')
        report['retry']=json.loads(js('JSON.stringify(NB.result)'));assert report['retry']['state']=='success',report['retry']
        capture('05-retry-complete-registration')
        assets=window.bridge.adapter.assets(413150);assert len(assets)==3
        report['resources']=[]
        from services.info_asset_runtime import load_usable_live_manifest
        with r.bind():
            for asset in assets:
                index=Path(asset.managed_path)/'.info/offline/index.html'
                manifest=load_usable_live_manifest(index);assert manifest is not None and manifest.assets
                report['resources'].append({'internal_id':asset.internal_id,'mod_pk':asset.mod_pk,'source':asset.source_url if hasattr(asset,'source_url') else asset.workspace_id,
                                            'path':str(index),'manifest_assets':len(manifest.assets),'hashes':hashes(Path(asset.managed_path))})
        js("document.querySelector('[data-workshop-view]').click()")
        wait('!!S.current&&!!S.details');capture('06-library-real-nexus-mod-and-cover')
        js("document.querySelector('[data-command=edit]').click();$('edit-name').value='My Nexus name';$('edit-description').value='My protected Nexus description';$('edit-notes').value='My protected Nexus notes';$('edit-form').requestSubmit()")
        wait("!S.busy&&!$('editor').open&&S.current?.name==='My Nexus name'")
        js("$('favorite-action').click()");wait('!S.busy&&!!S.current?.favorite')
        selected_id=js('S.current.internal_id')
        selected=next(a for a in window.bridge.adapter.assets(413150) if a.internal_id==selected_id)
        from PySide6.QtGui import QImage
        from services.importers.image_picker import apply_cover_to_mod
        image=QImage(8,8,QImage.Format.Format_RGB32);image.fill(0xff325747)
        cover=r.path('cache/user-cover.png');image.save(str(cover))
        with r.bind():apply_cover_to_mod(Path(selected.managed_path),cover,mod_id=selected.mod_pk,mark_user_override=True)
        js('openTags()');wait("$('organization').open&&!!$('tag-query')")
        js("$('tag-query').value='Nexus Offline';$('tag-add').click()")
        wait("!S.busy&&!!document.querySelector('[data-remove-tag]')")
        js("$('organization').close()")
        tags_before=[tuple(row) for row in r.db._conn.execute('SELECT * FROM mod_tags ORDER BY mod_id,tag_type,tag_value')]
        preserved={a.internal_id:hashes(Path(a.managed_path)) for a in window.bridge.adapter.assets(413150)}
        identities={a.internal_id:a.mod_pk for a in assets}
        js("$('library-add').click();$('registration-kind').value='offline';updateRegistrationFields()")
        choose(paths);parse();js("$('offline-all').click();$('offline-submit').click()")
        wait('!NB.busy&&!registrationBusy&&!!NB.result')
        report['repeat']=json.loads(js('JSON.stringify(NB.result)'));assert all(i['state']=='skipped' for i in report['repeat']['items'])
        assert identities=={a.internal_id:a.mod_pk for a in window.bridge.adapter.assets(413150)}
        assert preserved=={a.internal_id:hashes(Path(a.managed_path)) for a in window.bridge.adapter.assets(413150)}
        assert tags_before==[tuple(row) for row in r.db._conn.execute('SELECT * FROM mod_tags ORDER BY mod_id,tag_type,tag_value')]
        report['protected_tags']=tags_before
        capture('07-repeat-skipped-with-user-fields-protected')
        assert hashes(r.path('incoming'))==sources
        assert hashes(r.path('config'))==protected['config'] and hashes(r.path('games'))==protected['games']
        for file,digest in protected['mod'].items():assert hashlib.sha256((r.path('mod')/file).read_bytes()).hexdigest()==digest
        report['write_targets']=sorted({path for scope in scopes for _,path in scope.writes})
        assert not any(scope.violations for scope in scopes),[s.violations for s in scopes if s.violations]
        assert not any(Path(p).is_relative_to(r.path('incoming')) for p in report['write_targets'])
        from core.db_manager import DatabaseManager
        db=DatabaseManager.open_existing(r.db.db_path,read_only=True)
        report['reopened']=[dict(row) for row in db._conn.execute('SELECT mod_id,internal_id,app_id,platform,external_id,source_url,title,display_name,custom_description,user_notes,favorite,cover_path,user_override_fields,content_status,deploy_status,offline_status FROM mods WHERE app_id=413150')]
        assert len(report['reopened'])==3;db.close()
        report['cas_objects']=hashes(r.path('data/asset_store'))
        origins=json.loads(r.path('sample-origins.json').read_text('utf8'))
        assert all(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()==item['sha256'] for item in origins)
        report['sample_origins']=origins
        report['window']={'width':window.width(),'height':window.height(),'dpr':window.devicePixelRatioF()}
        from frontend.library_adapter import LibraryAdapter
        from frontend.web_window import WebLibraryWindow
        rodb=DatabaseManager.open_existing(r.db.db_path,read_only=True);original_window=window
        window=WebLibraryWindow(LibraryAdapter(rodb,writable=False));window.show()
        try:
            wait("typeof S!=='undefined'&&!!S.snapshot")
            js("window.offlineReadonly=null;rpc('offline-register',{game:$('game').value,snapshot:S.snapshot,preview:'readonly',ids:['0']},true).then(()=>window.offlineReadonly='BAD',e=>{window.offlineReadonly=e.message;$('game-caption').textContent=e.message;})")
            wait('window.offlineReadonly!==null')
            report['readonly_refused']=js('window.offlineReadonly');assert report['readonly_refused']!='BAD'
            capture('08-native-readonly-window-refusal')
        finally:
            while not window.bridge.shutdown():QTest.qWait(20);time.sleep(.01)
            window.close();window.deleteLater();QTest.qWait(50);rodb.close();window=original_window
        report['success']=True
    finally:output.joinpath('result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
