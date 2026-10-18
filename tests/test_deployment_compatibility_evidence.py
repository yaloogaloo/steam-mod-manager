"""Historical v2 ownership/content proof and read-only uncertainty regression."""
import hashlib,json
from pathlib import Path
import pytest
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest,ManifestFileEntry,save_manifest
from services.deploy_manifest_compat import validate_legacy_v2
from services.deployment_inspection import inspect_deployment
from tools.isolated_deploy_validation import initialize

@pytest.fixture
def legacy(tmp_path):
    r,source,target,identity=initialize(tmp_path/'legacy')
    file=next(p for p in source.rglob('*') if p.is_file() and '.info' not in p.parts)
    deployed=target;deployed.parent.mkdir(parents=True,exist_ok=True);deployed.write_bytes(file.read_bytes())
    manifest=DeployManifest(str(identity.mod_id),'2020','folder_copy',[
        ManifestFileEntry(str(file),str(deployed),type='folder_copy',source_relative=file.name,
                          source_hash=hashlib.sha256(file.read_bytes()).hexdigest())],internal_id=str(identity.mod_id),schema_version=2)
    with r.bind():save_manifest(source,manifest)
    config=r.db.get_game_deploy_config(42)
    ctx=DeployContext(internal_id=identity.internal_id,source=source,app_id=42,config=config,deploy_type='folder_copy',mod_pk=identity.mod_id)
    yield r,source,deployed,identity,ctx,manifest
    r.db.close()

def test_pk_stamped_v2_is_verified_without_writes(legacy):
    r,source,target,identity,ctx,manifest=legacy;path=source/'.info/deploy_manifest.json';before=path.read_bytes()
    with r.bind():assert validate_legacy_v2(manifest,ctx,r.db,verify_files=True) is manifest
    assert path.read_bytes()==before
    assert r.db.get_mod_deploy_info(identity.mod_id).deploy_status=='not_deployed'

@pytest.mark.parametrize('change',['target','source','identity','version','escape'])
def test_changed_or_unowned_history_cannot_authorize_removal(legacy,change):
    r,source,target,identity,ctx,manifest=legacy
    if change=='target':target.write_bytes(b'user edits')
    if change=='source':Path(manifest.files[0].source).write_bytes(b'new source')
    if change=='identity':manifest.internal_id='another entity'
    if change=='version':manifest.schema_version=0
    if change=='escape':manifest.files[0].source_relative='../other.txt'
    with r.bind(),pytest.raises((PermissionError,ValueError)):
        validate_legacy_v2(manifest,ctx,r.db,verify_files=True)

def test_unreadable_history_remains_unknown_not_undeployed(legacy):
    r,source,target,identity,ctx,manifest=legacy
    (source/'.info/deploy_manifest.json').write_text('{bad')
    before=r.db.get_mod_deploy_info(identity.mod_id).deploy_status
    result=inspect_deployment(r.db,identity.mod_id)
    assert result['state']=='unknown'
    assert r.db.get_mod_deploy_info(identity.mod_id).deploy_status==before

def test_existing_legacy_files_do_not_manufacture_deployed_membership(legacy):
    r,source,target,identity,ctx,manifest=legacy
    result=inspect_deployment(r.db,identity.mod_id)
    assert result['state']=='compatibility_required' and result['existing']==1
    target.unlink();result=inspect_deployment(r.db,identity.mod_id)
    assert result['state']=='inconsistent' and result['missing']==1
    assert r.db.get_mod_deploy_info(identity.mod_id).deploy_status=='not_deployed'

def test_paradox_partial_result_is_not_success(monkeypatch):
    from types import SimpleNamespace
    from services.deploy import ModDeployer
    monkeypatch.setattr('services.paradox_activation.consider_paradox_playset_notice',lambda *a,**k:SimpleNamespace(should_prompt=False))
    deployer=object.__new__(ModDeployer);monkeypatch.setattr(deployer,'_database',lambda:None)
    for report in (None,SimpleNamespace(errors=[],unresolved=['missing'],direct_dlc_load_written=True),SimpleNamespace(errors=['disk failed'],unresolved=[],direct_dlc_load_written=False)):
        result={'success':True};deployer._annotate_paradox_launcher_result(result,281990,report)
        assert result['success'] is False and result['partial_state_saved']
        assert result['game_load_verified'] is False

def test_bridge_preserves_failed_and_unknown_raw_state(legacy):
    from PySide6.QtWidgets import QApplication
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_bridge import LibraryBridge
    app=QApplication.instance() or QApplication([])
    r,source,target,identity,ctx,manifest=legacy;b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    try:
        for status in ('failed','unrecognized',''):
            r.db._conn.execute('UPDATE mods SET deploy_status=? WHERE mod_id=?',(status,identity.mod_id));r.db._conn.commit()
            assets=b.execute('library',{'game':42})['assets']
            row=next(a for a in assets if a['internal_id']==identity.internal_id)
            assert row['deploy_state']==status and row['deployed'] is False
            assert inspect_deployment(r.db,identity.mod_id)['state']=='unknown'
    finally:b.shutdown()

def test_legacy_backup_replay_uses_original_undeploy_and_restores_user_file(legacy):
    from services.deploy import ModDeployer
    from services.deploy_rules.manifest import ManifestBackupInfo
    r,source,target,identity,ctx,manifest=legacy
    backup=source/'.info/backups/original.bin';backup.parent.mkdir();backup.write_bytes(b'ORIGINAL USER FILE')
    manifest.files[0].backup=ManifestBackupInfo('.info/backups/original.bin',hashlib.sha256(backup.read_bytes()).hexdigest())
    with r.bind():
        save_manifest(source,manifest)
        validate_legacy_v2(manifest,ctx,r.db,verify_files=True)
        result=ModDeployer(library_root=r.path('mod'),db=r.db).undeploy_mod(identity.internal_id)
    assert result['success'],result
    assert target.read_bytes()==b'ORIGINAL USER FILE'
    assert (target.parent/'untouched.txt').read_text()=='KEEP'
    assert r.db.get_mod_deploy_info(identity.mod_id).deploy_status=='not_deployed'

def test_relocated_source_and_target_use_canonical_paths_without_rewriting_history(legacy):
    r,source,target,identity,ctx,manifest=legacy
    manifest.files[0].source=str(r.root/'old-archive'/source.name/'payload.txt')
    manifest.files[0].target=str(r.root/'old-drive'/'TestGame/mods'/source.name/'payload.txt')
    with r.bind():
        save_manifest(source,manifest);before=(source/'.info/deploy_manifest.json').read_bytes()
        validate_legacy_v2(manifest,ctx,r.db,verify_files=True)
    assert Path(manifest.files[0].target)==target
    assert (source/'.info/deploy_manifest.json').read_bytes()==before

def test_archive_proof_compares_member_content_instead_of_archive_hash_to_target(legacy):
    import zipfile
    r,source,target,identity,ctx,manifest=legacy
    archive=source/'content.zip'
    relative=target.relative_to(Path(ctx.config.mod_path)).as_posix()
    with zipfile.ZipFile(archive,'w') as z:z.writestr(relative,target.read_bytes())
    entry=manifest.files[0];entry.source=str(archive);entry.source_relative=archive.name;entry.type='archive';entry.source_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
    with r.bind():
        save_manifest(source,manifest)
        validate_legacy_v2(manifest,ctx,r.db,verify_files=True)
    target.write_bytes(b'changed by user')
    with r.bind(),pytest.raises(PermissionError):validate_legacy_v2(manifest,ctx,r.db,verify_files=True)

@pytest.mark.parametrize('saved',[True,False])
def test_worker_does_not_claim_membership_saved_when_service_reports_db_failure(legacy,saved):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.deploy_thread import DeployWorker
    app=QApplication.instance() or QApplication([])
    r,source,target,identity,ctx,manifest=legacy
    deployer=SimpleNamespace(deploy_mod=lambda _: {'success':False,'projection_errors':['failure'],'partial_state_saved':saved})
    worker=DeployWorker(identity.internal_id,library_root=r.path('mod'),deployer=deployer)
    result=worker._run_operation()
    assert result['success'] is False and result['partial_state_saved'] is saved
    assert ('已保存' in result['error']) is saved

@pytest.mark.parametrize('stamp',['','pk'])
def test_optional_historical_internal_stamp_still_requires_pk_folder_and_hash_proof(legacy,stamp):
    r,source,target,identity,ctx,manifest=legacy
    manifest.internal_id='' if stamp=='' else str(identity.mod_id)
    with r.bind():
        save_manifest(source,manifest)
        validate_legacy_v2(manifest,ctx,r.db,verify_files=True)

def test_shared_manifest_claim_cannot_authorize_legacy_undeploy(legacy):
    from tests.helpers.identity import create_steam_test_mod
    r,source,target,identity,ctx,manifest=legacy
    peer=create_steam_test_mod(r.db,external_id='902199',app_id=42,title='Other claimant')
    folder=r.path('mod/Game/Other claimant');folder.mkdir()
    r.db.update_mod_identity_fields(peer.mod_id,last_known_path=str(folder),folder_present=True)
    with r.bind():
        save_manifest(folder,DeployManifest(str(peer.mod_id),'2020','folder_copy',[
            ManifestFileEntry(str(source/'payload.txt'),str(target))],internal_id=peer.internal_id,schema_version=2))
        before=target.read_bytes()
        with pytest.raises(PermissionError,match='多个 Mod'):validate_legacy_v2(manifest,ctx,r.db,verify_files=True)
        assert target.read_bytes()==before

def test_empty_manifest_is_not_write_admission(legacy):
    from services.production_resources import ProductionResources
    r,source,target,identity,ctx,manifest=legacy;manifest.files=[]
    with r.bind():save_manifest(source,manifest)
    resources=ProductionResources(r.root,r.db,r.path('mod'))
    with pytest.raises(PermissionError,match='可核对文件'):resources.validate_deploy(identity.internal_id,scan=False)
    with pytest.raises(PermissionError):validate_legacy_v2(manifest,ctx,r.db,verify_files=False)
