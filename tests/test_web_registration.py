"""Real source importers, same-identity association and content authority."""
import json
from pathlib import Path
import zipfile
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_order_sandbox import initialize

@pytest.fixture
def setup(tmp_path):
    app=QApplication.instance() or QApplication([])
    r=initialize(tmp_path/'registration');r.path('incoming').mkdir()
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    result=b.execute('library',{'game':1142710});a={'game':'1142710','snapshot':result['snapshot']}
    yield app,r,b,a
    b.shutdown();r.db.close()

def register(b,a,platform='Nexus',url='https://www.nexusmods.com/totalwarwarhammer3/mods/91001',**extra):
    draft=b.execute('registration-preview',{**a,'kind':'url','platform':platform,'url':url,'title':'Registered without payload',**extra})
    return b.execute_command('registration-save',{**a,'draft':draft['draft']})

def context(b,iid):
    result=b.execute('library',{'game':1142710});asset=next(a for a in result['assets'] if a['internal_id']==iid)
    return {'game':'1142710','snapshot':result['snapshot'],'token':asset['token'],'internal_id':iid}


@pytest.mark.parametrize('platform,url',[('Steam','902778'),('Nexus','91009'),('GitHub','https://github.com/SteamRE/DepotDownloader'),('mod.io','https://mod.io/g/test/m/example'),('mod.io','91010'),('Other','')])
def test_original_optional_title_is_not_required_by_new_frontend(setup,platform,url):
    _,r,b,a=setup
    game=1086940 if platform=='mod.io' else 1142710
    if game!=1142710:
        r.db.update_game_deploy_config(game,name="Baldur's Gate 3",deploy_type='folder_copy')
        snapshot=b.execute('library',{'game':game});a={'game':str(game),'snapshot':snapshot['snapshot']}
    result=register(b,a,platform=platform,url=url,title='')
    row=r.db.get_mod_backup_row(result['mod_pk'])
    assert Path(result['managed_path']).is_dir()
    assert row['internal_id']==result['internal_id']
    assert b.adapter.verify(next(asset for asset in b.adapter.assets(game) if asset.internal_id==result['internal_id'])).display_name
    source=r.path('incoming/Optional local name');source.mkdir();(source/'payload.txt').write_text('real payload')
    preview=b.execute('registration-preview',{**a,'kind':'local','platform':'Other','format':'folder','source':str(source),'title':''})
    assert preview['has_content'] and preview['title']==''


@pytest.mark.parametrize('platform,url',[('Steam','902777'),('Nexus','91007'),('Nexus','https://www.nexusmods.com/totalwarwarhammer3/mods/91008?tab=files'),('GitHub','https://github.com/chadvandy/mct_wh3'),('Other','https://example.org/mods/local-reference'),('Other','')])
def test_original_github_and_other_importers_register_without_payload(setup,platform,url):
    _,r,b,a=setup
    first=register(b,a,platform=platform,url=url)
    assert first['content_status']=='content_missing'
    row=r.db.get_mod_backup_row(first['mod_pk'])
    assert row['platform'].lower()==platform.lower()
    assert Path(first['managed_path']).is_dir()
    if url:
        result=register(b,a,platform=platform,url=url)
        assert result['duplicate'] and result['internal_id']==first['internal_id']
    else:
        second=register(b,a,platform=platform,url=url)
        assert second['internal_id']!=first['internal_id']

def test_empty_register_add_zip_and_same_identity(setup):
    _,r,b,a=setup;result=register(b,a);pk=result['mod_pk'];iid=result['internal_id'];path=Path(result['managed_path'])
    assert result['content_status']=='content_missing'
    assert path.is_dir() and not list(path.glob('*.zip'))
    args=context(b,iid)
    r.db.update_mod_user_metadata(pk,{'display_name':'My title','custom_description':'My description','user_notes':'My notes','favorite':True})
    from PySide6.QtGui import QImage
    image=QImage(3,3,QImage.Format.Format_RGB32);image.fill(0xff224455)
    cover=r.path('incoming/my-cover.png');image.save(str(cover))
    from services.importers.image_picker import apply_cover_to_mod
    with r.bind():cover_rel=apply_cover_to_mod(path,cover,mod_id=pk,mark_user_override=True)
    cover_bytes=(path/cover_rel).read_bytes()
    payload=r.path('incoming/content.zip')
    with zipfile.ZipFile(payload,'w') as z:z.writestr('data/unit.pack',b'ACTUAL CONTENT')
    added=b.execute_command('content-add',{**args,'source':str(payload)})
    assert added['content_status']=='healthy' and added['internal_id']==iid
    assert (path/'content.zip').read_bytes()==payload.read_bytes()
    assert str(r.db.find_mod_by_internal_id(iid))==pk
    info=r.db.get_mod_display_info(pk)
    assert info.user_display_name=='My title' and info.favorite
    assert info.cover_path==cover_rel and (path/cover_rel).read_bytes()==cover_bytes
    assert r.db.get_user_override_fields(pk).get('cover')
    assert b.execute_command('content-rescan',args)['content_status']=='healthy'
    with pytest.raises(ValueError,match='覆盖'):b.execute_command('content-add',{**args,'source':str(payload)})


@pytest.mark.parametrize('platform,expected',[('GitHub','github'),('Other','other'),('mod.io','modio')])
def test_content_association_keeps_original_platform_file_semantics(setup,platform,expected):
    _,r,b,a=setup
    result=register(b,a)
    r.db.update_mod_user_metadata(result['mod_pk'],{'platform':platform})
    args=context(b,result['internal_id'])
    payload=r.path('incoming/source-content.zip')
    with zipfile.ZipFile(payload,'w') as z:z.writestr('real.pack',b'REAL SOURCE CONTENT')
    added=b.execute_command('content-add',{**args,'source':str(payload)})
    assert added['internal_id']==result['internal_id'] and added['content_status']=='healthy'
    files=r.db.get_mod_files(result['mod_pk']).files
    assert files and all(entry.source_type==expected for entry in files)
    assert (Path(result['managed_path'])/payload.name).read_bytes()==payload.read_bytes()

def test_offline_registration_duplicate_and_manual_recognition(setup):
    _,r,b,a=setup
    page=r.path('incoming/captured.html');page.write_text('<html><head><link rel="canonical" href="https://www.nexusmods.com/totalwarwarhammer3/mods/91002"><meta property="og:title" content="Offline Nexus Mod"><meta property="og:description" content="Saved description"></head><body><script>throw Error("must never execute")</script></body></html>',encoding='utf8')
    draft=b.execute('registration-preview',{**a,'kind':'offline','source':str(page)})
    result=b.execute_command('registration-save',{**a,'draft':draft['draft']});args=context(b,result['internal_id'])
    path=Path(result['managed_path']);(path/'saved.html').write_text('<html>not payload</html>');(path/'cover.png').write_bytes(b'cover only')
    assert b.execute_command('content-rescan',args)['content_status']=='content_missing'
    (path/'manual.pack').write_bytes(b'MANUAL CONTENT')
    assert b.execute_command('content-rescan',args)['content_status']=='healthy'
    count=r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]
    current={k:args[k] for k in ('game','snapshot')}
    repeat=register(b,current,url='https://www.nexusmods.com/totalwarwarhammer3/mods/91002')
    assert repeat['duplicate'] and repeat['internal_id']==result['internal_id']
    assert r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]==count

def test_invalid_stale_and_failed_association(setup):
    _,r,b,a=setup
    for url in ('http://www.nexusmods.com/game/mods/1','https://www.nexusmods.com.evil/game/mods/1','https://www.nexusmods.com/game/mods/1?redirect=http://127.0.0.1'):
        with pytest.raises(ValueError):register(b,a,url=url)
    draft=b.execute('registration-preview',{**a,'kind':'url','platform':'Nexus','url':'https://www.nexusmods.com/totalwarwarhammer3/mods/91003','title':'Stale stub'})
    b.execute('library',{'game':43})
    with pytest.raises(RuntimeError):b.execute_command('registration-save',{**a,'draft':draft['draft']})


def test_readonly_and_database_failure_compensation(setup):
    _,r,b,a=setup
    draft=b.execute('registration-preview',{**a,'kind':'url','platform':'Nexus','url':'https://www.nexusmods.com/totalwarwarhammer3/mods/91004','title':'Failure rollback'})
    count=r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]
    r.db._conn.execute("CREATE TEMP TRIGGER fail_registration BEFORE INSERT ON mods BEGIN SELECT RAISE(ABORT,'registration rejected'); END")
    with pytest.raises(Exception):b.execute_command('registration-save',{**a,'draft':draft['draft']})
    assert r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]==count
    r.db._conn.execute('DROP TRIGGER fail_registration')
    result=register(b,a,url='https://www.nexusmods.com/totalwarwarhammer3/mods/91005')
    args=context(b,result['internal_id']);pk=result['mod_pk'];path=Path(result['managed_path'])
    source=r.path('incoming/source');source.mkdir();(source/'real.pack').write_bytes(b'REAL')
    r.db._conn.execute("CREATE TEMP TRIGGER fail_content BEFORE UPDATE OF content_status ON mods WHEN NEW.content_status='healthy' BEGIN SELECT RAISE(ABORT,'content rejected'); END")
    with pytest.raises(RuntimeError,match='持久化'):b.execute_command('content-add',{**args,'source':str(source)})
    assert not (path/'real.pack').exists()
    assert str(r.db.find_mod_by_internal_id(result['internal_id']))==str(pk)
    r.db._conn.execute('DROP TRIGGER fail_content')
    from core.db_manager import DatabaseManager
    ro_db=DatabaseManager.open_existing(r.db.db_path,read_only=True)
    ro=LibraryBridge(LibraryAdapter(ro_db))
    snap=ro.execute('library',{'game':1142710});roargs={'game':'1142710','snapshot':snap['snapshot']}
    with pytest.raises(PermissionError):ro.execute('registration-preview',{**roargs,'kind':'url'})
    for action in ('registration-save','content-add','content-rescan','content-open'):
        with pytest.raises(PermissionError):ro.execute_command(action,roargs)
    ro.shutdown();ro_db.close()


def test_post_identity_failure_quarantines_only_owned_folder(setup):
    _,r,b,a=setup
    count=r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]
    r.db._conn.execute("CREATE TEMP TRIGGER fail_new_content BEFORE UPDATE OF content_status ON mods WHEN NEW.content_status='content_missing' BEGIN SELECT RAISE(ABORT,'new content denied'); END")
    with pytest.raises(RuntimeError):register(b,a,url='https://www.nexusmods.com/totalwarwarhammer3/mods/91006')
    assert r.db._conn.execute('SELECT COUNT(*) FROM mods').fetchone()[0]==count
    assert list(r.path('cache/registration_failed').rglob('metadata.json'))
    assert not list(r.path('mod').rglob('Registered without payload'))


def test_duplicate_submit_and_stale_preview_content(setup):
    app,r,b,a=setup
    result=register(b,a);args=context(b,result['internal_id'])
    messages=[];b.response.connect(lambda raw:messages.append(json.loads(raw)))
    with r.db._lock:
        b.command('first-rescan','content-rescan',json.dumps(args))
        b.command('duplicate-rescan','content-rescan',json.dumps(args))
    import time
    end=time.monotonic()+5
    while len(messages)<2 and time.monotonic()<end:app.processEvents();time.sleep(.01)
    assert len(messages)==2 and sum('error' in message for message in messages)==1
    source=r.path('incoming/new.zip')
    with zipfile.ZipFile(source,'w') as z:z.writestr('unit.pack',b'CONTENT')
    current={k:args[k] for k in ('game','snapshot')}
    draft=b.execute('registration-preview',{**current,'kind':'local','platform':'Steam','format':'zip','source':str(source),'title':'Local preview','workshop_id':'91007'})
    with zipfile.ZipFile(source,'a') as z:z.writestr('extra.pack',b'CHANGED')
    from frontend.registration import confirm_local
    with pytest.raises(ValueError,match='预览'):
        confirm_local(b,{**current,'registration_draft':draft['draft'],'source':str(source),'title':'Local preview','workshop_id':'91007'})


def test_directory_cannot_bypass_unapproved_archive_admission(setup):
    _,r,b,a=setup
    result=register(b,a);args=context(b,result['internal_id'])
    source=r.path('incoming/unsupported');source.mkdir();(source/'unsafe.7z').write_bytes(b'UNAPPROVED')
    with pytest.raises(ValueError,match='7z/RAR'):b.execute_command('content-add',{**args,'source':str(source)})
    assert not (Path(result['managed_path'])/'unsafe.7z').exists()


@pytest.mark.parametrize('game',[1142710,281990,916440,1086940,413150])
def test_manual_sources_are_game_independent(setup,game):
    _,r,b,_=setup
    from core.mod_platform import get_available_sources
    assert {p for p,label in get_available_sources('Any game',game)}=={'steam','nexus','modio','github','other'}
    r.db.update_game_deploy_config(game,name='Manual source game',deploy_type='folder_copy')
    snapshot=b.execute('library',{'game':game});args={'game':str(game),'snapshot':snapshot['snapshot']}
    result=register(b,args,platform='mod.io',url='https://mod.io/g/manual/m/controlled-source')
    assert r.db.get_mod_display_info(result['mod_pk']).app_id==game
