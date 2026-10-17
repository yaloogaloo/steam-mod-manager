"""Real WH3 native Workshop activation and canonical projection contracts."""
import json
import pytest
from tools.wh3_deploy_validation import initialize, verify_state, APP_ID
from services.deploy import ModDeployer
from tests.test_isolated_deploy import environment


@pytest.fixture
def wh3(tmp_path):
    resources, source, target, identity = initialize(tmp_path / 'wh3')
    yield resources, source, target, identity
    resources.db.close()


def test_native_workshop_real_roundtrip(wh3):
    resources, source, target, identity = wh3
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    verify_state(*wh3, False)
    for action in ('deploy', 'redeploy', 'undeploy'):
        if action == 'redeploy':
            # Redeploy must actually rebuild projection, not report cached state.
            (resources.root/'games/Warhammer3/used_mods.txt').write_text('STALE', encoding='utf8')
        result = getattr(deployer, action+'_mod')(identity.internal_id)
        assert result['success'], result
        verify_state(*wh3, action != 'undeploy')


def test_projection_failure_is_not_success(wh3):
    resources, source, target, identity = wh3
    projection = resources.root/'games/Warhammer3/used_mods.txt'
    projection.unlink()
    projection.mkdir()  # Real filesystem error, not a mocked Worker.
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    result = deployer.deploy_mod(identity.internal_id)
    assert not result['success']
    assert '投影' in result['error']
    # Canonical deployed membership remains authoritative despite failed projection.
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'deployed'
    assert target.read_bytes() == b'WORKSHOP-ORIGINAL'
    projection.rmdir()
    assert deployer.redeploy_mod(identity.internal_id)['success']
    verify_state(*wh3, True)
    assert deployer.undeploy_mod(identity.internal_id)['success']
    verify_state(*wh3, False)


def test_native_missing_and_unapproved_strategy_refused(wh3):
    resources, source, target, identity = wh3
    target.rename(target.with_suffix('.held'))
    with pytest.raises(PermissionError, match='原生 Workshop'):
        resources.validate_deploy(identity.internal_id)
    resources.db.update_game_deploy_config(APP_ID, name='Total War: WARHAMMER III',
        workshop_path=str(resources.root/'games/steamapps/workshop/content/1142710'))
    # App routing must still force WH3, regardless of the saved type label.
    target.with_suffix('.held').rename(target)
    with resources.bind():
        resources.validate_deploy(identity.internal_id)
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    assert deployer.deploy_mod(identity.internal_id)['success']
    target.rename(target.with_suffix('.held'))
    # Missing external projection is not permission to hide canonical membership;
    # withdrawing a deployed member still uses the original non-copy algorithm.
    assert deployer.undeploy_mod(identity.internal_id)['success']
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'not_deployed'
    target.with_suffix('.held').rename(target)
    verify_state(*wh3, False)


def test_unapproved_strategy_ui_and_backend_refuse(environment):
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_bridge import LibraryBridge
    from PySide6.QtWidgets import QApplication
    resources, source, target, identity = environment
    resources.db.update_game_deploy_config(42, deploy_type='custom_path')
    app = QApplication.instance() or QApplication([])
    bridge = LibraryBridge(LibraryAdapter(resources.db, writable=True), resources=resources)
    library = bridge.execute('library', {'game':42})
    asset = library['assets'][0]
    args = dict(asset, game='42', snapshot=library['snapshot'])
    detail = bridge.execute('detail', args)
    assert not detail['actions']['primary']['enabled']
    assert '尚未完成隔离验收' in detail['actions']['primary']['reason']
    messages = []
    bridge.response.connect(lambda raw: messages.append(json.loads(raw)))
    bridge.command('unapproved', 'deploy', json.dumps(args))
    for _ in range(100):
        app.processEvents()
        if messages and bridge.shutdown():
            break
        from PySide6.QtTest import QTest
        import time
        time.sleep(.01)
        QTest.qWait(10)
    assert messages and 'error' in messages[0]
    assert target.read_text() == 'ORIGINAL'
    assert bridge.shutdown()


def test_legacy_copy_manifest_is_not_admitted_as_native(wh3):
    from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, save_manifest
    resources, source, target, identity = wh3
    original = resources.root/'games/Warhammer3/data/original.pack'
    with resources.bind():
        save_manifest(source, DeployManifest(mod_id=str(identity.mod_id), deploy_time='',
            deploy_type='warhammer3_pack', internal_id=identity.internal_id,
            files=[ManifestFileEntry(source=str(source/'library-only.pack'), target=str(original))]))
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    with pytest.raises(PermissionError, match='历史复制 manifest'):
        deployer.deploy_mod(identity.internal_id)
    assert original.read_bytes() == b'GAME-ORIGINAL'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'not_deployed'


def test_order_backend_filters_members_and_rewrites_projection(wh3):
    from services.order_backend import get_order_backend
    resources, source, target, identity = wh3
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    assert deployer.deploy_mod(identity.internal_id)['success']
    backend = get_order_backend(APP_ID)
    with resources.bind():
        assert backend.persist_order([identity.internal_id], db=resources.db,
            library_root=resources.path('mod')) == [identity.internal_id]
        backend.sync_projection(db=resources.db, library_root=resources.path('mod'))
    verify_state(*wh3, True)
    assert json.loads((resources.root/'config/load_order/wh3.json').read_text())['order'] == [identity.internal_id]
    assert deployer.undeploy_mod(identity.internal_id)['success']
    with resources.bind():
        assert backend.persist_order([identity.internal_id], db=resources.db,
            library_root=resources.path('mod')) == []
    verify_state(*wh3, False)


def test_two_mod_order_and_unresolved_projection_keep_membership(wh3):
    from services.order_backend import get_order_backend
    from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
    resources, source, target, identity = wh3
    db = resources.db
    with resources.bind():
        second = create_steam_test_mod(db, external_id='902101', title='Second',
            app_id=APP_ID, game_name='Warhammer3')
        other_source = resources.path('mod/Warhammer3/Second')
        other_source.mkdir()
        (other_source/'second-library.pack').write_bytes(b'LIBRARY-B')
        prove_managed_folder(db, other_source, handle=second.mod_id, title='Second',
            app_id=APP_ID, game_name='Warhammer3')
        other = target.parent.parent / str(second.workspace_id) / 'second.pack'
        other.parent.mkdir()
        other.write_bytes(b'WORKSHOP-B')
    deployer = ModDeployer(library_root=resources.path('mod'), db=db, resources=resources)
    backend = get_order_backend(APP_ID)
    assert deployer.deploy_mod(identity.internal_id)['success']
    with resources.bind():
        assert backend.persist_order([second.internal_id, identity.internal_id], db=db,
            library_root=resources.path('mod')) == [identity.internal_id]
    assert deployer.deploy_mod(second.internal_id)['success']
    with resources.bind():
        expected = [second.internal_id, identity.internal_id]
        assert backend.persist_order(expected, db=db, library_root=resources.path('mod')) == expected
        backend.sync_projection(db=db, library_root=resources.path('mod'))
        text = (resources.root/'games/Warhammer3/used_mods.txt').read_text('utf8')
        assert text.index('mod "second.pack";') < text.index('mod "native.pack";')
        target.rename(target.with_suffix('.held'))
        backend.sync_projection(db=db, library_root=resources.path('mod'))
        assert backend.get_current_order(db=db, library_root=resources.path('mod')) == expected
        assert set(backend.get_sortable_mods(db=db, library_root=resources.path('mod'))) == set(expected)
        target.with_suffix('.held').rename(target)
    assert deployer.undeploy_mod(second.internal_id)['success']
    assert deployer.undeploy_mod(identity.internal_id)['success']
    verify_state(*wh3, False)
