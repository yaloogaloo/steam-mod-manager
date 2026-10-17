"""Real ImportWorker integration and admission/lifecycle failure boundaries."""
import json
import time
from threading import Event
import pytest
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from services.business_resources import BusinessResources
from tools.isolated_deploy_validation import initialize
from ui.import_thread import ImportWorker


@pytest.fixture
def acquisition(tmp_path):
    app=QApplication.instance() or QApplication([])
    resources,_,_,_=initialize(tmp_path/'acquisition')
    source=resources.path('incoming/Original')
    source.mkdir(parents=True)
    (source/'payload.txt').write_text('REAL IMPORT',encoding='utf8')
    bridge=LibraryBridge(LibraryAdapter(resources.db,writable=True),resources=resources)
    yield app,bridge,resources,source
    while not bridge.shutdown():app.processEvents();time.sleep(.01)
    resources.db.close()


def wait(app,condition):
    end=time.monotonic()+15
    while not condition():
        app.processEvents();time.sleep(.01)
        assert time.monotonic()<end,'worker timeout'


def args_for(bridge,source):
    snapshot=bridge.execute('library',{'game':42})
    return {'game':'42','snapshot':snapshot['snapshot'],'source':str(source),'workshop_id':'990701','title':'Actual imported Mod'}


def command(app,bridge,action,args):
    messages=[]
    bridge.response.connect(lambda raw:messages.append(json.loads(raw)))
    rid='cmd-'+str(time.monotonic_ns())
    bridge.command(rid,action,json.dumps(args))
    wait(app,lambda:any(m['request']==rid for m in messages))
    return next(m for m in messages if m['request']==rid)


def test_real_import_duplicate_and_invalid_recovery(acquisition):
    app,bridge,resources,source=acquisition
    args=args_for(bridge,source)
    assert 'error' in command(app,bridge,'import-folder',{**args,'source':str(source.parent)})
    result=command(app,bridge,'import-folder',args)['result']
    assert result['state']=='success'
    assert (resources.validate(result['managed_path'])/'payload.txt').read_text('utf8')=='REAL IMPORT'
    assert str(resources.db.find_mod_by_internal_id(result['internal_id']))==result['mod_pk']
    result=command(app,bridge,'import-folder',args)['result']
    assert result['state']=='duplicate'
    assert len(resources.db.list_mod_list_items(game_id=42))==2


def test_selected_cover_is_archived_and_changed_attachment_rejected(acquisition):
    from PySide6.QtGui import QImage,QColor
    app,bridge,resources,source=acquisition
    cover=resources.path('incoming/chosen-cover.png')
    image=QImage(48,32,QImage.Format_RGB32);image.fill(QColor(30,90,120));assert image.save(str(cover))
    args={**args_for(bridge,source),'kind':'local','platform':'Other','url':'','format':'folder','cover_source':str(cover)}
    preview=bridge.execute('registration-preview',args)
    assert preview['attachments']['cover_source']==cover.name
    image.fill(QColor(90,30,120));assert image.save(str(cover))
    changed=command(app,bridge,'import-folder',{**args,'registration_draft':preview['draft']})
    assert 'error' in changed
    preview=bridge.execute('registration-preview',args)
    reply=command(app,bridge,'import-folder',{**args,'registration_draft':preview['draft']})
    assert 'error' not in reply,reply
    info=resources.db.get_mod_display_info(reply['result']['mod_pk'])
    from services.importers.image_picker import resolve_cover_file
    cover_file=resolve_cover_file(reply['result']['managed_path'],info.cover_path)
    assert cover_file and resources.validate(cover_file).is_file()
    assert cover.is_file() and (source/'payload.txt').read_text('utf8')=='REAL IMPORT'


@pytest.mark.parametrize('platform,url',[('GitHub','https://github.com/smm-acceptance/local-mod'),('Nexus','https://www.nexusmods.com/testgame/mods/990703'),('Other',''),('mod.io','https://mod.io/g/anno-1800/m/acceptance-fixture')])
@pytest.mark.parametrize('zipped',[False,True])
def test_original_nonsteam_import_workers(acquisition,platform,url,zipped):
    import zipfile
    app,bridge,resources,source=acquisition
    if zipped:
        source=resources.path('incoming/local-content.zip')
        with zipfile.ZipFile(source,'w') as archive:archive.writestr('actual-data.txt','ARCHIVED CONTENT')
    args={**args_for(bridge,source),'kind':'local','platform':platform,'url':url,'format':'zip' if zipped else 'folder'}
    if platform=='mod.io':
        resources.db.update_game_deploy_config(916440,name='Anno 1800')
        snapshot=bridge.execute('library',{'game':916440})
        args.update(game='916440',snapshot=snapshot['snapshot'])
    preview=bridge.execute('registration-preview',args)
    reply=command(app,bridge,'import-zip' if zipped else 'import-folder',{**args,'source':source.as_posix(),'registration_draft':preview['draft']})
    assert 'error' not in reply,reply
    result=reply['result'];assert result['state']=='success'
    row=resources.db.get_mod_backup_row(result['mod_pk'])
    from core.mod_platform import normalize_platform
    assert normalize_platform(row['platform'])==normalize_platform(platform)
    target=resources.validate(result['managed_path'])
    if zipped:assert (target/source.name).read_bytes()==source.read_bytes()
    else:assert (target/'payload.txt').read_text('utf8')=='REAL IMPORT'


def test_nonsteam_multi_archive_retains_each_original_container(acquisition):
    import py7zr,zipfile
    app,bridge,resources,source=acquisition
    first=resources.path('incoming/release.zip');second=resources.path('incoming/source.7z')
    with zipfile.ZipFile(first,'w') as archive:archive.writestr('release.pack',b'RELEASE CONTENT')
    with py7zr.SevenZipFile(second,'w') as archive:archive.write(source/'payload.txt','source.txt')
    args={**args_for(bridge,first),'kind':'local','platform':'GitHub','url':'https://github.com/smm-acceptance/multiple-containers','format':'zip','archive_paths':[first.as_posix(),second.as_posix()]}
    preview=bridge.execute('registration-preview',args)
    reply=command(app,bridge,'import-zip',{**args,'registration_draft':preview['draft']})
    assert 'error' not in reply,reply
    result=reply['result'];target=resources.validate(result['managed_path'])
    assert (target/first.name).read_bytes()==first.read_bytes()
    assert (target/second.name).read_bytes()==second.read_bytes()
    assert len(resources.db.get_mod_files(result['mod_pk']).files)==2


def test_original_directory_batch_preserves_success_when_one_import_raises(acquisition,monkeypatch):
    from services.importers.other import OtherImporter
    app,bridge,resources,source=acquisition
    root=resources.path('incoming/Batch');root.mkdir()
    for name in ('First','Bad','Last'):
        folder=root/name;folder.mkdir();(folder/'content.pack').write_bytes(name.encode())
    original=OtherImporter.import_mod
    def fail_one(self,**kwargs):
        if kwargs.get('title')=='Bad':raise OSError('single item disk failure')
        return original(self,**kwargs)
    monkeypatch.setattr(OtherImporter,'import_mod',fail_one)
    args={**args_for(bridge,root),'kind':'local','platform':'Other','url':'','title':'','format':'batch','batch_directory':True}
    preview=bridge.execute('registration-preview',args)
    assert len(preview['batch_candidates'])==3
    assert preview['title']=='3 个 Mod · 使用各目录名称'
    assert '每次导入都会创建新的 Mod' in preview['metadata_note']
    reply=command(app,bridge,'import-folder',{**args,'registration_draft':preview['draft']})
    assert 'error' not in reply,reply
    assert reply['result']['state']=='partial'
    assert len(reply['result']['items'])==3
    assert sum(i['state']=='success' for i in reply['result']['items'])==2
    assert sum(i['state']=='failed' for i in reply['result']['items'])==1
    rows=resources.db.list_mod_list_items(game_id=42)
    assert any(r['name']=='First' for r in rows) and any(r['name']=='Last' for r in rows)


def test_stale_game_and_readonly_refuse_all_actions(acquisition):
    app,bridge,resources,source=acquisition
    args=args_for(bridge,source)
    bridge.execute('library',{'game':43})
    assert 'error' in command(app,bridge,'import-folder',args)
    readonly=DatabaseManager.open_existing(resources.db.db_path,read_only=True)
    ro=LibraryBridge(LibraryAdapter(readonly))
    try:
        for action in ('import-folder','import-zip','source-sync','pick-import-folder','pick-import-zip'):
            assert 'error' in command(app,ro,action,args)
        assert len(resources.db.list_mod_list_items(game_id=42))==1
    finally:ro.shutdown();readonly.close()


def test_real_database_import_failure_not_reported_success(acquisition):
    app,bridge,resources,source=acquisition
    args=args_for(bridge,source)
    resources.db._conn.execute("CREATE TEMP TRIGGER deny_import BEFORE INSERT ON mods BEGIN SELECT RAISE(ABORT,'import db failure'); END")
    failed=command(app,bridge,'import-folder',args)
    assert 'error' in failed
    assert not list(resources.path('mod').rglob('Actual imported Mod'))
    resources.db._conn.rollback()
    resources.db._conn.execute('DROP TRIGGER deny_import')
    assert command(app,bridge,'import-folder',args)['result']['state']=='success'


def test_worker_stale_preflight_and_close_wait(acquisition):
    app,bridge,resources,source=acquisition
    args=args_for(bridge,source)
    gate=Event()
    entered=Event()
    errors=[]
    def preflight():
        entered.set()
        gate.wait(5)
        bridge._command_game(args)
    worker=ImportWorker(platform='Steam',library_root=resources.path('mod'),
        params={'source_path':str(source),'workshop_id':'990702','title':'Must not import','app_id':42},
        resources=resources,preflight=preflight)
    worker.import_failed.connect(errors.append)
    bridge._jobs['held']=worker
    worker.start()
    wait(app,entered.is_set)
    bridge.execute('library',{'game':43})
    assert not bridge.shutdown()
    gate.set()
    wait(app,lambda:not worker.isRunning())
    wait(app,lambda:bool(errors))
    assert '快照' in errors[0]
    assert len(resources.db.list_mod_list_items(game_id=42))==1
    bridge._jobs.clear()


def test_fixture_endpoint_and_external_path_fail_closed(acquisition):
    app,bridge,resources,source=acquisition
    for url in ('https://steamcommunity.com/steam/details','http://localhost:5000/steam/details','http://127.0.0.1:5000/anything','http://127.0.0.1:5000/steam/details?redirect=x'):
        with pytest.raises(PermissionError):BusinessResources(resources.root,resources.db,steam_api_url=url)
    args=args_for(bridge,source)
    assert 'error' in command(app,bridge,'import-folder',{**args,'source':str(resources.root.parent)})
    for forbidden in ('.INFO','data.zip'):
        path=source/forbidden
        path.mkdir() if forbidden=='.INFO' else path.write_bytes(b'archive')
        assert 'error' in command(app,bridge,'import-folder',args)
        path.rmdir() if path.is_dir() else path.unlink()


def test_started_import_finishes_before_close(acquisition):
    app,bridge,resources,source=acquisition
    args=args_for(bridge,source)
    entered,release=Event(),Event()
    results=[]
    def preflight():
        bridge._command_game(args)
        entered.set()
        release.wait(5)
    worker=ImportWorker(platform='Steam',library_root=resources.path('mod'),
        params={'source_path':str(source),'workshop_id':'990703','title':'Finish on close','app_id':42,'game_name':'Game'},
        resources=resources,preflight=preflight)
    worker.import_finished.connect(results.append)
    bridge._jobs['actual-import']=worker
    worker.start()
    wait(app,entered.is_set)
    assert not bridge.shutdown()
    release.set()
    wait(app,lambda:not worker.isRunning())
    wait(app,lambda:bool(results))
    assert results[0].success
    row=resources.db.get_mod_backup_row(results[0].mod_id)
    assert (resources.validate(row['last_known_path'])/'payload.txt').read_text('utf8')=='REAL IMPORT'
    bridge._jobs.clear()
    assert bridge.shutdown()


def test_zip_real_import_deployed_duplicate_does_not_replace(acquisition):
    import zipfile
    import hashlib
    from services.deploy import ModDeployer
    app,bridge,resources,source=acquisition
    archive=source.parent/'mod.zip'
    with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr('script/mod.lua',b'ORIGINAL MOD SCRIPT')
    args=args_for(bridge,archive)
    imported=command(app,bridge,'import-zip',args)['result']
    assert imported['state']=='success'
    folder=resources.validate(imported['managed_path'])
    assert (folder/'mod.lua').read_bytes()==b'ORIGINAL MOD SCRIPT'
    deployer=ModDeployer(library_root=resources.path('mod'),db=resources.db,resources=resources)
    assert deployer.deploy_mod(imported['internal_id'])['success']
    before={str(p.relative_to(resources.root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for name in ('mod','games','data/deploy_backup','data/asset_store')
        for p in resources.path(name).rglob('*') if p.is_file()}
    with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr('script/mod.lua',b'UNREQUESTED REPLACEMENT')
    assert command(app,bridge,'import-zip',args)['result']['state']=='duplicate'
    assert resources.db.get_mod_deploy_info(imported['mod_pk']).deploy_status=='deployed'
    after={str(p.relative_to(resources.root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for name in ('mod','games','data/deploy_backup','data/asset_store')
        for p in resources.path(name).rglob('*') if p.is_file()}
    assert before==after


@pytest.mark.parametrize('entry',['../escape.txt','/absolute.txt','C:/escape.txt','dir/../escape.txt',
    'dir/file:stream','CON.txt','.INFO/metadata.json','dir /file.lua'])
def test_zip_path_rejection_no_identity_creation(acquisition,entry):
    import zipfile
    app,bridge,resources,source=acquisition
    archive=source.parent/'bad.zip'
    with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr(entry,'BAD')
    assert 'error' in command(app,bridge,'import-zip',args_for(bridge,archive))
    assert len(resources.db.list_mod_list_items(game_id=42))==1
    assert not resources.path('cache/import_cache').exists()


def test_zip_corrupt_bomb_link_and_case_collision_rejected(acquisition):
    import zipfile
    from frontend.acquisition import validate_zip
    app,bridge,resources,source=acquisition
    archive=source.parent/'bad.zip'
    archive.write_bytes(b'not a zip')
    assert 'error' in command(app,bridge,'import-zip',args_for(bridge,archive))
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as zipped:zipped.writestr('bomb.txt',b'A'*1024*1024)
    with pytest.raises(ValueError,match='压缩比例'):validate_zip(archive)
    with zipfile.ZipFile(archive,'w') as zipped:
        info=zipfile.ZipInfo('link');info.external_attr=0o120777<<16;zipped.writestr(info,'outside')
    with pytest.raises(ValueError,match='文件类型'):validate_zip(archive)
    with zipfile.ZipFile(archive,'w') as zipped:
        zipped.writestr('mod.lua','a');zipped.writestr('MOD.lua','b')
    with pytest.raises(ValueError,match='重名'):validate_zip(archive)


def test_public_dns_and_socket_boundary(acquisition,monkeypatch):
    import socket
    app,bridge,resources,source=acquisition
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,'',('127.0.0.1',443))])
    with pytest.raises(PermissionError,match='非公网'):BusinessResources(resources.root,resources.db,steam_public=True)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,'',('8.8.8.8',443))])
    public=BusinessResources(resources.root,resources.db,steam_public=True)
    with pytest.raises(PermissionError):
        with public.bind():
            with socket.socket() as connection:connection.connect(('127.0.0.1',443))


@pytest.mark.parametrize('fault',['redirect','oversize','404','429'])
def test_public_http_failure_guards(acquisition,monkeypatch,fault):
    """Negative transport guards only; public success is real Windows E2E."""
    import socket
    import requests
    from core.steam_api import SteamWorkshopClient, STEAM_PUBLISHED_FILE_DETAILS_URL
    app,bridge,resources,source=acquisition
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,0,'',('8.8.8.8',443))])
    public=BusinessResources(resources.root,resources.db,steam_public=True)
    seen=[]
    def denied_response(session,method,url,**kwargs):
        seen.append(kwargs)
        response=requests.Response()
        response.status_code=302 if fault=='redirect' else int(fault) if fault.isdigit() else 200
        response.url=url
        response._content=b'guard test'
        response._content_consumed=True
        response.headers['Location']='http://127.0.0.1/private'
        if fault=='oversize':response.iter_content=lambda *a,**k:iter([b'x'*(2*1024*1024+1)])
        return response
    monkeypatch.setattr(requests.Session,'request',denied_response)
    with public.bind():
        with SteamWorkshopClient(db=resources.db,proxy_url='',enable_scrape_fallback=False) as client:
            with pytest.raises((PermissionError,ValueError,requests.HTTPError)) as failed:
                client._request_without_adapter_retries('POST',STEAM_PUBLISHED_FILE_DETAILS_URL,timeout=(1,2),data={'itemcount':1})
            assert seen[0]['allow_redirects'] is False and seen[0]['stream'] is True
            if fault.isdigit():
                from core.steam_api import _format_steam_network_error
                assert fault in _format_steam_network_error(failed.value,url=STEAM_PUBLISHED_FILE_DETAILS_URL,proxy_enabled=False)
