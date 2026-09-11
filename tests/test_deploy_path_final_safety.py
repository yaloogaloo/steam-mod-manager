"""Deploy Path Canonicalization — final safety / lifecycle audit tests.

tmp_path + SMM_TEST_DB only. Never touches production DB or SteamLibrary.
Never executes against real D:/F: game trees.
"""
from __future__ import annotations
import inspect
import json
import os
import zipfile
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager, GameDeployConfig
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from services.deploy import ModDeployer
from services.deploy_paths import ROOT_KIND_GAME_MODS, DeployPathError, derive_legacy_relative, project_relative, remap_manifest_targets, resolve_deploy_managed_path
from services.deploy_rules import load_manifest
from services.deploy_rules.anno import ANNO_1800_APP_ID, Anno1800Strategy
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, save_manifest
from services.deploy_security import ManifestSecurityError, collect_allowed_target_roots, validate_manifest_for_save, validate_manifest_targets
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.importers.archive import extract_archive
from tests.helpers.identity import create_steam_test_mod

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'final_safety.db')
    yield manager
    manager.close()
    DatabaseManager.reset_instance()

def _write_meta(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def _prove_folder(db: DatabaseManager, mid: str, folder: Path, *, extra: dict | None = None) -> None:
    """Stamp .info + DB internal_id so Deploy resolve accepts last_known_path."""
    proof = str(mid)
    payload = {'internal_id': proof, 'published_file_id': mid}
    if extra:
        payload.update(extra)
    _write_meta(folder, payload)
    db.update_mod_identity_fields(mid, internal_id=proof, last_known_path=str(folder), folder_present=True)

def test_audit1_same_workspace_id_different_platform_no_cross_match(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    shared_ws = '555666777'
    steam_folder = library / 'Palworld' / 'SteamMod'
    steam_folder.mkdir(parents=True)
    (steam_folder / 'a.pak').write_bytes(b'steam')
    _write_meta(steam_folder, {'workspace_id': shared_ws, 'published_file_id': shared_ws, 'platform': 'steam', 'source_type': 'steam', 'app_id': 1623730, 'title': 'SteamMod', 'internal_id': 'steam-only'})
    modio_folder = library / 'Anno 1800' / 'ModioMod'
    modio_folder.mkdir(parents=True)
    (modio_folder / 'data').mkdir()
    (modio_folder / 'data' / 'x.xml').write_text('<x/>', encoding='utf-8')
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id='4503801', source_url='https://mod.io/g/anno-1800/m/4503801', title='ModioMod', app_id=ANNO_1800_APP_ID, game_name='Anno 1800', operation='import')
    internal = str(created.mod_id)
    db.update_mod_identity_fields(internal, workspace_id=shared_ws, last_known_path='', folder_present=True, platform=PLATFORM_MODIO, app_id=ANNO_1800_APP_ID, internal_id=f'uuid-{internal}')
    db.update_mod_content_status(internal, content_status='healthy')
    _write_meta(modio_folder, {'workspace_id': shared_ws, 'source_type': 'modio', 'platform': 'modio', 'app_id': ANNO_1800_APP_ID, 'title': 'ModioMod', 'internal_id': f'uuid-{internal}'})
    found = resolve_deploy_managed_path(internal, db=db, library_root=library)
    assert found is not None
    assert found.resolve() == modio_folder.resolve()
    assert found.resolve() != steam_folder.resolve()

def test_audit1_same_workspace_id_different_app_id_no_cross_game(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    shared_ws = 'ws-cross-game-1'
    bg3 = library / "Baldur's Gate 3" / 'SharedName'
    bg3.mkdir(parents=True)
    (bg3 / 'mod.json').write_text('{}', encoding='utf-8')
    _write_meta(bg3, {'workspace_id': shared_ws, 'source_type': 'modio', 'platform': 'modio', 'app_id': 1086940, 'title': 'BG3', 'internal_id': 'bg3-uuid'})
    anno = library / 'Anno 1800' / 'SharedName'
    anno.mkdir(parents=True)
    (anno / 'data').mkdir()
    (anno / 'data' / 'a.xml').write_text('<a/>', encoding='utf-8')
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id='4503802', source_url='https://mod.io/g/anno-1800/m/4503802', title='AnnoShared', app_id=ANNO_1800_APP_ID, game_name='Anno 1800', operation='import')
    internal = str(created.mod_id)
    db.update_mod_identity_fields(internal, workspace_id=shared_ws, last_known_path='', folder_present=True, platform=PLATFORM_MODIO, app_id=ANNO_1800_APP_ID, internal_id=f'uuid-{internal}')
    db.update_mod_content_status(internal, content_status='healthy')
    _write_meta(anno, {'workspace_id': shared_ws, 'source_type': 'modio', 'platform': 'modio', 'app_id': ANNO_1800_APP_ID, 'title': 'AnnoShared', 'internal_id': f'uuid-{internal}'})
    found = resolve_deploy_managed_path(internal, db=db, library_root=library)
    assert found is not None
    assert found.resolve() == anno.resolve()

def test_audit1_modio_without_published_file_id_resolves(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    folder = library / 'Anno 1800' / 'NoPub'
    folder.mkdir(parents=True)
    (folder / 'data').mkdir()
    (folder / 'data' / 'm.json').write_text('{}', encoding='utf-8')
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id='4503803', source_url='https://mod.io/g/anno-1800/m/4503803', title='NoPub', app_id=ANNO_1800_APP_ID, game_name='Anno 1800', operation='import')
    internal = str(created.mod_id)
    workspace_id = str(created.workspace_id or '').strip()
    proof = f'uuid-{internal}'
    db.update_mod_identity_fields(
        internal,
        last_known_path=str(folder),
        folder_present=True,
        platform=PLATFORM_MODIO,
        internal_id=proof,
    )
    db.update_mod_content_status(internal, content_status='healthy')
    _write_meta(
        folder,
        {
            'workspace_id': workspace_id,
            'source_type': 'modio',
            'app_id': ANNO_1800_APP_ID,
            'internal_id': proof,
        },
    )
    meta = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding='utf-8'))
    assert not str(meta.get('published_file_id') or '').strip()
    found = resolve_deploy_managed_path(internal, db=db, library_root=library)
    assert found is not None
    assert found.resolve() == folder.resolve()

def test_audit2_valid_anno_shaped_legacy_remaps(tmp_path: Path) -> None:
    old = tmp_path / 'old' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    new = tmp_path / 'new' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    old_file = old / 'Foo' / 'a.xml'
    old_file.parent.mkdir(parents=True)
    old_file.write_text('OLD', encoding='utf-8')
    live = new / 'Foo' / 'a.xml'
    live.parent.mkdir(parents=True)
    live.write_text('LIVE', encoding='utf-8')
    derived = derive_legacy_relative(old_file, new)
    assert derived == 'Foo/a.xml'
    assert project_relative(new, derived) == live.resolve()

def test_audit2_unrelated_other_mods_rejected(tmp_path: Path) -> None:
    current = tmp_path / 'new' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    current.mkdir(parents=True)
    unrelated = tmp_path / 'other' / 'mods' / 'Foo' / 'a.xml'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('X', encoding='utf-8')
    assert derive_legacy_relative(unrelated, current) is None

def test_audit2_cross_drive_unrelated_and_traversal_rejected(tmp_path: Path) -> None:
    current = tmp_path / 'new' / 'game' / 'mods'
    current.mkdir(parents=True)
    assert derive_legacy_relative(tmp_path / 'secret' / 'gone.xml', current) is None
    assert derive_legacy_relative(current / '..' / 'outside.txt', current) is None
    bare = tmp_path / 'mods'
    bare.mkdir()
    assert derive_legacy_relative(tmp_path / 'old' / 'mods' / 'Foo' / 'a.xml', bare) is None

def test_audit2_ambiguous_or_failed_remap_does_not_delete(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    new_mods = tmp_path / 'new' / 'game' / 'mods'
    new_mods.mkdir(parents=True)
    secret = tmp_path / 'elsewhere' / 'keep.xml'
    secret.parent.mkdir(parents=True)
    secret.write_text('KEEP', encoding='utf-8')
    folder = library / 'Game' / 'FailRemap'
    folder.mkdir(parents=True)
    (folder / 'a.xml').write_text('A', encoding='utf-8')
    _write_meta(folder, {'published_file_id': '92001', 'app_id': 4242, 'game_name': 'Game'})
    db.update_game_deploy_config(4242, name='Game', install_path=str(tmp_path / 'new' / 'game'), mod_path=str(new_mods), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92001', title='FailRemap', app_id=4242)
    db.update_mod_identity_fields('92001', last_known_path=str(folder), folder_present=True)
    save_manifest(folder, DeployManifest(mod_id='92001', deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder / 'a.xml'), target=str(secret.resolve()))]))
    out = ModDeployer(library_root=library, db=db).undeploy_mod('92001')
    assert out.get('success') is False, out
    assert secret.read_text(encoding='utf-8') == 'KEEP'
    assert (folder / INFO_DIR_NAME / 'deploy_manifest.json').is_file()

def test_audit3_relative_traversal_and_absolute_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'game' / 'mods'
    root.mkdir(parents=True)
    for bad in ('../outside.txt', 'Foo/../../outside.txt'):
        with pytest.raises(DeployPathError):
            project_relative(root, bad)
    with pytest.raises(DeployPathError):
        project_relative(root, str(tmp_path / 'outside.txt'))
    if os.name == 'nt':
        with pytest.raises(DeployPathError):
            project_relative(root, 'D:/Windows/outside.txt')

def test_audit3_symlink_escape_rejected_and_no_deletion(tmp_path: Path) -> None:
    root = tmp_path / 'game' / 'mods'
    outside = tmp_path / 'outside'
    outside.mkdir(parents=True)
    secret = outside / 'secret.txt'
    secret.write_text('SECRET', encoding='utf-8')
    foo = root / 'Foo'
    foo.mkdir(parents=True)
    link = foo / 'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'symlink not permitted: {exc}')
    with pytest.raises(DeployPathError):
        project_relative(root, 'Foo/link/secret.txt')
    assert secret.read_text(encoding='utf-8') == 'SECRET'

def test_audit3_validation_failure_preserves_manifest_and_files(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    mods = tmp_path / 'new' / 'game' / 'mods'
    mods.mkdir(parents=True)
    live = mods / 'Safe' / 'ok.xml'
    live.parent.mkdir(parents=True)
    live.write_text('OK', encoding='utf-8')
    folder = library / 'Game' / 'Trav'
    folder.mkdir(parents=True)
    (folder / 'a.xml').write_text('A', encoding='utf-8')
    _write_meta(folder, {'published_file_id': '92002', 'app_id': 4242, 'game_name': 'Game'})
    db.update_game_deploy_config(4242, name='Game', install_path=str(tmp_path / 'new' / 'game'), mod_path=str(mods), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92002', title='Trav', app_id=4242)
    db.update_mod_identity_fields('92002', last_known_path=str(folder), folder_present=True)
    save_manifest(folder, DeployManifest(mod_id='92002', deploy_time='t', deploy_type='folder_copy', files=[ManifestFileEntry(source=str(folder / 'a.xml'), target=str(live), root_kind=ROOT_KIND_GAME_MODS, relative='../outside.txt')]))
    cfg = db.get_game_deploy_config(4242)
    ctx = DeployContext(internal_id='92002', source=folder, app_id=4242, config=cfg, deploy_type='folder_copy', managed_path=folder)
    man = load_manifest(folder)
    assert man is not None
    with pytest.raises((DeployPathError, ManifestSecurityError)):
        remap_manifest_targets(man, ctx)
    assert live.read_text(encoding='utf-8') == 'OK'
    assert (folder / INFO_DIR_NAME / 'deploy_manifest.json').is_file()

def test_audit4_safe_and_malicious_zip_entries(tmp_path: Path) -> None:
    safe_zip = tmp_path / 'safe.zip'
    with zipfile.ZipFile(safe_zip, 'w') as zf:
        zf.writestr('Foo/data/a.xml', '<A/>')
    dest_safe = tmp_path / 'out_safe'
    dest_safe.mkdir()
    extract_archive(safe_zip, dest_dir=dest_safe)
    assert (dest_safe / 'Foo' / 'data' / 'a.xml').is_file()
    for name in ('../../outside.txt', 'Foo/../../outside.txt'):
        evil = tmp_path / f"evil_{name.replace('/', '_').replace('..', 'up')}.zip"
        with zipfile.ZipFile(evil, 'w') as zf:
            zf.writestr(name, 'EVIL')
        dest = tmp_path / f'out_{evil.stem}'
        dest.mkdir()
        outside = tmp_path / 'outside.txt'
        if outside.exists():
            outside.unlink()
        with pytest.raises(RuntimeError, match='不安全'):
            extract_archive(evil, dest_dir=dest)
        assert not outside.exists()
    abs_zip = tmp_path / 'abs.zip'
    with zipfile.ZipFile(abs_zip, 'w') as zf:
        zf.writestr('C:/Windows/Temp/smm_zipslip_probe.txt', 'ABS')
    dest_abs = tmp_path / 'out_abs'
    dest_abs.mkdir()
    probe = Path('C:/Windows/Temp/smm_zipslip_probe.txt')
    existed = probe.exists()
    with pytest.raises(RuntimeError, match='不安全'):
        extract_archive(abs_zip, dest_dir=dest_abs)
    if not existed:
        assert not probe.exists()

def test_audit5_normal_lifecycle_deploy_undeploy_redeploy(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    folder = library / 'Anno 1800' / 'LifeCycle'
    folder.mkdir(parents=True)
    (folder / 'data').mkdir()
    (folder / 'data' / 'a.xml').write_text('<A/>', encoding='utf-8')
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92101', title='LifeCycle', app_id=ANNO_1800_APP_ID)
    _prove_folder(
        db,
        '92101',
        folder,
        extra={'app_id': ANNO_1800_APP_ID, 'game_name': 'Anno 1800'},
    )
    deployer = ModDeployer(library_root=library, db=db)
    d1 = deployer.deploy_mod('92101')
    assert d1.get('success') is True, d1
    man = load_manifest(folder)
    assert man is not None
    assert man.schema_version == 2
    targets = {Path(e.target).resolve() for e in man.files}
    assert all((t.exists() for t in targets))
    rels = {e.relative.replace('\\', '/') for e in man.files}
    und = deployer.undeploy_mod('92101')
    assert und.get('success') is True, und
    assert all((not t.exists() for t in targets))
    d2 = deployer.deploy_mod('92101')
    assert d2.get('success') is True, d2
    man2 = load_manifest(folder)
    assert man2 is not None
    rels2 = {e.relative.replace('\\', '/') for e in man2.files}
    assert rels2 == rels

def test_audit5_drive_migration_style_remap_and_redeploy(db: DatabaseManager, tmp_path: Path) -> None:
    """Simulate D→F via tmp old/new trees — never real SteamLibrary drives."""
    library = tmp_path / 'library'
    old_mods = tmp_path / 'D_sim' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    new_mods = tmp_path / 'F_sim' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    old_target = old_mods / 'Foo' / 'a.xml'
    old_target.parent.mkdir(parents=True)
    old_target.write_text('OLD_D', encoding='utf-8')
    live = new_mods / 'Foo' / 'a.xml'
    live.parent.mkdir(parents=True)
    live.write_text('LIVE_F', encoding='utf-8')
    folder = library / 'Anno 1800' / 'Migrate'
    folder.mkdir(parents=True)
    (folder / 'a.xml').write_text('SRC', encoding='utf-8')
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(new_mods.parent), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92102', title='Migrate', app_id=ANNO_1800_APP_ID)
    _prove_folder(
        db,
        '92102',
        folder,
        extra={'app_id': ANNO_1800_APP_ID, 'game_name': 'Anno 1800'},
    )
    save_manifest(folder, DeployManifest(mod_id='92102', deploy_time='t', deploy_type='anno_1800', files=[ManifestFileEntry(source=str(folder / 'a.xml'), target=str(old_target))]))
    cfg = db.get_game_deploy_config(ANNO_1800_APP_ID)
    allowed_before = {str(p.resolve()) for p in collect_allowed_target_roots(DeployContext(internal_id='92102', source=folder, app_id=ANNO_1800_APP_ID, config=cfg, deploy_type='anno_1800', managed_path=folder))}
    assert not any(('D_sim' in p for p in allowed_before))
    deployer = ModDeployer(library_root=library, db=db)
    und = deployer.undeploy_mod('92102')
    assert und.get('success') is True, und
    assert not live.exists()
    assert old_target.read_text(encoding='utf-8') == 'OLD_D'
    (folder / 'data').mkdir(exist_ok=True)
    (folder / 'data' / 'b.xml').write_text('<B/>', encoding='utf-8')
    dep = deployer.deploy_mod('92102')
    assert dep.get('success') is True, dep
    man = load_manifest(folder)
    assert man is not None
    for entry in man.files:
        assert 'F_sim' in entry.target.replace('\\', '/')
        assert 'D_sim' not in entry.target.replace('\\', '/')
    cfg2 = db.get_game_deploy_config(ANNO_1800_APP_ID)
    allowed_after = {str(p.resolve()) for p in collect_allowed_target_roots(DeployContext(internal_id='92102', source=folder, app_id=ANNO_1800_APP_ID, config=cfg2, deploy_type='anno_1800', managed_path=folder))}
    assert not any(('D_sim' in p for p in allowed_after))

def test_audit5_failed_remap_blocks_redeploy_without_deletion(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    new_mods = tmp_path / 'F_sim' / 'SteamLibrary' / 'steamapps' / 'common' / 'Anno 1800' / 'mods'
    new_mods.mkdir(parents=True)
    secret = tmp_path / 'orphan' / 'keep.xml'
    secret.parent.mkdir(parents=True)
    secret.write_text('KEEP', encoding='utf-8')
    folder = library / 'Anno 1800' / 'BadLegacy'
    folder.mkdir(parents=True)
    (folder / 'a.xml').write_text('A', encoding='utf-8')
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(new_mods.parent), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92103', title='BadLegacy', app_id=ANNO_1800_APP_ID)
    _prove_folder(
        db,
        '92103',
        folder,
        extra={'app_id': ANNO_1800_APP_ID, 'game_name': 'Anno 1800'},
    )
    save_manifest(folder, DeployManifest(mod_id='92103', deploy_time='t', deploy_type='anno_1800', files=[ManifestFileEntry(source=str(folder / 'a.xml'), target=str(secret.resolve()))]))
    out = ModDeployer(library_root=library, db=db).redeploy_mod('92103')
    assert out.get('success') is False, out
    assert secret.read_text(encoding='utf-8') == 'KEEP'
    assert (folder / INFO_DIR_NAME / 'deploy_manifest.json').is_file()

def test_audit6_archive_uses_zip_root_not_library_folder_name(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    install.mkdir()
    lib_name = '1905 - Trans-Ocean Liner'
    zip_root = '[Gameplay] 1905 - Trans-Ocean Liner'
    folder = library / 'Anno 1800' / lib_name
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / 'm.zip', 'w') as zf:
        zf.writestr(f'{zip_root}/data/x.xml', '<X/>')
    db.update_game_deploy_config(ANNO_1800_APP_ID, name='Anno 1800', install_path=str(install), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='92201', title=lib_name, app_id=ANNO_1800_APP_ID)
    _prove_folder(
        db,
        '92201',
        folder,
        extra={'app_id': ANNO_1800_APP_ID, 'game_name': 'Anno 1800'},
    )
    out = ModDeployer(library_root=library, db=db).deploy_mod('92201')
    assert out.get('success') is True, out
    man = load_manifest(folder)
    assert man is not None
    rels = {e.relative.replace('\\', '/') for e in man.files}
    assert any((r.startswith(zip_root + '/') for r in rels))
    assert not any((lib_name == Path(r).parts[0] for r in rels))
    assert not (install / 'mods' / lib_name).exists()
    assert (install / 'mods' / zip_root / 'data' / 'x.xml').is_file()

def test_audit6_architecture_guard_anno_archive_not_library_wrapper() -> None:
    from services.deploy_rules import anno as anno_mod
    src = inspect.getsource(Anno1800Strategy.deploy)
    assert 'inert_strategy_deploy' in src
    plan_src = inspect.getsource(anno_mod._plan_anno_archive_deploy)
    assert 'iter_archive_members' in plan_src
    assert 'ArchiveExtractor' not in plan_src
    assert 'ctx.library_folder().name' not in plan_src
    resolve_src = inspect.getsource(__import__('services.deploy', fromlist=['ModDeployer']).ModDeployer._resolve_context)
    assert 'defer_archive_extract' in resolve_src

def test_audit7_save_validation_requires_plan_not_manifest_self() -> None:
    from services.deploy import ModDeployer
    from services.deploy_security import validate_manifest_for_save
    src = ''.join(inspect.getsource(validate_manifest_for_save).split())
    assert 'planned_targets=[e.targetforeinmanifest.files]' not in src
    body = ''.join(inspect.getsource(ModDeployer._deploy_with_context).split())
    assert 'planned_absolute_targets(planned.files)' in body
    assert 'planned_targets=[e.targetforeinmanifest.files]' not in body
    assert 'attach_canonical_targets' in body
    und = ''.join(inspect.getsource(ModDeployer._undeploy_mod_body).split())
    assert 'remap_manifest_targets(manifest,ctx)' in und

def test_audit8_deploy_entrypoints_use_deploy_paths_choke() -> None:
    from services import deploy as deploy_mod
    from services import deploy_status as status_mod
    from services import mod_source_integrity as msi
    deploy_src = inspect.getsource(deploy_mod)
    assert 'resolve_deploy_managed_path' in deploy_src
    assert 'resolve_deploy_identity' in deploy_src
    resolve_src = inspect.getsource(deploy_mod.ModDeployer._resolve_context)
    assert 'resolve_deploy_managed_path' in resolve_src
    assert 'find_by_published_id' not in resolve_src
    status_src = inspect.getsource(status_mod.resolve_deployment_status)
    assert 'resolve_deploy_managed_path' in status_src
    msi_src = inspect.getsource(msi._resolve_managed_path)
    assert 'resolve_deploy_managed_path' in msi_src
    paths_src = inspect.getsource(__import__('services.deploy_paths', fromlist=['resolve_deploy_managed_path']).resolve_deploy_managed_path)
    assert 'find_by_published_id' not in paths_src
    assert '_scan_library_sidecar' not in paths_src
    assert 'workspace_id' not in paths_src or 'resolve_managed_folder' in paths_src
    assert 'resolve_managed_folder' in paths_src
