"""Deploy path canonicalization — focused tmp_path tests.

Never touches production DB or SteamLibrary drives.
"""
from __future__ import annotations
import inspect
import json
import zipfile
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO
from services.deploy import ModDeployer
from services.deploy_paths import MANIFEST_SCHEMA_VERSION, ROOT_KIND_GAME_MODS, DeployPathError, derive_legacy_relative, project_relative
from services.deploy_rules import load_manifest
from services.deploy_rules.anno import ANNO_1800_APP_ID
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, save_manifest
from services.deploy_security import ManifestSecurityError, collect_allowed_target_roots, validate_manifest_for_save, validate_manifest_targets
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict
from services.identity_service import create_mod_identity
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'canonical.db')
    yield manager
    manager.close()
    DatabaseManager.reset_instance()

def _meta(folder: Path, extra: dict | None=None) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload = {'title': folder.name, 'app_id': ANNO_1800_APP_ID, 'game_name': 'Anno 1800', 'source_type': 'modio'}
    if extra:
        payload.update(extra)
    (info / METADATA_FILENAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def _prove_managed_folder(
    db: DatabaseManager,
    mid: str,
    folder: Path,
    *,
    extra: dict | None = None,
) -> str:
    """Stamp ``.info/internal_id`` from Entity and bind path. Returns PK."""
    title = ""
    app_id = 0
    game_name = ""
    if extra:
        title = str(extra.get("title") or "")
        app_id = int(extra.get("app_id") or 0)
        game_name = str(extra.get("game_name") or "")
    return prove_managed_folder(
        db,
        folder,
        handle=mid,
        title=title or folder.name,
        app_id=app_id,
        game_name=game_name,
        extra=extra,
    )


def _seed_modio(db: DatabaseManager, folder: Path, *, external_id: str, title: str) -> tuple[str, str]:
    """Create a Mod.io row via the Identity Creation Gate. Returns (pk, workspace)."""
    created = create_mod_identity(
        db,
        platform=PLATFORM_MODIO,
        external_id=external_id,
        source_url=f"https://mod.io/g/anno-1800/m/{external_id}",
        title=title,
        app_id=ANNO_1800_APP_ID,
        game_name="Anno 1800",
        operation="import",
    )
    pk = str(created.mod_id).strip()
    workspace_id = str(created.workspace_id or "").strip()
    frozen = str(created.internal_id or "").strip()
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title=title,
        app_id=ANNO_1800_APP_ID,
        game_name="Anno 1800",
        platform=PLATFORM_MODIO,
        extra={
            "workspace_id": workspace_id,
            "source_type": "modio",
            "platform": PLATFORM_MODIO,
            "app_id": ANNO_1800_APP_ID,
            "title": title,
        },
    )
    # Mod.io deploy fixtures must not carry Steam published_file_id pollution.
    meta_path = folder / INFO_DIR_NAME / METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload.pop("published_file_id", None)
    payload["workspace_id"] = workspace_id
    payload["source_type"] = "modio"
    payload["internal_id"] = frozen
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    db.update_mod_identity_fields(pk, platform=PLATFORM_MODIO)
    db.update_mod_content_status(pk, content_status="healthy")
    return (pk, workspace_id)

def test_1_modio_source_resolution_without_published_file_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    folder = library / 'Anno 1800' / '21 Legendary Items'
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / 'payload.zip', 'w') as zf:
        zf.writestr('[Gameplay] 21 new Legendary Items/data/config/export/main/asset/assets.xml', '<A/>')
    _meta(folder, {'source_type': 'modio'})
    sidecar = read_info_metadata_dict(folder) or {}
    assert not str(sidecar.get('published_file_id') or '').strip()
    internal, workspace_id = _seed_modio(db, folder, external_id='4503767', title='21 Legendary Items')
    sidecar = read_info_metadata_dict(folder) or {}
    assert not str(sidecar.get('published_file_id') or '').strip()
    assert workspace_id
    assert str(sidecar.get('internal_id') or '').strip()
    assert str(sidecar.get('internal_id') or '').strip() != internal  # Entity UUID ≠ PK
    db.update_mod_identity_fields(internal, last_known_path=str(folder), folder_present=True)
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    out = ModDeployer(library_root=library, db=db).deploy_mod(internal)
    assert out.get('success') is True, out
    assert '源 Mod 目录不存在' not in str(out.get('error') or '')
    assert (install / 'mods' / '[Gameplay] 21 new Legendary Items' / 'data' / 'config' / 'export' / 'main' / 'asset' / 'assets.xml').is_file()

def test_2_internal_id_deploy_does_not_require_published_file_id(db: DatabaseManager, tmp_path: Path) -> None:
    """Deploy keys on Internal ID; workspace_id in .info is display-only proof."""
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    folder = library / 'Anno 1800' / 'WS Deploy'
    folder.mkdir(parents=True)
    (folder / 'data').mkdir()
    (folder / 'data' / 'mod.json').write_text('{}', encoding='utf-8')
    _internal, workspace_id = _seed_modio(db, folder, external_id='4503768', title='WS Deploy')
    assert _internal.isdigit()
    assert workspace_id
    sidecar = read_info_metadata_dict(folder) or {}
    frozen = str(sidecar.get('internal_id') or '').strip()
    assert frozen and frozen != _internal
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    before = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding='utf-8'))
    out = ModDeployer(library_root=library, db=db).deploy_mod(_internal)
    assert out.get('success') is True, out
    after = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding='utf-8'))
    assert not str(after.get('published_file_id') or '').strip()
    assert after.get('workspace_id') == before.get('workspace_id') == workspace_id

def test_3_drive_migration_remaps_relative_to_current_root(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    old_mods = tmp_path / 'old' / 'game' / 'mods'
    new_mods = tmp_path / 'new' / 'game' / 'mods'
    old_file = old_mods / 'Foo' / 'a.xml'
    old_file.parent.mkdir(parents=True)
    old_file.write_text('OLD', encoding='utf-8')
    live = new_mods / 'Foo' / 'a.xml'
    live.parent.mkdir(parents=True)
    live.write_text('LIVE', encoding='utf-8')
    folder = library / 'Game' / 'FooMod'
    folder.mkdir(parents=True)
    (folder / 'a.xml').write_text('SRC', encoding='utf-8')
    _meta(folder, {'published_file_id': '91031', 'app_id': 4242, 'game_name': 'Game'})
    db.update_game_deploy_config(4242, name='Game', install_path=str(tmp_path / 'new' / 'game'), mod_path=str(new_mods), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='91031', title='FooMod', app_id=4242)
    pk = _prove_managed_folder(db, str(created.mod_id), folder, extra={'app_id': 4242, 'title': 'FooMod'})
    save_manifest(folder, DeployManifest(mod_id=pk, deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder / 'a.xml'), target=str(old_file))]))
    out = ModDeployer(library_root=library, db=db).undeploy_mod(pk)
    assert out.get('success') is True, out
    assert not live.exists()
    assert old_file.read_text(encoding='utf-8') == 'OLD'

def test_4_path_traversal_rejected(tmp_path: Path) -> None:
    current = tmp_path / 'new' / 'game' / 'mods'
    current.mkdir(parents=True)
    assert derive_legacy_relative(current / '..' / 'outside.txt', current) is None
    with pytest.raises(DeployPathError):
        project_relative(current, '../outside.txt')

def test_5_different_drive_escape_not_approved(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    new_mods = tmp_path / 'new' / 'game' / 'mods'
    new_mods.mkdir(parents=True)
    secret = tmp_path / 'secret' / 'outside.txt'
    secret.parent.mkdir(parents=True)
    secret.write_text('SAFE', encoding='utf-8')
    folder = library / 'Game' / 'EscMod'
    folder.mkdir(parents=True)
    (folder / 'a.txt').write_text('M', encoding='utf-8')
    _meta(folder, {'published_file_id': '91032', 'app_id': 4242, 'game_name': 'Game'})
    db.update_game_deploy_config(4242, name='Game', install_path=str(tmp_path / 'new' / 'game'), mod_path=str(new_mods), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='91032', title='EscMod', app_id=4242)
    pk = _prove_managed_folder(db, str(created.mod_id), folder, extra={'app_id': 4242, 'title': 'EscMod'})
    save_manifest(folder, DeployManifest(mod_id=pk, deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder / 'a.txt'), target=str(secret.resolve()))]))
    out = ModDeployer(library_root=library, db=db).undeploy_mod(pk)
    assert out.get('success') is False, out
    err = str(out.get('error') or '')
    assert '安全校验' in err or 'outside' in err
    assert secret.read_text(encoding='utf-8') == 'SAFE'

def test_6_save_validation_does_not_self_approve() -> None:
    src = inspect.getsource(validate_manifest_for_save)
    compact = ''.join(src.split())
    assert 'planned_targets=[e.targetforeinmanifest.files]' not in compact
    body = inspect.getsource(ModDeployer._deploy_with_context)
    compact_body = ''.join(body.split())
    assert 'planned_absolute_targets(planned.files)' in compact_body
    assert 'planned_targets=[e.targetforeinmanifest.files]' not in compact_body

def test_7_anno_archive_root_relative(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    folder = library / 'Anno 1800' / '1905 - Trans-Ocean Liner'
    folder.mkdir(parents=True)
    zip_root = '[Gameplay] 1905 - Trans-Ocean Liner'
    with zipfile.ZipFile(folder / 'liner.zip', 'w') as zf:
        zf.writestr(f'{zip_root}/data/config/export/main/asset/assets.xml', '<A/>')
    _meta(folder, {'published_file_id': '91677', 'workspace_id': '91677', 'source_type': 'modio'})
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='91677', title='1905-ocean-liner', app_id=ANNO_1800_APP_ID)
    pk = _prove_managed_folder(db, str(created.mod_id), folder, extra={'app_id': ANNO_1800_APP_ID, 'title': '1905-ocean-liner'})
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get('success') is True, out
    man = load_manifest(folder)
    assert man is not None
    assert man.schema_version == MANIFEST_SCHEMA_VERSION
    rels = {e.relative.replace('\\', '/') for e in man.files}
    assert f'{zip_root}/data/config/export/main/asset/assets.xml' in rels
    assert all((e.root_kind == ROOT_KIND_GAME_MODS for e in man.files))
    target = install / 'mods' / zip_root / 'data' / 'config' / 'export' / 'main' / 'asset' / 'assets.xml'
    assert target.is_file()
    assert not (install / 'mods' / '1905 - Trans-Ocean Liner').exists()

def test_8_nested_data_does_not_report_missing_content(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    folder = library / 'Anno 1800' / 'Nested'
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / 'nested.zip', 'w') as zf:
        zf.writestr('[Gameplay] Pack/[Shared] Pools and Definitions/data/config/export/main/asset/assets.xml', '<S/>')
        zf.writestr('[Gameplay] Pack/data/config/export/main/asset/assets.xml', '<A/>')
    _meta(folder, {'published_file_id': '91678'})
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='91678', title='Nested', app_id=ANNO_1800_APP_ID)
    pk = _prove_managed_folder(db, str(created.mod_id), folder, extra={'app_id': ANNO_1800_APP_ID, 'title': 'Nested'})
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get('success') is True, out
    assert '内容目录不存在' not in str(out.get('error') or '')
    assert (install / 'mods' / '[Gameplay] Pack' / '[Shared] Pools and Definitions' / 'data' / 'config' / 'export' / 'main' / 'asset' / 'assets.xml').is_file()

def test_9_archive_vs_folder_copy_undeploy_matches_deploy(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    deployer = ModDeployer(library_root=library, db=db)
    zip_folder = library / 'Anno 1800' / '1905 - Trans-Ocean Liner'
    zip_folder.mkdir(parents=True)
    zip_root = '[Gameplay] 1905 - Trans-Ocean Liner'
    with zipfile.ZipFile(zip_folder / 'liner.zip', 'w') as zf:
        zf.writestr(f'{zip_root}/data/x.xml', '<A/>')
    _meta(zip_folder, {'published_file_id': '91679'})
    created = create_steam_test_mod(db, external_id='91679', title='liner', app_id=ANNO_1800_APP_ID)
    pk = _prove_managed_folder(db, str(created.mod_id), zip_folder, extra={'app_id': ANNO_1800_APP_ID, 'title': 'liner'})
    deployed = deployer.deploy_mod(pk)
    assert deployed.get('success') is True, deployed
    man = load_manifest(zip_folder)
    assert man is not None
    archive_targets = [Path(e.target).resolve() for e in man.files]
    assert all((t.exists() for t in archive_targets))
    und = deployer.undeploy_mod(pk)
    assert und.get('success') is True, und
    assert all((not t.exists() for t in archive_targets))
    loose = library / 'Anno 1800' / 'LooseFoo'
    loose.mkdir(parents=True)
    (loose / 'data').mkdir()
    (loose / 'data' / 'y.xml').write_text('<Y/>', encoding='utf-8')
    _meta(loose, {'published_file_id': '91680'})
    created = create_steam_test_mod(db, external_id='91680', title='LooseFoo', app_id=ANNO_1800_APP_ID)
    pk = _prove_managed_folder(db, str(created.mod_id), loose, extra={'app_id': ANNO_1800_APP_ID, 'title': 'LooseFoo'})
    deployed2 = deployer.deploy_mod(pk)
    assert deployed2.get('success') is True, deployed2
    man2 = load_manifest(loose)
    assert man2 is not None
    folder_targets = [Path(e.target).resolve() for e in man2.files]
    assert any(('LooseFoo' in t.parts for t in folder_targets))
    assert all(('[Gameplay]' not in part for t in folder_targets for part in t.parts))
    und2 = deployer.undeploy_mod(pk)
    assert und2.get('success') is True, und2
    assert all((not t.exists() for t in folder_targets))

def test_10_legacy_manifest_remap_or_refuse(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    new_mods = tmp_path / 'new' / 'game' / 'mods'
    live = new_mods / 'Bar' / 'b.xml'
    live.parent.mkdir(parents=True)
    live.write_text('LIVE', encoding='utf-8')
    unknown = tmp_path / 'elsewhere' / 'gone.xml'
    unknown.parent.mkdir()
    unknown.write_text('KEEP', encoding='utf-8')
    folder = library / 'Game' / 'BarMod'
    folder.mkdir(parents=True)
    (folder / 'b.xml').write_text('SRC', encoding='utf-8')
    _meta(folder, {'published_file_id': '91033', 'app_id': 4242, 'game_name': 'Game'})
    db.update_game_deploy_config(4242, name='Game', install_path=str(tmp_path / 'new' / 'game'), mod_path=str(new_mods), deploy_type='folder_copy')
    created = create_steam_test_mod(db, external_id='91033', title='BarMod', app_id=4242)
    pk = _prove_managed_folder(db, str(created.mod_id), folder, extra={'app_id': 4242, 'title': 'BarMod'})
    old_target = tmp_path / 'old' / 'game' / 'mods' / 'Bar' / 'b.xml'
    old_target.parent.mkdir(parents=True)
    old_target.write_text('HIST', encoding='utf-8')
    save_manifest(folder, DeployManifest(mod_id=pk, deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder / 'b.xml'), target=str(old_target))]))
    out = ModDeployer(library_root=library, db=db).undeploy_mod(pk)
    assert out.get('success') is True, out
    assert not live.exists()
    assert old_target.read_text(encoding='utf-8') == 'HIST'
    folder2 = library / 'Game' / 'NoDerive'
    folder2.mkdir(parents=True)
    (folder2 / 'c.xml').write_text('C', encoding='utf-8')
    _meta(folder2, {'published_file_id': '91034', 'app_id': 4242, 'game_name': 'Game'})
    created = create_steam_test_mod(db, external_id='91034', title='NoDerive', app_id=4242)
    pk = _prove_managed_folder(db, str(created.mod_id), folder2, extra={'app_id': 4242, 'title': 'NoDerive'})
    save_manifest(folder2, DeployManifest(mod_id=pk, deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder2 / 'c.xml'), target=str(unknown.resolve()))]))
    out2 = ModDeployer(library_root=library, db=db).undeploy_mod(pk)
    assert out2.get('success') is False, out2
    assert unknown.read_text(encoding='utf-8') == 'KEEP'

def test_self_approve_manifest_targets_rejected(tmp_path: Path, db: DatabaseManager) -> None:
    mods_root = tmp_path / 'GameMods'
    mods_root.mkdir()
    db.update_game_deploy_config(4242, name='Game', mod_path=str(mods_root), install_path=str(tmp_path / 'GameInstall'), deploy_type='folder_copy')
    (tmp_path / 'GameInstall').mkdir()
    mod = tmp_path / 'library' / 'Game' / 'X'
    mod.mkdir(parents=True)
    (mod / 'a.txt').write_text('A', encoding='utf-8')
    cfg = db.get_game_deploy_config(4242)
    ctx = DeployContext(internal_id='36834fcf-3cbb-4ffe-8b78-be1921638bd4', source=mod, app_id=4242, config=cfg, deploy_type='folder_copy', managed_path=mod)
    evil = tmp_path / 'escape.txt'
    man = DeployManifest(mod_id='1', deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(mod / 'a.txt'), target=str(evil.resolve()))])
    with pytest.raises(ManifestSecurityError, match='outside allowed'):
        validate_manifest_for_save(man, managed=mod, ctx=ctx)
    with pytest.raises(ManifestSecurityError, match='outside allowed'):
        validate_manifest_targets(man, allowed_roots=collect_allowed_target_roots(ctx))
