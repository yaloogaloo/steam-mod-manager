"""Canonical startup routing and the release-critical size worker boundary."""
def test_main_readonly_routes_without_production_crash_log(monkeypatch):
    import main
    calls=[]
    monkeypatch.setattr(main,'_configure_logging',lambda **values:calls.append(values))
    monkeypatch.setattr(main,'launch_gui',lambda **values:calls.append(values) or 0)
    assert main.main(['--read-only'])==0
    assert calls==[{'verbose':False,'crash_hooks':False},
                   {'next_frontend':True,'read_only':True,'validation_root':None}]


def test_production_write_gate_stays_closed():
    import pytest
    from frontend.admission import require_production_admission
    with pytest.raises(RuntimeError):require_production_admission(read_only=False)


def test_size_observation_stays_in_owned_database(tmp_path,monkeypatch):
    import threading
    from tools.isolated_deploy_validation import initialize
    from services import size_observation as size
    r,source,target,identity=initialize(tmp_path/'candidate')
    errors=[];values=[]
    monkeypatch.setattr(size,'_ensure_workers',lambda:(_ for _ in ()).throw(AssertionError('global worker queue used')))
    def observe():
        try:
            with r.bind():values.append(size.enqueue_mod_size(identity.mod_id,source,force=True))
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=observe);thread.start();thread.join(10)
    assert not thread.is_alive() and not errors and values==[True],errors
    row=r.db.get_mod_size_observation(identity.mod_id)
    assert row['local_size_status']=='ok' and row['local_size_bytes']>0
    r.db.close()
