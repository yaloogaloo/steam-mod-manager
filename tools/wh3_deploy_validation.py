"""Native Workshop WH3 fixture and real file/projection evidence."""
import hashlib
import json
from pathlib import Path

APP_ID = 1142710


def initialize(root):
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources, checked_sandbox_root
    from services.identity_service import create_mod_identity, identity_create_scope
    root = checked_sandbox_root(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError('必须使用新的空隔离目录')
    (root / 'data').mkdir(parents=True)
    db = DatabaseManager(root / 'data/mod_manager.db')
    resources = BusinessResources(root, db)
    caps = (Path(__file__).resolve().parents[1] / 'config/game_capabilities.json').read_bytes()
    with resources.bind():
        resources.path('config').mkdir()
        resources.path('config/game_capabilities.json').write_bytes(caps)
        game = root / 'games/Warhammer3'
        data = game / 'data'
        data.mkdir(parents=True)
        (data / 'original.pack').write_bytes(b'GAME-ORIGINAL')
        (game / 'used_mods.txt').write_text('', encoding='utf8')
        workshop = root / 'games/steamapps/workshop/content/1142710'
        db.update_game_deploy_config(APP_ID, name='Total War: WARHAMMER III',
            install_path=str(game), mod_path=str(data), workshop_path=str(workshop),
            deploy_type='warhammer3_pack')
        with identity_create_scope():
            identity = create_mod_identity(db, platform='Steam', external_id='902100',
                workshop_id='902100', title='WH3 Native Workshop', app_id=APP_ID,
                game_name='Warhammer3', operation='import')
        source = root / 'mod/Warhammer3/NativeWorkshop'
        info = source / '.info'
        info.mkdir(parents=True)
        # Different library filename proves projection never falls back here.
        (source / 'library-only.pack').write_bytes(b'LIBRARY-PRESERVE')
        (info / 'metadata.json').write_text(json.dumps({
            'internal_id': identity.internal_id, 'workspace_id': identity.workspace_id,
            'external_id':'902100', 'published_file_id':'902100', 'platform':'Steam',
            'title':'WH3 Native Workshop', 'app_id':APP_ID, 'game_name':'Warhammer3',
            'description':'真实 Workshop Content 路径及 used_mods.txt 投影验收。',
            'source_url':'https://steamcommunity.com/sharedfiles/filedetails/?id=902100',
        }, ensure_ascii=False), encoding='utf8')
        db.update_mod_identity_fields(identity.mod_id, last_known_path=str(source),
            folder_present=True, title='WH3 Native Workshop', game_name='Warhammer3')
        target = workshop / str(identity.workspace_id) / 'native.pack'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'WORKSHOP-ORIGINAL')
        # Canonical state starts with no members; files are projection only.
        (root / 'config/load_order').mkdir()
        (root / 'config/load_order/wh3.json').write_text('{"order": []}\n', encoding='utf8')
        (root / '.smm-deploy-sandbox').write_text(identity.internal_id, encoding='utf8')
    return resources, source, target, identity


def verify_state(resources, source, target, identity, deployed):
    from services.deploy_rules.manifest import load_manifest
    from services.order_backend import get_order_backend
    info = resources.db.get_mod_deploy_info(identity.mod_id)
    assert info.deploy_status == ('deployed' if deployed else 'not_deployed')
    assert target.read_bytes() == b'WORKSHOP-ORIGINAL'
    assert (source / 'library-only.pack').read_bytes() == b'LIBRARY-PRESERVE'
    game = resources.root / 'games/Warhammer3'
    assert (game / 'data/original.pack').read_bytes() == b'GAME-ORIGINAL'
    assert sorted(p.name for p in (game/'data').iterdir()) == ['original.pack']
    assert load_manifest(source) is None  # WH3 activation deliberately has no copy manifest.
    backend = get_order_backend(APP_ID)
    with resources.bind():
        order = backend.get_current_order(db=resources.db, library_root=resources.path('mod'))
    assert order == ([identity.internal_id] if deployed else [])
    text = (game/'used_mods.txt').read_text('utf8')
    if deployed:
        assert 'mod "native.pack";' in text
        assert str(target.parent).replace('\\', '/') in text.replace('\\', '/')
        assert str(source).replace('\\', '/') not in text.replace('\\', '/')
        assert info.deploy_path.replace('\\', '/') == str(target.parent).replace('\\', '/')
    else:
        assert text == ''
    assert not list((resources.root/'data').glob('deploy_backup/**/*'))
    backup_root = resources.root/'data/mod_backup'
    if backup_root.exists():
        from uuid import UUID
        assert all(str(UUID(p.name)) == p.name for p in backup_root.iterdir())
        saved = json.loads((backup_root/identity.internal_id/'metadata.json').read_text('utf8'))
        assert saved['internal_id'] == identity.internal_id
    return {'status':info.deploy_status, 'projection':text, 'order':order,
        'native_sha256':hashlib.sha256(target.read_bytes()).hexdigest(), 'manifest':None}
