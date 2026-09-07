"""Phase 4 Deploy contract tests — permanent FilePlan / Apply / Verify lock."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO
from services.deploy import ModDeployer
from services.deploy_apply import apply_file_plan
from services.deploy_file_plan import OP_COPY, OP_EXTRACT_MEMBER, DeployFilePlan, DeployFilePlanEntry, file_plan_from_strategy_result, manifest_from_file_plan
from services.deploy_rules.base import DeployContext
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_verifier import verify_file_plan
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_status import CONTENT_HEALTHY

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / 'phase4_contract.db')
    yield manager
    manager.close()
    DatabaseManager.reset_instance()

def _archive_plan(tmp_path: Path, *, n: int, preseed: int, root: str='[Gameplay] Contract') -> tuple[DeployFilePlan, Path]:
    zpath = tmp_path / 'mod.zip'
    mods = tmp_path / 'mods'
    mods.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath, 'w') as zf:
        for i in range(n):
            zf.writestr(f'{root}/f{i}.txt', f'c{i}')
    if preseed:
        with zipfile.ZipFile(zpath, 'r') as zf:
            for i in range(preseed):
                member = f'{root}/f{i}.txt'
                dest = mods / member
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member))
    entries = [DeployFilePlanEntry(source_relative=f'{root}/f{i}.txt', target_relative=f'{root}/f{i}.txt', target_absolute=str(mods / root / f'f{i}.txt'), source=str(zpath), op=OP_EXTRACT_MEMBER, type='archive') for i in range(n)]
    plan = DeployFilePlan(internal_id='p4', deploy_type='anno_1800', source=str(tmp_path), source_kind='zip', target_root=str(mods), archives=[str(zpath)], files=entries)
    plan.refresh_planned_count()
    return (plan, mods)

def test_a_all_targets_already_exist(tmp_path: Path) -> None:
    plan, mods = _archive_plan(tmp_path, n=10, preseed=10)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    assert plan.diagnostics.planned_files == 10
    assert plan.diagnostics.applied_files == 10
    assert plan.diagnostics.verified_files == 10
    assert len([p for p in mods.rglob('*') if p.is_file()]) == 10

def test_b_partial_existing_targets(tmp_path: Path) -> None:
    plan, _mods = _archive_plan(tmp_path, n=10, preseed=6)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    assert plan.diagnostics.planned_files == 10
    assert plan.diagnostics.applied_files == 10
    assert plan.diagnostics.verified_files == 10

def test_c_empty_target(tmp_path: Path) -> None:
    plan, _mods = _archive_plan(tmp_path, n=10, preseed=0)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    assert plan.diagnostics.planned_files == 10
    assert plan.diagnostics.applied_files == 10
    assert plan.diagnostics.verified_files == 10

def test_d_verify_failure_keeps_diagnostics(tmp_path: Path) -> None:
    plan, mods = _archive_plan(tmp_path, n=10, preseed=0)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    victim = Path(plan.files[0].target_absolute)
    victim.unlink()
    result = verify_file_plan(plan)
    assert result.success is False
    assert result.failed >= 1
    assert plan.diagnostics.planned_files == 10
    assert plan.diagnostics.applied_files == 10
    assert plan.diagnostics.verified_files == 9
    assert plan.diagnostics.failed_files >= 1
    diag = plan.diagnostics_dict()
    assert diag.get('source')
    assert diag.get('target') or diag.get('target_root')
    assert int(diag.get('planned_files') or 0) == 10
    assert int(diag.get('applied_files') or 0) == 10
    assert int(diag.get('failed_files') or 0) >= 1
    assert str(diag.get('target') or diag.get('target_root') or '') != ''
    assert int(diag.get('planned_files') or 0) != 0

def test_manifest_equals_fileplan_on_success(tmp_path: Path) -> None:
    plan, _mods = _archive_plan(tmp_path, n=7, preseed=0)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    manifest = manifest_from_file_plan(plan, deploy_time='t0')
    assert len(manifest.files) == len(plan.files) == 7
    planned = {e.target_absolute for e in plan.files}
    recorded = {e.target for e in manifest.files}
    assert planned == recorded

def test_folder_deploy_fileplan_apply_verify(tmp_path: Path, db: DatabaseManager) -> None:
    from core.db_manager import GameDeployConfig
    src = tmp_path / 'lib' / 'ModA'
    src.mkdir(parents=True)
    (src / 'a.txt').write_text('a', encoding='utf-8')
    (src / 'b').mkdir()
    (src / 'b' / 'c.bin').write_bytes(b'\x01\x02')
    mods = tmp_path / 'Mods'
    mods.mkdir()
    cfg = GameDeployConfig(app_id=1, name='Game', install_path='', mod_path=str(mods), deploy_type='folder_copy')
    ctx = DeployContext(internal_id='1', source=src, app_id=1, config=cfg, deploy_type='folder_copy', managed_path=src)
    planned = FolderCopyStrategy().plan(ctx)
    assert planned.success, planned.error
    plan = file_plan_from_strategy_result(planned, ctx)
    assert all((e.op == OP_COPY for e in plan.files))
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    assert plan.diagnostics.planned_files == plan.diagnostics.applied_files
    assert plan.diagnostics.applied_files == plan.diagnostics.verified_files
    assert (mods / 'ModA' / 'a.txt').is_file()

def test_archive_deploy_uses_extract_member(tmp_path: Path) -> None:
    plan, mods = _archive_plan(tmp_path, n=3, preseed=0)
    assert all((e.op == OP_EXTRACT_MEMBER for e in plan.files))
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).success
    assert (mods / '[Gameplay] Contract' / 'f0.txt').is_file()

def _anno_shape_deploy(tmp_path: Path, db: DatabaseManager, *, folder_name: str, zip_root: str, members: list[str], title: str, external_id: str, url_slug: str, preseed_all: bool) -> dict:
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    mods = install / 'mods'
    mods.mkdir(parents=True)
    folder = library / 'Anno 1800' / folder_name
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    zpath = folder / 'mod.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for rel in members:
            zf.writestr(f'{zip_root}/{rel}', f'payload:{rel}')
    if preseed_all:
        with zipfile.ZipFile(zpath, 'r') as zf:
            zf.extractall(mods)
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id=external_id, source_url=f'https://mod.io/g/anno-1800/m/{url_slug}', title=title, app_id=916440, game_name='Anno 1800', operation='import')
    mid = str(created.mod_id)
    (info / METADATA_FILENAME).write_text(json.dumps({'internal_id': mid, 'published_file_id': mid, 'workspace_id': created.workspace_id, 'title': title, 'app_id': 916440, 'game_name': 'Anno 1800', 'platform': 'modio', 'source_type': 'modio'}), encoding='utf-8')
    db.update_game_deploy_config(916440, name='Anno 1800', install_path=str(install), mod_path='', deploy_type='folder_copy')
    db.update_mod_identity_fields(mid, internal_id=mid, folder_present=True, last_known_path=str(folder), platform=PLATFORM_MODIO, app_id=916440)
    db.update_mod_content_status(mid, content_status=CONTENT_HEALTHY)
    return ModDeployer(library_root=library, db=db).deploy_mod(mid)

def test_anno_ocean_liner_overwrite_contract(tmp_path: Path, db: DatabaseManager) -> None:
    members = ['data/config/export/main/asset/assets.xml', 'data/graphics/icon.png', 'modinfo.json']
    out = _anno_shape_deploy(tmp_path, db, folder_name='1905 - Trans-Ocean Liner', zip_root='[Gameplay] 1905 - Trans-Ocean Liner', members=members, title='1905-ocean-liner', external_id='1786890582764011', url_slug='p4-ocean-liner', preseed_all=True)
    n = len(members)
    assert out.get('success') is True, out
    assert '压缩包解压后没有可部署的文件' not in str(out.get('error') or '')
    assert int(out.get('planned_files') or 0) == n
    assert int(out.get('applied_files') or 0) == n
    assert int(out.get('verified_files') or 0) == n
    assert str(out.get('target') or '') != ''

def test_anno_legendary_items_overwrite_contract(tmp_path: Path, db: DatabaseManager) -> None:
    members = ['data/config/export/main/asset/assets.xml', 'data/config/gui/texts_english.xml', 'modinfo.json']
    out = _anno_shape_deploy(tmp_path, db, folder_name='21 Legendary Items (Lion053)', zip_root='[Gameplay] 21 new Legendary Items (Lion053)', members=members, title='21 Legendary Items', external_id='1786890582764012', url_slug='p4-legendary', preseed_all=True)
    n = len(members)
    assert out.get('success') is True, out
    assert '压缩包解压后没有可部署的文件' not in str(out.get('error') or '')
    assert int(out.get('planned_files') or 0) == n
    assert int(out.get('applied_files') or 0) == n
    assert int(out.get('verified_files') or 0) == n
