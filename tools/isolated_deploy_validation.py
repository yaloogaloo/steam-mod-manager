"""Real, unpatched deployment fixture and persisted file-state evidence."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def initialize(root):
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources, checked_sandbox_root
    from services.identity_service import create_mod_identity, identity_create_scope
    root = checked_sandbox_root(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError('必须使用新的空隔离目录')
    (root / 'data').mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(root / 'data/mod_manager.db')
    resources = BusinessResources(root, db)
    capability_bytes = (Path(__file__).resolve().parents[1] / 'config/game_capabilities.json').read_bytes()
    with resources.bind():
        resources.path('config').mkdir(parents=True)
        resources.path('config/game_capabilities.json').write_bytes(capability_bytes)
        source = root / 'mod/Game/RoundTripMod'
        source.mkdir(parents=True)
        (source / 'payload.txt').write_text('MOD-v1', encoding='utf8')
        game = root / 'games/TestGame'
        target = game / 'mods/RoundTripMod/payload.txt'
        target.parent.mkdir(parents=True)
        target.write_text('ORIGINAL', encoding='utf8')
        (target.parent / 'untouched.txt').write_text('KEEP', encoding='utf8')
        db.update_game_deploy_config(42, name='隔离部署验收游戏', install_path=str(game),
                                    mod_path=str(game / 'mods'), deploy_type='folder_copy')
        db.update_game_deploy_config(43, name='隔离游戏切换测试', install_path=str(root / 'games/OtherGame'),
                                    mod_path=str(root / 'games/OtherGame/mods'), deploy_type='folder_copy')
        with identity_create_scope():
            identity = create_mod_identity(db, platform='Steam', external_id='902100',
                workshop_id='902100', title='RoundTripMod', app_id=42,
                game_name='Game', operation='import')
        info = source / '.info'
        info.mkdir()
        (info / 'metadata.json').write_text(json.dumps({
            'internal_id': identity.internal_id, 'workspace_id': identity.workspace_id,
            'external_id': '902100', 'published_file_id': '902100',
            'title': 'RoundTripMod', 'app_id': 42, 'platform': 'Steam', 'game_name': 'Game',
            'description': '隔离运行：真实部署、重新部署与原文件恢复验收。'}, ensure_ascii=False), encoding='utf8')
        db.update_mod_identity_fields(identity.mod_id, last_known_path=str(source),
                                     folder_present=True, game_name='Game', title='RoundTripMod')
        # Empty directories establish the complete resource namespace even when
        # folder_copy does not need CAS or a game-specific order projection.
        for name in ('data/asset_store', 'data/mod_backup', 'data/deploy_backup', 'config/load_order', 'cache', 'logs'):
            resources.path(name).mkdir(parents=True, exist_ok=True)
        (root / '.smm-deploy-sandbox').write_text(identity.internal_id, encoding='utf8')
    return resources, source, target, identity


def snapshot(resources, identity):
    files = {}
    for path in resources.root.rglob('*'):
        if path.is_file():
            files[path.relative_to(resources.root).as_posix()] = {
                'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'deploy_status': resources.db.get_mod_deploy_info(identity.mod_id).deploy_status,
            'files': files}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    from services.mutation_context import activate_test_context, reset_mutation_context
    from services.deploy import ModDeployer
    from services.deploy_rules.manifest import load_manifest
    from services.business_resources import checked_sandbox_root
    root = checked_sandbox_root(args.root)
    token = activate_test_context([root])
    resources = None
    try:
        resources, source, target, identity = initialize(args.root)
        deployer = ModDeployer(library_root=resources.path('mod'), db=resources.db, resources=resources)
        stages = {'before': snapshot(resources, identity)}
        for action in ('deploy', 'redeploy', 'undeploy'):
            if action == 'redeploy':
                (source / 'payload.txt').write_text('MOD-v2', encoding='utf8')
            result = getattr(deployer, action + '_mod')(identity.internal_id)
            assert result['success'], result
            expected = {'deploy': 'MOD-v1', 'redeploy': 'MOD-v2', 'undeploy': 'ORIGINAL'}[action]
            assert target.read_text('utf8') == expected
            stages[action] = {'result': result, **snapshot(resources, identity)}
        assert (target.parent / 'untouched.txt').read_text('utf8') == 'KEEP'
        assert (source / 'payload.txt').read_text('utf8') == 'MOD-v2'
        assert load_manifest(source) is None
        assert stages['undeploy']['deploy_status'] == 'not_deployed'
        report = {'root': str(resources.root), 'internal_id': identity.internal_id,
                  'mod_pk': str(identity.mod_id), 'workspace_id': identity.workspace_id,
                  'strategy': 'folder_copy', 'stages': stages,
                  'cas_used_by_strategy': False, 'order_backend_for_game': None}
        (resources.root / 'roundtrip.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf8')
        print(json.dumps({'success': True, 'root': str(resources.root)}, ensure_ascii=False))
    finally:
        if resources is not None:
            resources.db.close()
        reset_mutation_context(token)


if __name__ == '__main__':
    main()
