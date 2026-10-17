"""Actual WebEngine batch selection -> SyncWorker -> original service -> resources/DAL."""
import hashlib
import json
import time
from pathlib import Path


def verify(window,r,requests):
    from PySide6.QtTest import QTest
    from services.business_resources import _scope
    scopes=[]
    def audit(event,args):
        scope=_scope.get()
        if scope is not None and scope.resources is r and not any(s is scope for s in scopes):scopes.append(scope)
    import sys
    sys.addaudithook(audit)
    output=r.path('evidence');output.mkdir()
    report={'responses':[],'stages':{}}
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    source=r.path('games/steamapps/workshop/content/1142710')
    def hashes(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    def protected():
        return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for prefix in ('config','games/Warhammer3','mod/Warhammer3/NativeWorkshop','mod/Warhammer3/CampaignUI') for p in r.path(prefix).rglob('*') if p.is_file()}
    baseline=protected()
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
        raise TimeoutError(condition+': '+str(js("$('workshop-status').textContent+' / '+$('registration-error').textContent")))
    def capture(name):
        QTest.qWait(120);window.grab().save(str(output/(name+'.png')))
        report['stages'][name]={'mods':[dict(row) for row in r.db._conn.execute('SELECT mod_id,internal_id,workspace_id,app_id,content_status,deploy_status,last_known_path FROM mods ORDER BY mod_id')],
            'tasks':json.loads(js('JSON.stringify(acquisitionData?.tasks||[])')),'source_hashes':hashes(source)}
    def open_scan():
        if not js("$('registration-dialog').open"):js("$('library-add').click();$('registration-kind').value='workshop';updateRegistrationFields()")
        js("$('workshop-scan').click()")
        wait('!!W.data && !W.busy && !!W.data.scan')
    def select(ids):
        js("$('workshop-none').click()")
        for wid in ids:
            js("{const box=document.querySelector('[data-workshop-id=\""+wid+"\"]');if(!box||box.disabled)throw Error('unavailable item');box.checked=true;box.dispatchEvent(new Event('change',{bubbles:true}));}")
    def sync(ids,name):
        open_scan();select(ids);before=hashes(source)
        js("$('workshop-start').click()")
        wait("!W.busy && !registrationBusy && $('workshop-status').textContent.includes('失败')")
        assert hashes(source)==before,'Business changed Workshop source'
        assert protected()==baseline,'Business changed protected deployments/order'
        capture(name)
        return json.loads(js('JSON.stringify(acquisitionData.tasks.filter(t=>t.action==="workshop-sync").at(-1))'))
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load()")
        wait("S.assets.length===4 && !$('refresh').disabled")
        open_scan();capture('01-discovery-preview')
        statuses=js('JSON.stringify(W.data.rows.map(r=>r.status))');assert all(kind in statuses for kind in ('new','existing','protected','source_missing','invalid'))
        select(['940001','940002']);before=hashes(source)
        js("$('workshop-start').click()")
        wait('W.busy');capture('02-original-worker-running')
        wait("!W.busy && !registrationBusy && S.assets.length===6")
        assert hashes(source)==before
        capture('03-batch-materialized-and-registered')
        first=next(a for a in window.bridge.adapter.assets(1142710) if a.workspace_id=='940001')
        second=next(a for a in window.bridge.adapter.assets(1142710) if a.workspace_id=='940002')
        for asset in (first,second):assert (Path(asset.managed_path)/'payload.pack').read_bytes()==('WORKSHOP CONTENT '+asset.workspace_id).encode()
        js("document.querySelector('[data-workshop-view=\""+first.internal_id+"\"]').click()")
        wait("!!S.details && S.current?.internal_id==="+json.dumps(first.internal_id))
        capture('04-library-real-steam-content')
        # Real user metadata UI and official cover ownership service, before update.
        js("document.querySelector('[data-command=edit]').click();$('edit-name').value='My Workshop name';$('edit-description').value='My protected description';$('edit-notes').value='My protected notes';$('edit-form').requestSubmit()")
        wait("!S.busy && !$('editor').open && S.current?.name==='My Workshop name'")
        js("$('favorite-action').click()")
        wait('!S.busy && !!S.current?.favorite')
        from PySide6.QtGui import QImage
        from services.importers.image_picker import apply_cover_to_mod
        image=QImage(5,5,QImage.Format.Format_RGB32);image.fill(0xff224466)
        cover=r.path('incoming/user-cover.png');cover.parent.mkdir(exist_ok=True);image.save(str(cover))
        with r.bind():cover_rel=apply_cover_to_mod(Path(first.managed_path),cover,mod_id=first.mod_pk,mark_user_override=True)
        cover_hash=hashlib.sha256((Path(first.managed_path)/cover_rel).read_bytes()).hexdigest()
        repeat=sync(['940001','940002'],'05-idempotent-repeat')
        assert all(item['state']=='skipped' for item in repeat['items']),repeat
        assert len(window.bridge.adapter.assets(1142710))==6
        partial=sync(['940003','940004'],'06-partial-failure-retains-success')
        assert partial['state']=='partial',partial
        assert {item['id']:item['state'] for item in partial['items']}=={'940003':'failed','940004':'success'}
        stub=next(a for a in window.bridge.adapter.assets(1142710) if a.workspace_id=='940010')
        (source/'940010').mkdir();(source/'940010/new.pack').write_bytes(b'NEW LOCAL WORKSHOP CONTENT')
        filled=sync(['940010'],'07-existing-identity-content-replenished')
        assert filled['state']=='success',filled
        stub_after=next(a for a in window.bridge.adapter.assets(1142710) if a.workspace_id=='940010')
        assert (stub.mod_pk,stub.internal_id)==(stub_after.mod_pk,stub_after.internal_id)
        assert (Path(stub_after.managed_path)/'new.pack').read_bytes()==b'NEW LOCAL WORKSHOP CONTENT'
        # Explicit fixture mutation models a legitimate updated local subscription.
        (source/'940001/payload.pack').write_bytes(b'UPDATED LOCAL WORKSHOP CONTENT')
        control=json.loads(r.path('workshop-response.json').read_text('utf8'));control['timestamps']['940001']=1800000000;r.path('workshop-response.json').write_text(json.dumps(control),encoding='utf8')
        updated=sync(['940001'],'08-old-update-policy-and-user-protection')
        assert updated['state']=='success' and updated['items'][0]['state']=='updated',updated
        live=next(a for a in window.bridge.adapter.assets(1142710) if a.workspace_id=='940001')
        assert (live.mod_pk,live.internal_id)==(first.mod_pk,first.internal_id)
        assert (Path(live.managed_path)/'payload.pack').read_bytes()==b'UPDATED LOCAL WORKSHOP CONTENT'
        info=r.db.get_mod_display_info(first.mod_pk)
        assert info.user_display_name=='My Workshop name' and info.custom_description=='My protected description' and info.user_notes=='My protected notes' and info.favorite
        assert info.cover_path==cover_rel and hashlib.sha256((Path(live.managed_path)/cover_rel).read_bytes()).hexdigest()==cover_hash
        # Cross-game UI navigation while the committed worker finishes remains on its original AppID.
        open_scan();select(['940001']);old_context=js('JSON.stringify({...registrationContext,scan:W.data.scan,ids:["940002"]})')
        js("$('workshop-start').click();$('registration-dialog').close();$('game').value='43';load()")
        wait("!W.busy && !registrationBusy && $('game').value==='43'")
        assert not window.bridge.adapter.assets(43)
        js('window.staleWorkshop=null;rpc("workshop-sync",'+old_context+',true).then(()=>window.staleWorkshop="BAD",e=>window.staleWorkshop=e.message)')
        wait('window.staleWorkshop!==null');assert js('window.staleWorkshop')!='BAD'
        report['cross_game_refused']=js('window.staleWorkshop')
        js("$('game').value='1142710';load()")
        wait("S.assets.length===7 && !$('refresh').disabled")
        js("$('acquisition-nav').click()")
        wait('!!acquisitionData?.tasks.some(t=>t.action==="workshop-sync")')
        capture('09-task-results-and-safe-game-switch')
        assert protected()==baseline
        report['source_http_requests']=requests;report['protected_hashes']=baseline
        report['business_write_targets']=sorted({path for scope in scopes for event,path in scope.writes})
        assert not any(scope.violations for scope in scopes)
        assert not any(Path(path).is_relative_to(source) for path in report['business_write_targets'])
        from core.db_manager import DatabaseManager
        db=DatabaseManager.open_existing(r.db.db_path,read_only=True)
        report['reopened']=[dict(row) for row in db._conn.execute('SELECT mod_id,internal_id,workspace_id,title,display_name,custom_description,user_notes,favorite,cover_path,user_override_fields,app_id,content_status,deploy_status,last_known_path FROM mods ORDER BY mod_id')]
        assert len(report['reopened'])==7;db.close()
        report['window']={'width':window.width(),'height':window.height(),'dpr':window.devicePixelRatioF()}
        from frontend.library_adapter import LibraryAdapter
        from frontend.web_window import WebLibraryWindow
        rodb=DatabaseManager.open_existing(r.db.db_path,read_only=True)
        original_window=window
        window=WebLibraryWindow(LibraryAdapter(rodb,writable=False));window.show()
        try:
            wait("typeof S!=='undefined' && !!S.snapshot")
            js("window.readonlySync=null;rpc('workshop-sync',{game:$('game').value,snapshot:S.snapshot,scan:'readonly',ids:['940001']},true).then(()=>window.readonlySync='BAD',e=>{window.readonlySync=e.message;$('game-caption').textContent=e.message;})")
            wait('window.readonlySync!==null')
            report['readonly_refused']=js('window.readonlySync');assert report['readonly_refused']!='BAD'
            capture('10-native-readonly-window-refusal')
        finally:
            while not window.bridge.shutdown():QTest.qWait(20);time.sleep(.01)
            window.close();window.deleteLater();QTest.qWait(50);rodb.close();window=original_window
        report['success']=True
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
