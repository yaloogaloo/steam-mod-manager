"""Actual deploy/backup/manifest roundtrip under injected business resources."""
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from services.business_resources import BusinessResources
from services.deploy import ModDeployer
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


def seed(root, db):
    library = root / 'mod'
    source = library / 'Game' / 'RoundTripMod'
    source.mkdir(parents=True)
    (source / 'payload.txt').write_text('MOD-v1', encoding='utf8')
    game = root / 'games' / 'TestGame'
    mods = game / 'mods'
    prior = mods / 'RoundTripMod' / 'payload.txt'
    prior.parent.mkdir(parents=True)
    prior.write_text('ORIGINAL', encoding='utf8')
    (prior.parent / 'untouched.txt').write_text('KEEP', encoding='utf8')
    db.update_game_deploy_config(42, name='Isolation game', install_path=str(game),
                               mod_path=str(mods), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='902100', title='RoundTripMod',
                                  app_id=42, game_name='Game')
    prove_managed_folder(db, source, handle=created.mod_id, title='RoundTripMod',
                         app_id=42, game_name='Game')
    return source, prior, created


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / 'business'
    (root / 'data').mkdir(parents=True)
    db = DatabaseManager(root / 'data/mod_manager.db')
    (root / 'config').mkdir()
    (root / 'config/game_capabilities.json').write_bytes(
        (Path(__file__).resolve().parents[1] / 'config/game_capabilities.json').read_bytes())
    source, target, identity = seed(root, db)
    resources = BusinessResources(root, db)
    yield resources, source, target, identity
    db.close()


def test_roundtrip_real_backup_manifest_and_restore(environment):
    from services.deploy_rules.manifest import load_manifest
    resources, source, target, identity = environment
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    result = deployer.deploy_mod(identity.internal_id)
    assert result['success'], result
    assert target.read_text('utf8') == 'MOD-v1'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'deployed'
    manifest = load_manifest(source)
    assert manifest and manifest.files[0].backup
    assert (resources.path('data/deploy_backup') / identity.internal_id).is_dir()
    (source / 'payload.txt').write_text('MOD-v2', encoding='utf8')
    result = deployer.redeploy_mod(identity.internal_id)
    assert result['success'], result
    assert target.read_text('utf8') == 'MOD-v2'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'deployed'
    result = deployer.undeploy_mod(identity.internal_id)
    assert result['success'], result
    assert target.read_text('utf8') == 'ORIGINAL'
    assert (target.parent / 'untouched.txt').read_text('utf8') == 'KEEP'
    assert (source / 'payload.txt').read_text('utf8') == 'MOD-v2'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'not_deployed'
    assert load_manifest(source) is None


def test_unknown_write_rejected_even_if_helper_catches(environment, tmp_path):
    resources, *_ = environment
    outside = tmp_path / 'outside.txt'
    with pytest.raises(PermissionError):
        with resources.bind():
            try:
                outside.write_text('escape', encoding='utf8')
            except PermissionError:
                pass
    assert not outside.exists()


def test_config_escape_refused_without_filesystem_change(environment, tmp_path):
    resources, source, target, identity = environment
    resources.db.update_game_deploy_config(42, mod_path=str(tmp_path / 'outside'))
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    with pytest.raises(PermissionError):
        deployer.deploy_mod(identity.internal_id)
    assert target.read_text('utf8') == 'ORIGINAL'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status == 'not_deployed'


def test_cas_db_and_order_paths_use_same_context(environment):
    from core import paths
    from core.db_manager import get_db
    from services.asset_store import AssetStore
    from services.info_asset_runtime import _asset_store_dir
    resources, *_ = environment
    with resources.bind():
        assert get_db() is resources.db
        assert paths.database_path() == Path(resources.db.db_path)
        assert paths.default_mod_library() == resources.path('mod')
        assert paths.load_order_dir() == resources.path('config/load_order')
        assert _asset_store_dir() == resources.path('data/asset_store')
        stored = AssetStore().put_bytes(b'isolated-CAS-proof')
        assert stored.path.is_relative_to(resources.path('data/asset_store'))
        assert stored.path.read_bytes() == b'isolated-CAS-proof'


def test_real_missing_payload_failure_leaves_original_files(environment):
    resources, source, target, identity = environment
    (source / 'payload.txt').unlink()
    deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
    result = deployer.deploy_mod(identity.internal_id)
    assert not result['success'] and result['error']
    assert target.read_text('utf8') == 'ORIGINAL'
    assert (target.parent / 'untouched.txt').read_text('utf8') == 'KEEP'
    assert resources.db.get_mod_deploy_info(identity.mod_id).deploy_status != 'deployed'


def test_junction_and_relative_escape_rejected(environment, tmp_path):
    import os
    import subprocess
    resources, *_ = environment
    with pytest.raises(PermissionError):
        resources.validate('../outside')
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = resources.root / 'junction'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        resources.validate(link / 'escape.txt')
    assert not (outside / 'escape.txt').exists()
