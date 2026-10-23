import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import pytest
from tests.test_web_registration import setup, register, context
from tests.test_archive_admission import archive_fixture, rar_store


@pytest.mark.parametrize('platform',['Steam','Nexus'])
def test_mixed_zip_rar_7z_completes_one_entity_and_persists(setup,platform):
    _,r,b,a=setup
    result=register(b,a,platform=platform,url='902788' if platform=='Steam' else 'https://www.nexusmods.com/totalwarwarhammer3/mods/91078')
    args=context(b,result['internal_id']);root=r.path('incoming')
    paths=[archive_fixture(root,'zip','zip/data.pack'),rar_store(root/'part.rar','rar/data.pack'),archive_fixture(root,'7z','seven/data.pack')]
    before=b.execute('library',{'game':1142710})['assets']
    assert next(x for x in before if x['internal_id']==result['internal_id'])['content_status']=='content_missing'
    args=context(b,result['internal_id'])
    added=b.execute_command('content-add',{**args,'archive_paths':list(map(str,paths))})
    assert added['content_status']=='healthy' and added['files']==3
    managed=Path(result['managed_path'])
    if platform=='Steam':assert all((managed/name).is_file() for name in ['zip/data.pack','rar/data.pack','seven/data.pack'])
    else:
        assert all((managed/p.name).read_bytes()==p.read_bytes() for p in paths)
        assert {f.filename for f in r.db.get_mod_files(result['mod_pk']).files}=={p.name for p in paths}
    with sqlite3.connect(r.db.db_path) as reopened:
        assert reopened.execute('SELECT content_status,deploy_status FROM mods WHERE mod_id=?',(result['mod_pk'],)).fetchone()==('healthy','not_deployed')
    after=b.execute('library',{'game':1142710})['assets']
    assert len(before)==len(after) and next(x for x in after if x['internal_id']==result['internal_id'])['content_status']=='healthy'


@pytest.mark.parametrize('kind',['rar','7z','tar.gz','tar.zst','renamed-rar'])
@pytest.mark.parametrize('platform',['Steam','Nexus'])
def test_registration_worker_uses_same_real_archive_admission(setup,kind,platform):
    app,r,b,a=setup;source=archive_fixture(r.path('incoming'),'rar' if kind=='renamed-rar' else kind)
    if kind=='renamed-rar':source=source.rename(source.with_name('wrong-extension.data'))
    args={**a,'kind':'local','platform':platform,'format':'zip','source':str(source),'archive_paths':[str(source)],
          'title':'Real archive registration','workshop_id':'902789','url':'' if platform=='Steam' else 'https://www.nexusmods.com/totalwarwarhammer3/mods/91079'}
    preview=b.execute('registration-preview',args)
    messages=[];b.response.connect(lambda raw:messages.append(json.loads(raw)))
    b.command('real-archive','import-zip',json.dumps({**args,'registration_draft':preview['draft'],'confirm_nexus_identity':True}))
    deadline=time.monotonic()+30
    while not any(m['request']=='real-archive' for m in messages):
        app.processEvents();time.sleep(.01);assert time.monotonic()<deadline
    message=next(m for m in messages if m['request']=='real-archive')
    assert 'error' not in message,message
    result=message['result'];assert result['state']=='success',result
    row=r.db.get_mod_backup_row(result['mod_pk']);assert row['content_status']=='healthy'
    managed=Path(result['managed_path'])
    if platform=='Steam':assert any(p.read_bytes()==b'actual payload' for p in managed.rglob('unit.pack'))
    else:
        entries=r.db.get_mod_files(result['mod_pk']).files
        assert len(entries)==1 and (managed/entries[0].path).read_bytes()==source.read_bytes()
        assert entries[0].metadata['archive_name']==source.name


def test_resource_directory_memory_cancellation_fallback_and_new_process(setup):
    from PySide6.QtCore import QSettings,QStandardPaths
    from services.importers.import_settings import (remember_resource_directory,resource_picker_directory,
        resource_picker_settings,get_last_import_directory,SETTING_LAST_IMPORT_DIRECTORY)
    _,r,b,a=setup
    folder=r.path('incoming/resources/UI');folder.mkdir(parents=True);file=folder/'source.mhtml';file.write_text('input')
    remember_resource_directory(r,folder);assert Path(resource_picker_directory(r))==folder
    remember_resource_directory(r,file);remember_resource_directory(r,'')
    assert Path(resource_picker_directory(r))==folder
    store=resource_picker_settings(r)
    script="from PySide6.QtCore import QSettings;from services.importers.import_settings import get_last_import_directory;import sys;print(get_last_import_directory(settings=QSettings(sys.argv[1],QSettings.IniFormat)))"
    restarted=subprocess.check_output([sys.executable,'-B','-c',script,store.fileName()],text=True).strip()
    assert Path(restarted)==folder
    store.setValue(SETTING_LAST_IMPORT_DIRECTORY,str(folder/'deleted'));store.sync()
    fallback=Path(get_last_import_directory(settings=store))
    assert fallback.is_dir() and not fallback.is_relative_to(Path(__file__).resolve().parents[1]/'_tmp')


def test_decoder_does_not_admit_arbitrary_external_execution(setup):
    _,r,b,a=setup
    with pytest.raises(PermissionError):
        with r.bind():subprocess.run([sys.executable,'-c','print(123)'],check=True)
    from services.business_resources import archive_decoder
    from services.importers.archive import find_7z_executable
    with pytest.raises(PermissionError,match='受控'):
        with archive_decoder([find_7z_executable(),'x','-oC:/','--',str(r.path('incoming/x.zip'))],r.path('incoming/x.zip')):pass
