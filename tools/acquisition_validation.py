"""Drive the actual WebChannel UI and record authoritative persisted results."""
import hashlib
import json
import time


def verify_window(window,resources,source,control,requests):
    from PySide6.QtTest import QTest
    from services.business_resources import _scope
    import sys
    scopes=[]
    def record_scope(event,args):
        scope=_scope.get()
        if scope is not None and scope.resources is resources and not any(item is scope for item in scopes):
            scopes.append(scope)
    sys.addaudithook(record_scope)
    output=resources.root/'evidence'
    output.mkdir()
    report={'responses':[],'stages':{}}
    def protected_files():
        return {str(path.relative_to(resources.root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ('games','config','data/deploy_backup') for path in resources.path(name).rglob('*') if path.is_file()}
    protected_before=protected_files()
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    def js(code):
        values=[]
        window.page.runJavaScript(code,values.append)
        for _ in range(300):
            if values:return values[0]
            QTest.qWait(10);time.sleep(.005)
        raise TimeoutError(code)
    def wait(condition):
        for _ in range(1000):
            if js(condition):return
            QTest.qWait(15);time.sleep(.01)
        raise TimeoutError(condition+' '+str(js("$('acquisition-message').textContent")))
    def capture(name):
        window.grab().save(str(output/(name+'.png')))
        with resources.db._lock:
            report['stages'][name]=[dict(row) for row in resources.db._conn.execute('SELECT * FROM mods ORDER BY mod_id')]
    def submit_import():
        js("$('acquisition-add').click();$('registration-kind').value='local';updateRegistrationFields()")
        js("$('import-source').value="+json.dumps(str(source))+"; $('import-workshop').value='990700'; $('import-title').value='Imported Local Workshop Mod'; $('import-start').click();")
        wait("!registrationBusy && (!!registrationDraft || $('registration-error').textContent.startsWith('预览失败'))")
        if js("!!registrationDraft"):js("$('registration-confirm').click()")
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='42';load();")
        wait("S.assets.length===1 && !$('refresh').disabled")
        js("$('acquisition-nav').click()")
        wait("!!acquisitionData && acquisitionData.import_allowed")
        capture('01-acquisition')
        submit_import()
        wait("!!acquisitionData?.tasks.some(t=>t.state==='running')")
        capture('02-import-running')
        js("window.duplicateResult=null;rpc('import-folder',{game:$('game').value,snapshot:S.snapshot,source:$('import-source').value,workshop_id:'990700',title:'Imported Local Workshop Mod'},true).then(()=>window.duplicateResult='BAD',e=>window.duplicateResult=e.message)")
        wait('window.duplicateResult!==null')
        assert '正在执行' in js('window.duplicateResult')
        report['duplicate_in_flight_rejected']=js('window.duplicateResult')
        wait("!acquisitionSubmitting && !registrationBusy")
        wait("acquisitionData.tasks.some(t=>t.action==='import-folder' && t.state==='success')")
        capture('03-import-result')
        assert js("acquisitionData.tasks.some(t=>t.action==='import-folder' && t.state==='success')"),js("$('acquisition-message').textContent")
        with resources.db._lock:
            row=dict(resources.db._conn.execute("SELECT * FROM mods WHERE workspace_id='990700'").fetchone())
        pk=row['mod_id']
        folder=resources.validate(row['last_known_path'])
        assert folder.is_dir() and folder!=source
        payload={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.txt')}
        assert all((folder/name).is_file() and hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest for name,digest in payload.items())
        assert row['internal_id'] and row['workspace_id'] and (folder/'.info/metadata.json').is_file()
        with resources.bind():
            resources.db.update_mod_user_metadata(pk,{'display_name':'My custom name','custom_description':'My protected description','user_notes':'Keep my notes','favorite':True})
            from PySide6.QtGui import QImage
            cover=folder/'.info/user-cover.png'
            image=QImage(32,32,QImage.Format_RGB32)
            image.fill(0x467C74)
            assert image.save(str(cover),'PNG')
            resources.db.update_mod_cover_path(pk,'.info/user-cover.png')
            resources.db.set_user_override_field(pk,'cover')
        cover_hash=hashlib.sha256(cover.read_bytes()).hexdigest()
        report['import']={'mod_pk':pk,'internal_id':row['internal_id'],'workspace_id':row['workspace_id'],'archive':str(folder),'payload_count':len(payload)}
        js("document.querySelector('[data-task-view]').click()")
        wait("S.space==='library' && !!S.details")
        capture('04-imported-library-detail')
        js("$('acquisition-nav').click()")
        submit_import()
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.tasks.some(t=>t.state==='duplicate')")
        with resources.db._lock:assert resources.db._conn.execute("SELECT COUNT(*) FROM mods WHERE workspace_id='990700'").fetchone()[0]==1
        control.write_text(json.dumps({'mode':'missing'}),encoding='utf8')
        wait("acquisitionData.candidates.some(a=>a.internal_id==="+json.dumps(row['internal_id'])+")")
        js("$('sync-mod').value=acquisitionData.candidates.find(a=>a.internal_id==="+json.dumps(row['internal_id'])+").token;$('sync-start').click();")
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='failed')")
        capture('05-source-failure')
        assert js("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='failed')")
        assert not resources.db.is_official_metadata_synced(pk)
        control.write_text(json.dumps({'mode':'wrong-game'}),encoding='utf8')
        js("$('sync-start').click()")
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("$('acquisition-message').textContent.includes('不匹配')")
        with resources.db._lock:
            assert resources.db._conn.execute('SELECT app_id FROM mods WHERE mod_id=?',(pk,)).fetchone()[0]==42
        report['wrong_source_game_rejected']=True
        for mode in ('malformed','timeout'):
            control.write_text(json.dumps({'mode':mode}),encoding='utf8')
            js("$('sync-start').click()")
            wait('!acquisitionSubmitting && !registrationBusy')
            assert js("$('acquisition-message').textContent.startsWith('操作失败：')")
            assert not resources.db.is_official_metadata_synced(pk)
            report[mode+'_reported_failure']=True
        control.write_text(json.dumps({'mode':'success'}),encoding='utf8')
        prior_requests=len(requests)
        js("$('sync-start').click()")
        wait("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='running')")
        capture('06-source-running')
        for _ in range(300):
            if len(requests)>prior_requests:break
            QTest.qWait(10);time.sleep(.005)
        assert len(requests)>prior_requests
        js("$('game').value='43';load();")
        wait("S.assets.length===0 && !$('refresh').disabled")
        wait('!acquisitionSubmitting && !registrationBusy')
        assert js("$('game').value")=='43'
        assert not resources.db.list_mod_list_items(game_id=43)
        js("$('game').value='42';load();")
        wait("S.assets.length===2 && !$('refresh').disabled")
        wait("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='success')")
        assert '执行中' not in js("$('acquisition-message').textContent")
        capture('07-source-result')
        assert js("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='success')"),js("$('acquisition-message').textContent")
        assert resources.db.is_official_metadata_synced(pk)
        with resources.db._lock:
            after=dict(resources.db._conn.execute('SELECT * FROM mods WHERE mod_id=?',(pk,)).fetchone())
        assert after['title']=='Official Source Updated Title',after
        assert after['display_name']=='My custom name' and after['custom_description']=='My protected description'
        assert after['user_notes']=='Keep my notes' and after['favorite']==1
        folder=resources.validate(after['last_known_path'])
        assert after['cover_path']=='.info/user-cover.png' and hashlib.sha256((folder/'.info/user-cover.png').read_bytes()).hexdigest()==cover_hash
        assert all(hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest for name,digest in payload.items())
        assert len(requests)>=2 and all(r['path']=='/steam/details' for r in requests)
        report['sync']={'title':after['title'],'description':after['description'],'http_requests':requests,'payload_unchanged':True,'user_fields_and_cover_preserved':True}
        report['cross_game_result_not_projected']=True
        report['window']={'width':window.width(),'height':window.height(),'dpr':window.devicePixelRatioF()}
        targets=sorted({path for scope in scopes for event,path in scope.writes})
        assert targets and all(resources.validate(path).is_relative_to(resources.root) for path in targets)
        assert not any(scope.violations for scope in scopes)
        report['resource_boundary']={'sqlite':str(resources.db.db_path),'write_targets':targets,'violations':[]}
        assert protected_files()==protected_before
        report['game_and_deploy_resources_unchanged']=protected_before
        report['success']=True
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')

