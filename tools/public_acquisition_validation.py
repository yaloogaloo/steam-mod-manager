"""Public Steam E2E and existing ZIP importer validation, with real Mod source."""
import hashlib
import json
import sys
import time
import zipfile


def prepare_source(resources):
    # Test input preparation only. This is NOT a product GitHub downloader.
    import requests
    url='https://codeload.github.com/chadvandy/mct_wh3/zip/refs/heads/main'
    session=requests.Session()
    session.trust_env=False
    archive=resources.path('incoming/mct_wh3-source.zip')
    with session.get(url,stream=True,timeout=(8,20),allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code!=200:raise RuntimeError('源 ZIP 不可用')
        total=0
        with archive.open('wb') as output:
            for block in response.iter_content(65536):
                total+=len(block)
                if total>64*1024*1024:raise ValueError('源 ZIP 超过限制')
                output.write(block)
    session.close()
    with resources.bind():
        game=resources.path('games/WH3')
        (game/'data').mkdir(parents=True)
        resources.db.update_game_deploy_config(1142710,name='Total War: WARHAMMER III',
            install_path=str(game),mod_path=str(game/'data'),deploy_type='warhammer3_pack',
            workshop_path=str(resources.path('games/steamapps/workshop/content/1142710')))
    resources.path('sample-provenance.json').write_text(json.dumps({
        'url':url,'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'note':'公开 Mod 源码 ZIP，仅用于准备本地导入输入；不代表 GitHub 下载已迁移'},ensure_ascii=False,indent=2),encoding='utf8')


def verify_window(window,resources):
    from PySide6.QtTest import QTest
    from services.business_resources import _scope
    output=resources.path('evidence')
    output.mkdir()
    scopes=[]
    def audit(event,args):
        scope=_scope.get()
        if scope is not None and scope.resources is resources and not any(s is scope for s in scopes):scopes.append(scope)
    sys.addaudithook(audit)
    report={'responses':[],'stages':{}}
    window.bridge.response.connect(lambda raw:report['responses'].append(json.loads(raw)))
    def js(code):
        values=[]
        window.page.runJavaScript(code,values.append)
        for _ in range(300):
            if values:return values[0]
            QTest.qWait(10);time.sleep(.005)
        raise TimeoutError(code)
    def wait(condition):
        for _ in range(1200):
            if js(condition):return
            QTest.qWait(15);time.sleep(.01)
        raise TimeoutError(condition+': '+str(js("$('acquisition-message').textContent")))
    def capture(name):
        window.grab().save(str(output/(name+'.png')))
        with resources.db._lock:report['stages'][name]=[dict(r) for r in resources.db._conn.execute('SELECT * FROM mods')]
    archive=resources.path('incoming/mct_wh3-source.zip')
    zip_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
    def form(path,wid='2927955021',title='MCT source import'):
        js("$('acquisition-add').click();$('registration-kind').value='local';updateRegistrationFields()")
        js("$('import-kind').value='zip';$('import-kind').dispatchEvent(new Event('change'));$('import-source').value="+json.dumps(str(path))+";$('import-workshop').value="+json.dumps(wid)+";$('import-title').value="+json.dumps(title)+";$('import-start').click();")
        wait("!registrationBusy && (!!registrationDraft || $('registration-error').textContent.startsWith('预览失败'))")
        if js("!!registrationDraft"):js("$('registration-confirm').click()")
    try:
        wait("typeof S!=='undefined' && !!S.snapshot")
        js("$('game').value='1142710';load();")
        wait("!$('refresh').disabled && $('game').value==='1142710'")
        js("$('acquisition-nav').click()")
        wait('!!acquisitionData?.sync_public')
        capture('01-public-acquisition')
        form(archive)
        wait("!!acquisitionData?.tasks.some(t=>t.action==='import-zip' && t.state==='running')")
        capture('02-zip-running')
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.tasks.some(t=>t.action==='import-zip' && t.state==='success')")
        capture('03-zip-success')
        with resources.db._lock:
            row=dict(resources.db._conn.execute('SELECT * FROM mods WHERE workspace_id=?',('2927955021',)).fetchone())
        pk=row['mod_id']
        folder=resources.validate(row['last_known_path'])
        actual={str(p.relative_to(folder)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file() and '.info' not in p.relative_to(folder).parts}
        with zipfile.ZipFile(archive) as zipped:
            expected={e.filename:hashlib.sha256(zipped.read(e)).hexdigest() for e in zipped.infolist() if not e.is_dir()}
        from collections import Counter
        assert actual and Counter(actual.values())==Counter(expected.values())
        report['import']={'internal_id':row['internal_id'],'mod_pk':pk,'workspace_id':row['workspace_id'],'archive':str(archive),'files':actual,'input_sha256':zip_hash}
        js("document.querySelector('[data-task-view]').click()")
        wait("S.space==='library' && !!S.details")
        capture('04-zip-library-detail')
        with resources.bind():
            resources.db.update_mod_user_metadata(pk,{'display_name':'My MCT name','custom_description':'Keep my description','user_notes':'Keep notes','favorite':True})
            from PySide6.QtGui import QImage
            image=QImage(32,32,QImage.Format_RGB32);image.fill(0x467c74)
            cover=folder/'.info/user-cover.png'
            assert image.save(str(cover),'PNG')
            resources.db.update_mod_cover_path(pk,'.info/user-cover.png')
            resources.db.set_user_override_field(pk,'cover')
        cover_hash=hashlib.sha256(cover.read_bytes()).hexdigest()
        js("$('acquisition-nav').click();")
        form(archive)
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.tasks.some(t=>t.state==='duplicate')")
        wait("acquisitionData.candidates.some(a=>a.internal_id==="+json.dumps(row['internal_id'])+")")
        js("$('sync-mod').value=acquisitionData.candidates.find(a=>a.internal_id==="+json.dumps(row['internal_id'])+").token;$('sync-start').click()")
        wait("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='running')")
        capture('05-public-sync-running')
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.tasks.some(t=>t.action==='source-sync' && t.state==='success')")
        capture('06-public-sync-success')
        with resources.db._lock:after=dict(resources.db._conn.execute('SELECT * FROM mods WHERE mod_id=?',(pk,)).fetchone())
        assert after['title']=='Mod Configuration Tool - v0.9 Beta' and after['description']
        assert after['display_name']=='My MCT name' and after['custom_description']=='Keep my description'
        assert after['user_notes']=='Keep notes' and after['favorite']==1
        assert after['cover_path']=='.info/user-cover.png' and hashlib.sha256(cover.read_bytes()).hexdigest()==cover_hash
        assert after['internal_id']==row['internal_id'] and after['app_id']==1142710
        assert resources.db.is_official_metadata_synced(pk)
        assert hashlib.sha256(archive.read_bytes()).hexdigest()==zip_hash
        assert all(hashlib.sha256((folder/name).read_bytes()).hexdigest()==digest for name,digest in actual.items())
        report['public_sync']={'title':after['title'],'description_sha256':hashlib.sha256(after['description'].encode()).hexdigest(),'user_fields_and_cover_preserved':True,'payload_unchanged':True}
        form(archive,wid='9999999999',title='Source failure verification')
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("acquisitionData.candidates.some(a=>a.name==='Source failure verification')")
        js("$('sync-mod').value=acquisitionData.candidates.find(a=>a.name==='Source failure verification').token;$('sync-start').click()")
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("$('acquisition-message').textContent.startsWith('操作失败：')")
        capture('07-public-source-failure')
        with resources.db._lock:
            missing=dict(resources.db._conn.execute('SELECT * FROM mods WHERE workspace_id=?',('9999999999',)).fetchone())
        assert not missing['official_metadata_synced']
        report['public_missing_source_rejected']=True
        bad=resources.path('incoming/unsafe.zip')
        with zipfile.ZipFile(bad,'w') as zipped:zipped.writestr('../escape.txt','NO')
        form(bad,wid='990799',title='Unsafe ZIP')
        wait('!acquisitionSubmitting && !registrationBusy')
        wait("$('acquisition-message').textContent.startsWith('操作失败：')")
        capture('08-zip-failure')
        assert not (resources.root/'escape.txt').exists()
        report['network']=[event for scope in scopes for event in scope.network]
        assert any(event.get('status')==200 for event in report['network'])
        report['writes']=sorted({path for scope in scopes for event,path in scope.writes})
        assert all(resources.validate(path).is_relative_to(resources.root) for path in report['writes'])
        report['violations']=[event for scope in scopes for event in scope.violations]
        assert not report['violations'],report['violations']
        report['success']=True
    finally:
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf8')
