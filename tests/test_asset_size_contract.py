"""Stored payload bytes across sources, stale reads and content completion."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.test_web_registration import setup, register, context
from tests.test_content_completion_contract import package


@pytest.mark.parametrize('platform,url', [
    ('Steam','982001'), ('Nexus','982002'), ('mod.io','982003'),
    ('GitHub','https://github.com/owner/sized'), ('Other',''),
])
def test_completion_updates_payload_bytes_before_response(setup, platform, url):
    _,resources,bridge,args=setup
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(register,bridge,args,platform,url).result()
        pk=result['mod_pk'];command=context(bridge,result['internal_id'])
        root=Path(result['managed_path'])
        (root/'.info/irrelevant.bin').write_bytes(b'metadata'*1000)
        before=bridge.execute('detail',command)
        assert before['size']==0
        archive=package(resources,'size.zip',data=b'payload bytes'*123)
        expected=len(b'payload bytes'*123) if platform=='Steam' else archive.stat().st_size
        completed=pool.submit(bridge.execute_command,'content-add',{
            **command,'archive_paths':[str(archive)]}).result()
        assert completed['success']
        stored=resources.db.get_mod_size_observation(pk)
        assert stored['local_size_status']=='ok' and stored['local_size_bytes']==expected
        latest=context(bridge,result['internal_id'])
        changes=resources.db._conn.total_changes
        assert bridge.execute('detail',latest)['size']==expected
        assert resources.db._conn.total_changes==changes
        # Historical SQL observation is never used as a detail-byte fallback.
        resources.db.update_mod_size_observation(pk,status='ok',size_bytes=636,
            observed_at=stored['local_size_observed_at'],root_mtime=stored['local_size_root_mtime'])
        changes=resources.db._conn.total_changes
        assert bridge.execute('detail',latest)['size']==expected
        assert resources.db._conn.total_changes==changes


def test_nested_change_bypasses_root_mtime_cache(tmp_path):
    from services.dir_size import directory_size
    root=tmp_path/'root';(root/'data').mkdir(parents=True)
    file=root/'data/content.bin';file.write_bytes(b'x'*10)
    assert directory_size(root)==10
    stamp=root.stat().st_mtime
    file.write_bytes(b'x'*100)
    assert root.stat().st_mtime==stamp
    assert directory_size(root,use_cache=False)==100


def test_failed_completion_restores_size_observation(setup, monkeypatch):
    import frontend.registration as registration
    _,resources,bridge,args=setup
    original=registration.rescan
    def fail_after_observation(*args):
        original(*args)
        raise RuntimeError('cancel after size observation')
    monkeypatch.setattr(registration,'rescan',fail_after_observation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(register,bridge,args).result()
        command=context(bridge,result['internal_id'])
        archive=package(resources,'rolled-back.zip')
        with pytest.raises(RuntimeError,match='cancel after size'):
            pool.submit(bridge.execute_command,'content-add',{
                **command,'source':str(archive)}).result()
        assert not (Path(result['managed_path'])/archive.name).exists()
        row=resources.db.get_mod_size_observation(result['mod_pk'])
        assert row['local_size_status']=='ok' and row['local_size_bytes']==0
