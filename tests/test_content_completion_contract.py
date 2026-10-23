from pathlib import Path
import zipfile
import pytest
from tests.test_web_registration import setup,register,context
from services.content_completion import prepare

def package(r,name,member='data/unit.pack',data=b'actual payload'):
    path=r.path('incoming/'+name)
    with zipfile.ZipFile(path,'w') as z:z.writestr(member,data)
    return path

def test_multiple_zip_one_identity_preserves_metadata(setup):
    _,r,b,a=setup;result=register(b,a);pk=result['mod_pk'];args=context(b,result['internal_id'])
    r.db.update_mod_user_metadata(pk,{'display_name':'keep','favorite':True,'user_notes':'notes'})
    paths=[package(r,'one.zip'),package(r,'two.zip','textures/new.dds')]
    added=b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    root=Path(result['managed_path'])
    assert added['files']==2 and added['content_status']=='healthy'
    assert all((root/p.name).read_bytes()==p.read_bytes() for p in paths)
    assert r.db.get_mod_display_info(pk).user_display_name=='keep'
    assert r.db.get_mod_display_info(pk).favorite
    assert str(r.db.find_mod_by_internal_id(result['internal_id']))==pk

@pytest.mark.parametrize('member',['../escape.pack','C:/escape.pack','CON.txt','.info/type.json'])
def test_all_inputs_rejected_before_publish(setup,member):
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    one=package(r,'one.zip');two=package(r,'two.zip',member)
    with pytest.raises(ValueError):b.execute_command('content-add',{**args,'archive_paths':[str(one),str(two)]})
    assert not list(Path(result['managed_path']).glob('*.zip'))
    assert r.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(result['mod_pk'],)).fetchone()[0]=='content_missing'

def test_7z_and_zip_retained_as_original_source_units(setup):
    import py7zr
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    payload=r.path('incoming/real.pack');payload.write_bytes(b'real content')
    seven=r.path('incoming/two.7z')
    with py7zr.SevenZipFile(seven,'w') as z:z.write(payload,arcname='other/real.pack')
    one=package(r,'one.zip')
    result=b.execute_command('content-add',{**args,'archive_paths':[str(one),str(seven)]})
    assert result['files']==2 and result['content_status']=='healthy'

def test_cancel_and_corruption_remove_owned_stage(setup):
    _,r,b,a=setup;source=package(r,'one.zip');calls=0
    def cancel():
        nonlocal calls
        calls+=1
        if calls>4:raise RuntimeError('cancel')
    with r.bind(),pytest.raises(RuntimeError,match='cancel'):
        with prepare(r,[source],platform='Steam',cancel_check=cancel):pytest.fail('must not publish')
    bad=r.path('incoming/bad.rar');bad.write_bytes(b'bad')
    with r.bind(),pytest.raises(ValueError,match='RAR'):
        with prepare(r,[bad],platform='Steam'):pytest.fail('must refuse')
    assert not list(r.path('cache/content_completion').glob('*'))

def test_failed_index_rolls_back_all_files(setup,monkeypatch):
    import frontend.registration as reg
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    paths=[package(r,'one.zip'),package(r,'two.zip','second.pack')]
    monkeypatch.setattr(reg,'rescan',lambda *args: (_ for _ in ()).throw(RuntimeError('index failure')))
    with pytest.raises(RuntimeError,match='index failure'):b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    assert not list(Path(result['managed_path']).glob('*.zip'))

def test_steam_multiple_packages_extract_without_flattening_and_reopen(setup):
    import sqlite3
    _,r,b,a=setup;result=register(b,a,platform='Steam',url='902799');args=context(b,result['internal_id'])
    paths=[package(r,'one.zip','root/data/unit.pack'),package(r,'two.zip','textures/extra.dds')]
    added=b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    root=Path(result['managed_path'])
    assert (root/'root/data/unit.pack').read_bytes()==b'actual payload'
    assert (root/'textures/extra.dds').is_file() and not list(root.glob('*.zip'))
    assert added['content_status']=='healthy'
    with sqlite3.connect(r.db.db_path) as reopened:
        row=reopened.execute('SELECT internal_id,content_status,deploy_status FROM mods WHERE mod_id=?',(result['mod_pk'],)).fetchone()
        assert row==(result['internal_id'],'healthy','not_deployed')

def test_bomb_limit_and_symlink_are_refused(setup,monkeypatch):
    import services.content_completion as completion
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    source=package(r,'oversize.zip',data=b'123456')
    monkeypatch.setattr(completion,'MAX_FILE_BYTES',5)
    with pytest.raises(ValueError,match='限额'):b.execute_command('content-add',{**args,'source':str(source)})
    link=r.path('incoming/link.zip')
    with zipfile.ZipFile(link,'w') as archive:
        item=zipfile.ZipInfo('link.pack');item.create_system=3;item.external_attr=0o120777<<16
        archive.writestr(item,'outside')
    with pytest.raises(ValueError,match='链接'):b.execute_command('content-add',{**args,'source':str(link)})

def _still_missing(r,result):
    assert not list(Path(result['managed_path']).glob('*.zip'))
    assert not list(Path(result['managed_path']).rglob('*.pak'))
    status=r.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(result['mod_pk'],)).fetchone()[0]
    assert status=='content_missing' and not r.db.get_mod_files(result['mod_pk']).files

def test_nexus_four_archives_keep_shared_member_names(setup):
    import sqlite3
    from core.mod_platform import FILE_ROLE_NEXUS_MAIN,FILE_ROLE_NEXUS_OPTIONAL,ModFilesBundle
    from services.mod_files import ModFileManager
    _,r,b,a=setup;result=register(b,a);pk=result['mod_pk'];args=context(b,result['internal_id'])
    paths=[package(r,f'part-{i}.zip','DroneScanEffectExtend_P.pak',f'payload-{i}'.encode()) for i in range(4)]
    added=b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    root=Path(result['managed_path'])
    assert added['files']==4 and added['content_status']=='healthy'
    stored=[(root/path.name).read_bytes() for path in paths]
    assert stored==[path.read_bytes() for path in paths] and len(set(stored))==4
    for path in paths:
        with zipfile.ZipFile(root/path.name) as archive:assert archive.namelist()==['DroneScanEffectExtend_P.pak']
    manager=ModFileManager(r.db)
    files=sorted(manager.get_files(pk),key=lambda item:item.name)
    assert [item.name for item in files]==[path.name for path in paths]
    manager.set_nexus_file_category(pk,files[0].id,'Main')
    manager.set_nexus_file_category(pk,files[1].id,'Main')
    manager.set_nexus_file_category(pk,files[2].id,'Optional')
    manager.set_nexus_file_category(pk,files[3].id,'Optional')
    manager.set_file_selection(pk,files[1].id,False)
    manager.set_file_selection(pk,files[2].id,True)
    with sqlite3.connect(r.db.db_path) as reopened:
        raw=reopened.execute('SELECT mod_files FROM mods WHERE mod_id=?',(pk,)).fetchone()[0]
    again=sorted(ModFilesBundle.from_json(raw).files,key=lambda item:item.name)
    assert [(item.file_role,item.selected_for_deploy,item.metadata.get('category')) for item in again]==[
        (FILE_ROLE_NEXUS_MAIN,True,'Main'),(FILE_ROLE_NEXUS_MAIN,False,'Main'),
        (FILE_ROLE_NEXUS_OPTIONAL,True,'Optional'),(FILE_ROLE_NEXUS_OPTIONAL,False,'Optional')]
    assert [item.path for item in again]==[path.name for path in paths]

def test_nexus_case_and_nested_member_names_stay_separate_archives(setup):
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    paths=[package(r,'one.zip'),package(r,'two.zip','DATA/UNIT.pack',b'second'),package(r,'three.zip','data/unit.pack/child',b'third')]
    added=b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    root=Path(result['managed_path'])
    assert added['files']==3 and added['content_status']=='healthy'
    assert [(root/path.name).read_bytes() for path in paths]==[path.read_bytes() for path in paths]

def test_duplicate_archive_name_and_member_do_not_publish(setup):
    _,r,b,a=setup;result=register(b,a);args=context(b,result['internal_id'])
    left=r.path('incoming/left');right=r.path('incoming/right');left.mkdir();right.mkdir()
    one=left/'same.zip';two=right/'same.zip'
    for path,data in ((one,b'left'),(two,b'right')):
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('DroneScanEffectExtend_P.pak',data)
    with pytest.raises(ValueError,match='来源包文件名重名'):b.execute_command('content-add',{**args,'archive_paths':[str(one),str(two)]})
    _still_missing(r,result)
    dup=r.path('incoming/dup.zip')
    with zipfile.ZipFile(dup,'w') as archive:
        archive.writestr('DroneScanEffectExtend_P.pak',b'a');archive.writestr('DroneScanEffectExtend_P.pak',b'b')
    with pytest.raises(ValueError,match='归档路径重名'):b.execute_command('content-add',{**args,'source':str(dup)})
    _still_missing(r,result)

def test_steam_still_refuses_merged_member_collisions(setup):
    _,r,b,a=setup;result=register(b,a,platform='Steam',url='902801');args=context(b,result['internal_id'])
    same=[package(r,'one.zip','DroneScanEffectExtend_P.pak',b'one'),package(r,'two.zip','dronescaneffectextend_p.pak',b'two')]
    with pytest.raises(ValueError,match='多包路径重名'):b.execute_command('content-add',{**args,'archive_paths':list(map(str,same))})
    _still_missing(r,result)
    nested=[package(r,'file.zip','data/unit.pack',b'file'),package(r,'dir.zip','data/unit.pack/child',b'dir')]
    with pytest.raises(ValueError,match='多包文件与目录冲突'):b.execute_command('content-add',{**args,'archive_paths':list(map(str,nested))})
    _still_missing(r,result)
