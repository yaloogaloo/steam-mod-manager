import pytest
from pathlib import Path
from PySide6.QtWidgets import QApplication
from tools.organization_validation import initialize
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from services import collection


def test_original_removal_service_relations_rollback_and_file_preservation(tmp_path):
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'removal');b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    db=r.db
    def args(iid):
        s=b.execute('library',{'game':1142710});a=next(a for a in s['assets'] if a['internal_id']==iid)
        return {'game':'1142710','snapshot':s['snapshot'],'token':a['token'],'internal_id':iid}
    try:
        a=args(ids[0].internal_id);preview=b.execute('deregister-preview',a)
        with pytest.raises(PermissionError):b.execute_command('mod-deregister',{**a,'version':preview['version'],'confirm':True,'keep_files':True})
        with r.bind():
            c=collection.create_collection(1142710,'Keep collection',db=db)
            collection.add_mod_to_collection(c.collection_id,ids[1].internal_id,db=db)
        a=args(ids[1].internal_id);p=b.execute('deregister-preview',a)
        from services.asset_store import AssetStore
        with r.bind():
            store=AssetStore(r.path('data/asset_store'));shared=store.put_bytes(b'Shared CAS must survive deregistration')
        shared_path=store.object_path(shared.sha256);shared_bytes=shared_path.read_bytes()
        files={str(f):f.read_bytes() for f in r.path('mod').rglob('*') if f.is_file()}
        db._conn.execute("CREATE TEMP TRIGGER refuse_removal BEFORE DELETE ON mods BEGIN SELECT RAISE(ABORT,'controlled deletion failure'); END")
        with pytest.raises(Exception):b.execute_command('mod-deregister',{**a,'version':p['version'],'confirm':True,'keep_files':True})
        assert db.get_category_tags(ids[1].mod_id)==['Graphics']
        assert db._conn.execute('SELECT COUNT(*) FROM collection_mods WHERE mod_id=?',(ids[1].mod_id,)).fetchone()[0]==1
        db._conn.execute('DROP TRIGGER refuse_removal')
        result=b.execute_command('mod-deregister',{**a,'version':p['version'],'confirm':True,'keep_files':True})
        assert result['success'] and db.find_mod_by_internal_id(ids[1].internal_id) is None
        assert db._conn.execute('SELECT COUNT(*) FROM collection_mods WHERE mod_id=?',(ids[1].mod_id,)).fetchone()[0]==0
        assert not db._conn.execute('PRAGMA foreign_key_check').fetchall()
        assert files=={str(f):f.read_bytes() for f in r.path('mod').rglob('*') if f.is_file()}
        assert shared_path.read_bytes()==shared_bytes
        with pytest.raises(Exception):b.execute_command('mod-deregister',{**a,'version':p['version'],'confirm':True,'keep_files':True})
    finally:b.shutdown();db.close()


def test_offline_command_materializes_then_opens_on_gui_thread(tmp_path,monkeypatch):
    import json,time
    from PySide6.QtCore import QThread
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtTest import QTest
    from services.info_asset_runtime import finalize_live_offline_to_cas
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'open');b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    responses=[];opened=[]
    b.response.connect(lambda raw:responses.append(json.loads(raw)))
    monkeypatch.setattr(QDesktopServices,'openUrl',lambda url:opened.append((url.toLocalFile(),QThread.currentThread()==app.thread())) or True)
    try:
        pk=ids[1].mod_id;folder=Path(r.db.get_mod_backup_row(pk)['last_known_path'])
        with r.bind():
            assets=folder/'.info/assets';assets.mkdir()
            (assets/'style.css').write_text('body{color:red}')
            (folder/'.info/index.html').write_text('<html><head><link rel="stylesheet" href="assets/style.css"></head><body>Read me</body></html>')
            assert finalize_live_offline_to_cas(folder).ok
        snap=b.execute('library',{'game':1142710});a=next(a for a in snap['assets'] if a['internal_id']==ids[1].internal_id)
        args={'game':'1142710','snapshot':snap['snapshot'],'token':a['token'],'internal_id':a['internal_id']}
        b.command('open-page','open-offline',json.dumps(args))
        until=time.monotonic()+8
        while not responses and time.monotonic()<until:QTest.qWait(20);time.sleep(.01)
        assert responses,(b.pool.activeThreadCount(),b._busy)
        assert 'error' not in responses[-1],responses
        assert len(opened)==1 and opened[0][1]
        assert 'cache' in opened[0][0] and 'offline_view' in opened[0][0]
    finally:b.shutdown();r.db.close()


@pytest.mark.parametrize('status',['deployed','failed',''])
def test_deregistration_unknown_deployed_and_readonly_refused(tmp_path,status):
    from core.db_manager import DatabaseManager
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'refused');b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    ro=None;readonly=None
    try:
        r.db._conn.execute('UPDATE mods SET deploy_status=? WHERE mod_id=?',(status,ids[1].mod_id));r.db._conn.commit()
        snap=b.execute('library',{'game':1142710});a=next(a for a in snap['assets'] if a['internal_id']==ids[1].internal_id)
        args={'game':'1142710','snapshot':snap['snapshot'],'token':a['token'],'internal_id':a['internal_id']}
        p=b.execute('deregister-preview',args)
        assert not p['allowed']
        with pytest.raises(PermissionError):b.execute_command('mod-deregister',{**args,'version':p['version'],'confirm':True,'keep_files':True})
        ro=DatabaseManager.open_existing(r.db.db_path,read_only=True);readonly=LibraryBridge(LibraryAdapter(ro))
        with pytest.raises(PermissionError):readonly.execute_command('mod-deregister',args)
        assert r.db.find_mod_by_internal_id(ids[1].internal_id) is not None
    finally:
        if readonly:readonly.shutdown()
        if ro:ro.close()
        b.shutdown();r.db.close()


def test_cleanup_archive_real_files_and_database_failure(tmp_path):
    from services.registration_removal import ModRegistrationRemovalService
    from services.registration_archive_cleanup import exclusive_archive
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'cleanup')
    db=r.db;service=ModRegistrationRemovalService(db=db);pk=ids[1].mod_id
    try:
        with r.bind():
            folder,files=exclusive_archive(db,r,pk)
            original={str(p.relative_to(folder)):p.read_bytes() for p in files}
            db._conn.execute("CREATE TEMP TRIGGER cleanup_failure BEFORE DELETE ON mods BEGIN SELECT RAISE(ABORT,'DB failure'); END")
            with pytest.raises(Exception):service.remove_mod_registration(pk,cleanup_resources=r)
            assert db.get_mod_backup_row(pk) and folder.is_dir()
            assert original=={str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}
            db._conn.execute('DROP TRIGGER cleanup_failure')
            other=Path(db.get_mod_backup_row(ids[2].mod_id)['last_known_path'])
            before={str(p):p.read_bytes() for p in other.rglob('*') if p.is_file()}
            result=service.remove_mod_registration(pk,cleanup_resources=r)
            assert result.success and result.filesystem_touched
            assert result.details['removed_files'] and not folder.exists()
            assert db.get_mod_backup_row(pk) is None
            assert before=={str(p):p.read_bytes() for p in other.rglob('*') if p.is_file()}
            assert not db._conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:db.close()


def test_cleanup_shared_path_and_pending_recovery(tmp_path,monkeypatch):
    from services.registration_removal import ModRegistrationRemovalService
    from services.registration_archive_cleanup import exclusive_archive,recover_pending
    import services.registration_archive_cleanup as cleanup
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'pending');db=r.db;service=ModRegistrationRemovalService(db=db);pk=ids[1].mod_id
    try:
        with r.bind():
            folder,files=exclusive_archive(db,r,pk)
            db._conn.execute('UPDATE mods SET cover_path=? WHERE mod_id=?',(str(files[0]),ids[2].mod_id));db._conn.commit()
            with pytest.raises(PermissionError,match='共享'):exclusive_archive(db,r,pk)
            db._conn.execute('UPDATE mods SET cover_path=? WHERE mod_id=?',('',ids[2].mod_id));db._conn.commit()
            original=cleanup.shutil.rmtree
            monkeypatch.setattr(cleanup.shutil,'rmtree',lambda *a,**kw:(_ for _ in ()).throw(OSError('occupied after commit')))
            with pytest.raises(RuntimeError,match='登记已注销'):service.remove_mod_registration(pk,cleanup_resources=r)
            assert db.get_mod_backup_row(pk) is None
            journals=list(r.path('mod/.smm-unregister').glob('*/operation.json'));assert len(journals)==1
            assert (journals[0].parent/'archive').exists()
            monkeypatch.setattr(cleanup.shutil,'rmtree',original)
            result=recover_pending(service,r,journals[0].parent.name)
            assert result['state']=='complete' and not (journals[0].parent/'archive').exists()
            assert recover_pending(service,r,journals[0].parent.name)['state']=='complete'
    finally:db.close()


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows file-sharing contract')
def test_cleanup_refuses_occupied_file_before_database_change(tmp_path):
    import ctypes
    from ctypes import wintypes
    from services.registration_removal import ModRegistrationRemovalService
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'locked');db=r.db;pk=ids[1].mod_id
    folder=Path(db.get_mod_backup_row(pk)['last_known_path'])
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=create(str(folder/'payload.txt'),0x80000000,0,None,3,0,None)
    assert handle!=ctypes.c_void_p(-1).value
    try:
        with r.bind():
            with pytest.raises(OSError,match='占用'):ModRegistrationRemovalService(db=db).remove_mod_registration(pk,cleanup_resources=r)
            assert folder.is_dir() and db.get_mod_backup_row(pk)
    finally:kernel.CloseHandle(handle);db.close()



def test_cleanup_rejects_deployed_and_archive_outside_root(tmp_path):
    from services.registration_archive_cleanup import exclusive_archive
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'boundary');db=r.db
    try:
        with r.bind():
            with pytest.raises(PermissionError,match='未部署'):exclusive_archive(db,r,ids[0].mod_id)
            pk=ids[1].mod_id;row=db.get_mod_backup_row(pk)
            inside=Path(row['last_known_path']);outside=r.path('incoming/foreign');outside.mkdir(parents=True)
            import shutil
            shutil.copytree(inside/'.info',outside/'.info')
            db._conn.execute('UPDATE mods SET last_known_path=? WHERE mod_id=?',(str(outside),pk));db._conn.commit()
            from services.managed_path_cache import invalidate_managed_path_cache
            invalidate_managed_path_cache(pk)
            with pytest.raises(PermissionError):exclusive_archive(db,r,pk)
            assert inside.is_dir() and outside.is_dir() and db.get_mod_backup_row(pk)
    finally:db.close()


def test_cleanup_keeps_real_shared_cas_objects(tmp_path):
    from services.registration_removal import ModRegistrationRemovalService
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas
    import hashlib
    app=QApplication.instance() or QApplication([])
    r,ids=initialize(tmp_path/'shared-cas');db=r.db
    try:
        with r.bind():
            for identity in (ids[1],ids[2]):
                folder=Path(db.get_mod_backup_row(identity.mod_id)['last_known_path'])
                assets=folder/'.info/assets';assets.mkdir()
                (assets/'style.css').write_bytes(b'body{color:#cde;background:#123}')
                (folder/'.info/index.html').write_text('<link rel="stylesheet" href="assets/style.css"><p>Shared snapshot</p>')
                assert finalize_live_offline_to_cas(folder).ok
            objects={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.path('data/asset_store').rglob('*') if p.is_file()}
            assert objects
            other=Path(db.get_mod_backup_row(ids[2].mod_id)['last_known_path'])/'.info/manifest.json'
            saved=other.read_bytes()
            assert ModRegistrationRemovalService(db=db).remove_mod_registration(ids[1].mod_id,cleanup_resources=r).success
            assert saved==other.read_bytes()
            assert objects=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.path('data/asset_store').rglob('*') if p.is_file()}
    finally:db.close()
