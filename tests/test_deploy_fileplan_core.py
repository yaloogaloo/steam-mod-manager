"""Phase 2 Deploy Core — FilePlan / Apply / Verify / overwrite accounting."""
from __future__ import annotations
import inspect
import json
import zipfile
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_apply import apply_file_plan
from services.deploy_file_plan import OP_COPY, OP_EXTRACT_MEMBER, DeployFilePlan, DeployFilePlanEntry, file_plan_core_applicable, file_plan_from_strategy_result, manifest_from_file_plan
from services.deploy_rules.base import DeployContext, StrategyResult
from services.deploy_rules.manifest import ManifestFileEntry
from services.deploy_verifier import verify_file_plan
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from tests.helpers.identity import bind_managed_path, create_steam_test_mod
from services.library_status import CONTENT_HEALTHY

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / 'fileplan_core.db')
    yield manager
    manager.close()
    DatabaseManager.reset_instance()

def _prove_managed_folder(
    db: DatabaseManager,
    mid: str,
    folder: Path,
    *,
    extra: dict | None = None,
) -> None:
    """Stamp ``.info/entity_key`` so Deploy path resolve accepts the folder."""
    from services.mod_identity import set_entity_key

    proof = str(mid)
    payload = set_entity_key({'published_file_id': mid}, proof)
    if extra:
        payload.update(extra)
        payload = set_entity_key(payload, proof)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding='utf-8',
    )
    db.update_mod_identity_fields(
        mid,
        internal_id=proof,
        last_known_path=str(folder),
        folder_present=True,
    )

def test_file_plan_folder_apply_verify_manifest(tmp_path: Path) -> None:
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.txt').write_text('A', encoding='utf-8')
    nested = src / 'sub'
    nested.mkdir()
    (nested / 'b.txt').write_text('B', encoding='utf-8')
    dest_root = tmp_path / 'dest'
    dest_root.mkdir()
    plan = DeployFilePlan(internal_id='1', deploy_type='folder_copy', source=str(src), source_kind='folder', content_root=str(src), managed_path=str(src), target_root=str(dest_root), files=[DeployFilePlanEntry(source_relative='a.txt', target_relative='a.txt', target_absolute=str(dest_root / 'a.txt'), source=str(src / 'a.txt'), op=OP_COPY), DeployFilePlanEntry(source_relative='sub/b.txt', target_relative='sub/b.txt', target_absolute=str(dest_root / 'sub' / 'b.txt'), source=str(src / 'sub' / 'b.txt'), op=OP_COPY)])
    plan.refresh_planned_count()
    assert file_plan_core_applicable(plan)
    applied = apply_file_plan(plan, staging_parent=tmp_path / 'stage')
    assert applied.success
    assert applied.applied == 2
    assert plan.diagnostics.applied_files == 2
    verified = verify_file_plan(plan)
    assert verified.success
    assert verified.verified == 2
    assert plan.diagnostics.verified_files == 2
    man = manifest_from_file_plan(plan, deploy_time='t0')
    assert len(man.files) == 2
    assert {e.target for e in man.files} == {str(dest_root / 'a.txt'), str(dest_root / 'sub' / 'b.txt')}

def test_file_plan_overwrite_existing_identical_is_success(tmp_path: Path) -> None:
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'x.bin').write_bytes(b'same')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'x.bin').write_bytes(b'same')
    plan = DeployFilePlan(internal_id='2', deploy_type='folder_copy', source=str(src), target_root=str(dest), files=[DeployFilePlanEntry(source_relative='x.bin', target_relative='x.bin', target_absolute=str(dest / 'x.bin'), source=str(src / 'x.bin'), op=OP_COPY)])
    plan.refresh_planned_count()
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).verified == 1

def test_zip_extract_member_apply_nested(tmp_path: Path) -> None:
    zpath = tmp_path / 'mod.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('[Gameplay] Demo/data/a.txt', 'hello')
        zf.writestr('[Gameplay] Demo/data/nested/b.txt', 'world')
    mods = tmp_path / 'mods'
    mods.mkdir()
    entries = [DeployFilePlanEntry(source_relative='[Gameplay] Demo/data/a.txt', target_relative='[Gameplay] Demo/data/a.txt', target_absolute=str(mods / '[Gameplay] Demo' / 'data' / 'a.txt'), source=str(zpath), op=OP_EXTRACT_MEMBER, type='archive'), DeployFilePlanEntry(source_relative='[Gameplay] Demo/data/nested/b.txt', target_relative='[Gameplay] Demo/data/nested/b.txt', target_absolute=str(mods / '[Gameplay] Demo' / 'data' / 'nested' / 'b.txt'), source=str(zpath), op=OP_EXTRACT_MEMBER, type='archive')]
    plan = DeployFilePlan(internal_id='3', deploy_type='anno_1800', source=str(tmp_path), source_kind='zip', target_root=str(mods), archives=[str(zpath)], files=entries)
    plan.refresh_planned_count()
    assert file_plan_core_applicable(plan)
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).verified == 2
    assert (mods / '[Gameplay] Demo' / 'data' / 'a.txt').read_text(encoding='utf-8') == 'hello'

def test_anno_accident_regression_target_already_full(tmp_path: Path, db: DatabaseManager) -> None:
    """
    Permanent regression for the production failure mode:

    ZIP has N files AND game mods/ already contains all N files
    → deploy MUST succeed with planned == applied == verified == N.
    Do not clear the target directory to make the test pass.
    """
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    mods = install / 'mods'
    mods.mkdir(parents=True)
    folder = library / 'Anno 1800' / 'Ocean Liner'
    folder.mkdir(parents=True)
    n = 12
    root_name = '[Gameplay] Ocean Liner'
    zpath = folder / 'payload.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for i in range(n):
            member = f'{root_name}/data/file_{i:03d}.txt'
            zf.writestr(member, f'content-{i}')
    with zipfile.ZipFile(zpath, 'r') as zf:
        zf.extractall(mods)
    preexisting_files = [p for p in (mods / root_name).rglob('*') if p.is_file()]
    assert len(preexisting_files) == n
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id='1786890582763999', source_url='https://mod.io/g/anno-1800/m/ocean-liner-test', title='Ocean Liner', app_id=916440, game_name='Anno 1800', operation='import')
    mod_id = str(created.mod_id)
    _prove_managed_folder(
        db,
        mod_id,
        folder,
        extra={
            'workspace_id': created.workspace_id,
            'title': 'Ocean Liner',
            'app_id': 916440,
            'game_name': 'Anno 1800',
            'platform': 'modio',
            'source_type': 'modio',
        },
    )
    db.update_game_deploy_config(916440, name='Anno 1800', install_path=str(install), mod_path='', deploy_type='folder_copy')
    db.update_mod_identity_fields(mod_id, platform=PLATFORM_MODIO, app_id=916440)
    db.update_mod_content_status(mod_id, content_status=CONTENT_HEALTHY)
    out = ModDeployer(library_root=library, db=db).deploy_mod(mod_id)
    assert out.get('success') is True, out
    assert int(out.get('planned_files') or 0) == n, out
    assert int(out.get('applied_files') or 0) == n, out
    assert int(out.get('verified_files') or 0) == n, out
    assert int(out.get('copied_files') or 0) == n, out
    assert Path(out.get('target') or '').resolve() == mods.resolve()
    assert len([p for p in (mods / root_name).rglob('*') if p.is_file()]) == n

def test_folder_deploy_via_moddeployer_uses_fileplan_counts(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'library'
    mods = tmp_path / 'Mods'
    mods.mkdir()
    folder = library / 'Game' / 'ModA'
    folder.mkdir(parents=True)
    (folder / 'readme.txt').write_text('hi', encoding='utf-8')
    (folder / 'data').mkdir()
    (folder / 'data' / 'c.bin').write_bytes(b'\x00\x01')
    db.update_game_deploy_config(1, name='Game', install_path='', mod_path=str(mods), deploy_type='folder_copy')
    create_steam_test_mod(db, external_id='88011', title='ModA', app_id=1, game_name='Game')
    bind_managed_path(db, '88011', folder, title='ModA', game_name='Game')
    _prove_managed_folder(
        db,
        '88011',
        folder,
        extra={'title': 'ModA', 'app_id': 1, 'game_name': 'Game'},
    )
    db.update_mod_content_status('88011', content_status=CONTENT_HEALTHY)
    out = ModDeployer(library_root=library, db=db).deploy_mod('88011')
    assert out.get('success') is True, out
    assert int(out.get('planned_files') or 0) == 2
    assert int(out.get('applied_files') or 0) == 2
    assert int(out.get('verified_files') or 0) == 2
    assert (mods / 'ModA' / 'readme.txt').is_file()

def test_apply_failure_keeps_planned_diagnostics(tmp_path: Path) -> None:
    plan = DeployFilePlan(internal_id='9', deploy_type='folder_copy', source=str(tmp_path), target_root=str(tmp_path / 't'), files=[DeployFilePlanEntry(source_relative='missing.txt', target_relative='missing.txt', target_absolute=str(tmp_path / 't' / 'missing.txt'), source=str(tmp_path / 'nope.txt'), op=OP_COPY)])
    plan.refresh_planned_count()
    result = apply_file_plan(plan, staging_parent=tmp_path / 'stage')
    assert result.success is False
    assert plan.diagnostics.planned_files == 1
    assert plan.diagnostics.applied_files == 0
    assert plan.diagnostics.failed_files >= 1

def test_conversion_archive_entries_become_extract_member(tmp_path: Path, db: DatabaseManager) -> None:
    install = tmp_path / 'game'
    mods = install / 'mods'
    mods.mkdir(parents=True)
    zpath = tmp_path / 'a.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('Root/x.txt', 'x')
    db.update_game_deploy_config(916440, name='Anno', install_path=str(install), deploy_type='anno_1800')
    cfg = db.get_game_deploy_config(916440)
    assert cfg is not None
    ctx = DeployContext(internal_id='1', source=tmp_path, managed_path=tmp_path, app_id=916440, config=cfg, deploy_type='anno_1800')
    planned = StrategyResult(success=True, target=str(mods.resolve()), deploy_type='anno_1800', files=[ManifestFileEntry(source=str(zpath), target=str((mods / 'Root' / 'x.txt').resolve()), type='archive')])
    plan = file_plan_from_strategy_result(planned, ctx, archives=[zpath])
    assert len(plan.files) == 1
    assert plan.files[0].op == OP_EXTRACT_MEMBER
    assert plan.files[0].source_relative == 'Root/x.txt'
    assert file_plan_core_applicable(plan)

def test_core_apply_source_has_no_after_before_accounting() -> None:
    from services import deploy_apply
    from services import deploy_file_plan
    apply_src = inspect.getsource(deploy_apply)
    plan_src = inspect.getsource(deploy_file_plan)
    assert '_snapshot_files' not in apply_src
    assert 'after - before' not in apply_src
    assert '_snapshot_files' not in plan_src

def test_moddeployer_uses_fileplan_apply() -> None:
    from services.deploy import ModDeployer
    src = inspect.getsource(ModDeployer._deploy_with_context)
    assert 'file_plan_from_strategy_result' in src
    assert 'apply_file_plan' in src
    assert 'verify_file_plan' in src
    assert 'file_plan_core_applicable' in src

def test_architecture_guard_phase3_strategies_are_inert() -> None:
    from services.deploy_rules.anno import Anno1800Strategy
    from services.deploy_rules.custom import CustomPathStrategy
    from services.deploy_rules.generic import FolderCopyStrategy
    for cls in (FolderCopyStrategy, CustomPathStrategy, Anno1800Strategy):
        src = inspect.getsource(cls.deploy)
        assert 'inert_strategy_deploy' in src
        assert 'self.plan(' not in src

def test_partial_existing_targets_still_succeed(tmp_path: Path) -> None:
    """Regression B: 6 of 10 targets already exist → still SUCCESS for all 10."""
    zpath = tmp_path / 'mod.zip'
    mods = tmp_path / 'mods'
    mods.mkdir()
    root = '[Gameplay] Partial'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for i in range(10):
            zf.writestr(f'{root}/f{i}.txt', f'c{i}')
    with zipfile.ZipFile(zpath, 'r') as zf:
        for i in range(6):
            member = f'{root}/f{i}.txt'
            dest = mods / member
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))
    entries = []
    for i in range(10):
        member = f'{root}/f{i}.txt'
        entries.append(DeployFilePlanEntry(source_relative=member, target_relative=member, target_absolute=str(mods / member), source=str(zpath), op=OP_EXTRACT_MEMBER, type='archive'))
    plan = DeployFilePlan(internal_id='p', deploy_type='anno_1800', source=str(tmp_path), source_kind='zip', target_root=str(mods), archives=[str(zpath)], files=entries)
    plan.refresh_planned_count()
    assert apply_file_plan(plan, staging_parent=tmp_path / 'stage').success
    assert verify_file_plan(plan).verified == 10
    assert plan.diagnostics.planned_files == 10
    assert plan.diagnostics.applied_files == 10
    assert plan.diagnostics.verified_files == 10
    assert len([p for p in (mods / root).rglob('*') if p.is_file()]) == 10

def _anno_zip_moddeployer(tmp_path: Path, db: DatabaseManager, *, folder_name: str, zip_root: str, members: list[str], title: str, external_id: str, url_slug: str, preseed_all: bool) -> dict:
    """Fixture helper: Anno ZIP library mod → ModDeployer (temp dirs only)."""
    library = tmp_path / 'library'
    install = tmp_path / 'AnnoInstall'
    mods = install / 'mods'
    mods.mkdir(parents=True)
    folder = library / 'Anno 1800' / folder_name
    folder.mkdir(parents=True)
    zpath = folder / 'mod.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for rel in members:
            zf.writestr(f'{zip_root}/{rel}', f'payload:{rel}')
    if preseed_all:
        with zipfile.ZipFile(zpath, 'r') as zf:
            zf.extractall(mods)
        assert len([p for p in (mods / zip_root).rglob('*') if p.is_file()]) == len(members)
    created = create_mod_identity(db, platform=PLATFORM_MODIO, external_id=external_id, source_url=f'https://mod.io/g/anno-1800/m/{url_slug}', title=title, app_id=916440, game_name='Anno 1800', operation='import')
    mod_id = str(created.mod_id)
    _prove_managed_folder(
        db,
        mod_id,
        folder,
        extra={
            'workspace_id': created.workspace_id,
            'title': title,
            'app_id': 916440,
            'game_name': 'Anno 1800',
            'platform': 'modio',
            'source_type': 'modio',
        },
    )
    db.update_game_deploy_config(916440, name='Anno 1800', install_path=str(install), mod_path='', deploy_type='folder_copy')
    db.update_mod_identity_fields(mod_id, platform=PLATFORM_MODIO, app_id=916440)
    db.update_mod_content_status(mod_id, content_status=CONTENT_HEALTHY)
    out = ModDeployer(library_root=library, db=db).deploy_mod(mod_id)
    return out

def test_anno_fixture_ocean_liner_overwrite_succeeds(tmp_path: Path, db: DatabaseManager) -> None:
    """Regression D — Ocean Liner-shaped ZIP with targets already present."""
    members = ['data/config/export/main/asset/assets.xml', 'data/graphics/icon.png', 'modinfo.json']
    out = _anno_zip_moddeployer(tmp_path, db, folder_name='1905 - Trans-Ocean Liner', zip_root='[Gameplay] 1905 - Trans-Ocean Liner', members=members, title='1905-ocean-liner', external_id='1786890582763991', url_slug='ocean-liner-phase3', preseed_all=True)
    n = len(members)
    assert out.get('success') is True, out
    assert '压缩包解压后没有可部署的文件' not in str(out.get('error') or '')
    assert int(out.get('planned_files') or 0) == n
    assert int(out.get('applied_files') or 0) == n
    assert int(out.get('verified_files') or 0) == n

def test_anno_fixture_legendary_items_fresh_succeeds(tmp_path: Path, db: DatabaseManager) -> None:
    """Regression D/C — Legendary Items-shaped ZIP, empty target."""
    members = ['data/config/export/main/asset/assets.xml', 'data/config/gui/texts_english.xml', 'modinfo.json']
    out = _anno_zip_moddeployer(tmp_path, db, folder_name='21 Legendary Items (Lion053)', zip_root='[Gameplay] 21 new Legendary Items (Lion053)', members=members, title='21 Legendary Items', external_id='1786890582763992', url_slug='legendary-items-fresh', preseed_all=False)
    n = len(members)
    assert out.get('success') is True, out
    assert '压缩包解压后没有可部署的文件' not in str(out.get('error') or '')
    assert int(out.get('planned_files') or 0) == n
    assert int(out.get('applied_files') or 0) == n
    assert int(out.get('verified_files') or 0) == n

def test_anno_fixture_legendary_items_overwrite_succeeds(tmp_path: Path, db: DatabaseManager) -> None:
    """Regression D/A — Legendary Items-shaped ZIP, targets already present."""
    members = ['data/config/export/main/asset/assets.xml', 'data/config/gui/texts_english.xml', 'modinfo.json']
    out = _anno_zip_moddeployer(tmp_path, db, folder_name='21 Legendary Items (Lion053)', zip_root='[Gameplay] 21 new Legendary Items (Lion053)', members=members, title='21 Legendary Items', external_id='1786890582763993', url_slug='legendary-items-overwrite', preseed_all=True)
    n = len(members)
    assert out.get('success') is True, out
    assert '压缩包解压后没有可部署的文件' not in str(out.get('error') or '')
    assert int(out.get('planned_files') or 0) == n
    assert int(out.get('applied_files') or 0) == n
    assert int(out.get('verified_files') or 0) == n


def test_large_copy_hashes_during_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    from unittest.mock import patch

    from services import deploy_apply as apply_mod
    from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry
    from services.mod_source_integrity import enrich_manifest_source_hashes

    monkeypatch.setattr(apply_mod, "_HASH_WHILE_COPY_MIN_BYTES", 1)
    src = tmp_path / "src"
    src.mkdir()
    payload = b"PACKDATA" * 2048
    pack = src / "a.pack"
    pack.write_bytes(payload)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    plan = DeployFilePlan(
        internal_id="1",
        deploy_type="warhammer3_pack",
        source=str(src),
        source_kind="folder",
        content_root=str(src),
        managed_path=str(src),
        target_root=str(dest_root),
        files=[
            DeployFilePlanEntry(
                source_relative="a.pack",
                target_relative="a.pack",
                target_absolute=str(dest_root / "a.pack"),
                source=str(pack),
                op=OP_COPY,
            )
        ],
    )
    plan.refresh_planned_count()
    applied = apply_file_plan(plan, staging_parent=tmp_path / "stage")
    assert applied.success
    assert (dest_root / "a.pack").read_bytes() == payload
    key = str(pack.resolve())
    digest = hashlib.sha256(payload).hexdigest()
    assert apply_mod.current_apply_source_hashes()[key] == digest

    manifest = DeployManifest(
        mod_id="1",
        deploy_time="t0",
        deploy_type="warhammer3_pack",
        files=[
            ManifestFileEntry(
                source=str(pack),
                target=str(dest_root / "a.pack"),
                type="pack",
            )
        ],
    )
    with patch("services.mod_source_integrity._sha256_file") as hashed:
        enrich_manifest_source_hashes(manifest)
        hashed.assert_not_called()
    assert manifest.files[0].source_hash == digest


def test_extract_member_streams_to_target_without_apply_staging(tmp_path: Path) -> None:
    """OP_EXTRACT_MEMBER writes the planned file only; never builds apply_*."""
    staging = tmp_path / "stage"
    staging.mkdir()
    parts = ["[Gameplay] DeepMod", "data", *[f"seg_{i:02d}" for i in range(12)]]
    member = "/".join(parts) + "/payload.txt"
    unplanned = "/".join(parts) + "/secret.txt"
    zpath = tmp_path / "mod.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(member, "planned-bytes")
        zf.writestr(unplanned, "should-not-extract")
        zf.writestr("other/root.txt", "also-unplanned")
    dest_root = tmp_path / "out"
    dest = dest_root.joinpath(*member.split("/"))
    assert not dest.parent.exists()
    plan = DeployFilePlan(
        internal_id="d",
        deploy_type="folder_copy",
        source=str(tmp_path),
        source_kind="zip",
        target_root=str(dest_root),
        archives=[str(zpath)],
        files=[
            DeployFilePlanEntry(
                source_relative=member,
                target_relative=member,
                target_absolute=str(dest),
                source=str(zpath),
                op=OP_EXTRACT_MEMBER,
                type="archive",
            )
        ],
    )
    plan.refresh_planned_count()
    result = apply_file_plan(plan, staging_parent=staging)
    assert result.success, result.error
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "planned-bytes"
    assert not list(staging.glob("apply_*"))
    assert not (dest.parent / "secret.txt").exists()
    assert not (dest_root / "other" / "root.txt").exists()
    staging_abs = str((staging / f"apply_{'a' * 32}").joinpath(*member.split("/")))
    assert len(str(dest)) < len(staging_abs)


def test_extract_member_missing_archive_entry_fails(tmp_path: Path) -> None:
    zpath = tmp_path / "mod.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("keep/a.txt", "A")
    dest = tmp_path / "out" / "missing.txt"
    plan = DeployFilePlan(
        internal_id="m",
        deploy_type="folder_copy",
        source=str(tmp_path),
        source_kind="zip",
        target_root=str(tmp_path / "out"),
        archives=[str(zpath)],
        files=[
            DeployFilePlanEntry(
                source_relative="keep/missing.txt",
                target_relative="keep/missing.txt",
                target_absolute=str(dest),
                source=str(zpath),
                op=OP_EXTRACT_MEMBER,
                type="archive",
            )
        ],
    )
    plan.refresh_planned_count()
    result = apply_file_plan(plan, staging_parent=tmp_path / "stage")
    assert result.success is False
    assert "缺少成员" in (result.error or "")
    assert not dest.exists()
    assert not list((tmp_path / "stage").glob("apply_*"))


def test_extract_member_creates_missing_target_parent(tmp_path: Path) -> None:
    zpath = tmp_path / "mod.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("nested/inner/a.txt", "ok")
    dest = tmp_path / "mods" / "nested" / "inner" / "a.txt"
    assert not dest.parent.exists()
    plan = DeployFilePlan(
        internal_id="p",
        deploy_type="folder_copy",
        source=str(tmp_path),
        source_kind="zip",
        target_root=str(tmp_path / "mods"),
        archives=[str(zpath)],
        files=[
            DeployFilePlanEntry(
                source_relative="nested/inner/a.txt",
                target_relative="nested/inner/a.txt",
                target_absolute=str(dest),
                source=str(zpath),
                op=OP_EXTRACT_MEMBER,
                type="archive",
            )
        ],
    )
    plan.refresh_planned_count()
    result = apply_file_plan(plan)
    assert result.success, result.error
    assert dest.read_text(encoding="utf-8") == "ok"
