"""Civilization VI (AppID 289070) — Steam Workshop folder_copy deploy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    CIVILIZATION_VI_APP_IDS,
    PLATFORM_STEAM,
    is_civilization_vi_game,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import (
    CIVILIZATION_VI_APP_ID,
    DEPLOY_TYPE_FOLDER_COPY,
    FolderCopyStrategy,
    load_manifest,
    resolve_deploy_type,
    resolve_strategy,
)
from services.deploy_rules.base import DeployContext
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

CIV6 = CIVILIZATION_VI_APP_ID
assert CIV6 == 289070
assert CIV6 in CIVILIZATION_VI_APP_IDS


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "civ6_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_civ6(db: DatabaseManager, mod_path: Path) -> Path:
    mod_path.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        CIV6,
        name="文明Ⅵ",
        install_path=str(mod_path.parent / "Civ6Install"),
        mod_path=str(mod_path),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed_mod(
    library: Path,
    *,
    folder: str,
    mod_id: str,
    files: dict[str, str],
) -> Path:
    mod_dir = library / "文明Ⅵ" / folder
    mod_dir.mkdir(parents=True)
    info = mod_dir / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        (
            "{\n"
            f'  "internal_id": "{mod_id}",\n'
            f'  "published_file_id": "{mod_id}",\n'
            f'  "title": "{folder}",\n'
            f'  "app_id": {CIV6},\n'
            '  "game_name": "文明Ⅵ"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    (info / "manager_only.txt").write_text("skip", encoding="utf-8")
    for rel, data in files.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return mod_dir


def _prove_managed_folder(db: DatabaseManager, mid: str, folder: Path) -> None:
    """Stamp ``.info.internal_id`` so Deploy path resolve accepts the folder."""
    proof = str(mid)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    meta_path = info / METADATA_FILENAME
    payload: dict = {}
    if meta_path.is_file():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["internal_id"] = proof
    payload.setdefault("published_file_id", mid)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    db.update_mod_identity_fields(
        mid,
        internal_id=proof,
        last_known_path=str(folder),
        folder_present=True,
    )


def _register(
    db: DatabaseManager, *, mod_id: str, title: str, folder: Path
) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title=title,
            app_id=CIV6,
            game_name="文明Ⅵ",
        )
    )
    _prove_managed_folder(db, mod_id, folder)


# ---------------------------------------------------------------------------
# Identification / strategy registration
# ---------------------------------------------------------------------------


def test_is_civilization_vi_game() -> None:
    assert is_civilization_vi_game(game_id=289070)
    assert is_civilization_vi_game("文明Ⅵ")
    assert is_civilization_vi_game("Civilization VI")
    assert is_civilization_vi_game("Sid Meier's Civilization VI")
    assert not is_civilization_vi_game("Palworld")
    assert not is_civilization_vi_game(game_id=1623730)
    assert not is_civilization_vi_game("Civilization V")


def test_resolve_deploy_type_forces_folder_copy() -> None:
    assert resolve_deploy_type(CIV6, "folder_copy") == DEPLOY_TYPE_FOLDER_COPY
    assert resolve_deploy_type(CIV6, "palworld_pak") == DEPLOY_TYPE_FOLDER_COPY
    assert resolve_deploy_type(CIV6, "anno_1800") == DEPLOY_TYPE_FOLDER_COPY


def test_resolve_strategy_is_folder_copy(db: DatabaseManager, tmp_path: Path) -> None:
    mods = _configure_civ6(db, tmp_path / "ConfiguredCivMods")
    cfg = db.get_game_deploy_config(CIV6)
    assert cfg is not None
    source = tmp_path / "src" / "ModA"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    ctx = DeployContext(internal_id="289070001",
        app_id=CIV6,
        source=source,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        managed_path=source,
    )
    strategy = resolve_strategy(ctx)
    assert isinstance(strategy, FolderCopyStrategy)
    planned = strategy.plan(ctx)
    assert planned.success is True
    assert Path(planned.target) == (mods / "ModA").resolve()
    for entry in planned.files:
        assert Path(entry.target).resolve().is_relative_to(mods.resolve())


# ---------------------------------------------------------------------------
# Deploy scenarios
# ---------------------------------------------------------------------------


def test_empty_target_success(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsEmpty")
    folder = "ModA"
    source = _seed_mod(
        library,
        folder=folder,
        mod_id="289070101",
        files={
            "file1.txt": "one",
            "file2.txt": "two",
            "Assets/x.txt": "x",
            "Assets/y.txt": "y",
        },
    )
    _register(db, mod_id="289070101", title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod("289070101")
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY

    target = mods / folder
    assert (target / "file1.txt").is_file()
    assert (target / "file2.txt").is_file()
    assert (target / "Assets" / "x.txt").is_file()
    assert (target / "Assets" / "y.txt").is_file()
    assert not (target / INFO_DIR_NAME).exists()

    man = load_manifest(source)
    assert man is not None
    for entry in man.files:
        assert Path(entry.target).is_file()
    assert int(result.get("planned_files") or 0) == len(man.files)
    assert int(result.get("verified_files") or 0) == len(man.files)


def test_existing_target_redeploy_success(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsExisting")
    folder = "ModExisting"
    files = {f"f{i}.txt": f"v{i}" for i in range(5)}
    files["Assets/nested.txt"] = "n"
    source = _seed_mod(library, folder=folder, mod_id="289070102", files=files)
    _register(db, mod_id="289070102", title=folder, folder=source)

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod("289070102")
    assert first["success"] is True, first

    # All targets already present — redeploy must still SUCCESS.
    second = deployer.deploy_mod("289070102")
    assert second["success"] is True, second
    assert second["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY
    assert int(second.get("verified_files") or 0) == len(files)

    target = mods / folder
    for rel in files:
        assert (target / rel).is_file()
    assert load_manifest(source) is not None


def test_partial_existing_target_success(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPartial")
    folder = "ModPartial"
    files = {f"f{i}.txt": f"content-{i}" for i in range(10)}
    source = _seed_mod(library, folder=folder, mod_id="289070103", files=files)
    _register(db, mod_id="289070103", title=folder, folder=source)

    target = mods / folder
    target.mkdir(parents=True)
    for i in range(6):
        (target / f"f{i}.txt").write_text(f"old-{i}", encoding="utf-8")

    result = ModDeployer(library_root=library, db=db).deploy_mod("289070103")
    assert result["success"] is True, result
    assert int(result.get("planned_files") or 0) == 10
    assert int(result.get("verified_files") or 0) == 10

    for i in range(10):
        path = target / f"f{i}.txt"
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == f"content-{i}"

    man = load_manifest(source)
    assert man is not None
    assert len(man.files) == 10


def test_manifest_equals_fileplan(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    _configure_civ6(db, tmp_path / "CivModsManifest")
    folder = "ModManifest"
    files = {
        "a.txt": "a",
        "b.txt": "b",
        "Assets/c.txt": "c",
    }
    source = _seed_mod(library, folder=folder, mod_id="289070104", files=files)
    _register(db, mod_id="289070104", title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod("289070104")
    assert result["success"] is True, result

    man = load_manifest(source)
    assert man is not None
    assert man.deploy_type == DEPLOY_TYPE_FOLDER_COPY
    assert len(man.files) == len(files)
    assert int(result.get("planned_files") or 0) == len(man.files)
    assert int(result.get("applied_files") or 0) == len(man.files)
    assert int(result.get("verified_files") or 0) == len(man.files)

    # Manifest targets come from FilePlan — not a post-deploy filesystem scan.
    result_targets = {Path(f["target"]).resolve() for f in result.get("files") or []}
    manifest_targets = {Path(f.target).resolve() for f in man.files}
    assert result_targets == manifest_targets
    for entry in man.files:
        assert Path(entry.target).is_file()


def test_uses_configured_mod_path_not_hardcoded(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    # Distinctive configured path — must not match any real Civ VI install layout.
    configured = tmp_path / "UNIQUE_CIV6_DEPLOY_ROOT_XYZ" / "Mods"
    _configure_civ6(db, configured)
    folder = "SteamWorkshopMod"
    source = _seed_mod(
        library,
        folder=folder,
        mod_id="289070105",
        files={"modinfo": "<Mod/>", "Assets/tex.png": "png"},
    )
    _register(db, mod_id="289070105", title=folder, folder=source)

    # Confirm Steam Workshop identity defaults (platform steam).
    display = db.get_mod_display_info("289070105")
    assert display is not None
    assert display.platform == PLATFORM_STEAM

    result = ModDeployer(library_root=library, db=db).deploy_mod("289070105")
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    assert target == (configured / folder).resolve()
    assert str(configured.resolve()) in str(target)
    # Must not invent Steam/Documents Civ VI paths.
    assert "Sid Meier" not in str(target)
    assert "Documents" not in str(target)
    assert (configured / folder / "modinfo").is_file()
    assert (source / "modinfo").is_file()  # library untouched


# ---------------------------------------------------------------------------
# Chinese deploy-folder name → workspace_id (library source unchanged)
# ---------------------------------------------------------------------------


def test_chinese_directory_maps_to_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.deploy_rules.generic import contains_chinese

    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCN")
    # Steam upsert: PK coincidentally equals workspace_id for this fixture only.
    workspace_id = "123456789"
    folder = "中文 Mod 名"
    assert contains_chinese(folder)

    source = _seed_mod(
        library,
        folder=folder,
        mod_id=workspace_id,
        files={"file1.modinfo": "<Mod/>", "Assets/a.xml": "<ok/>"},
    )
    _register(db, mod_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(workspace_id)
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    assert target == (mods / workspace_id).resolve()
    assert (target / "file1.modinfo").is_file()
    assert (target / "Assets" / "a.xml").is_file()
    assert not (mods / folder).exists()
    # Source library directory must not be renamed.
    assert source.is_dir()
    assert source.name == folder
    assert (source / "file1.modinfo").is_file()


def test_chinese_directory_uses_workspace_id_not_mod_id_pk(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """
    Real identity layout: mods.mod_id (PK) ≠ mods.workspace_id.

    Deploy folder must be workspace_id, never the SQLite PK.
    """
    from services.deploy_rules.generic import contains_chinese

    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPkWs")
    pk = "465"
    workspace_id = "3681020076"
    folder = "测试中文Mod"
    assert contains_chinese(folder)
    assert pk != workspace_id

    source = _seed_mod(
        library,
        folder=folder,
        mod_id=pk,
        files={"file1.modinfo": "<Mod/>", "Assets/a.xml": "<ok/>"},
    )
    _register(db, mod_id=pk, title=folder, folder=source)
    # upsert_mod sets workspace_id=PK under Steam scheme — overwrite to diverge.
    db.update_mod_identity_fields(
        pk,
        workspace_id=workspace_id,
        external_id=workspace_id,
    )
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert str(info.mod_id) == pk
    assert str(info.workspace_id) == workspace_id

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    assert target == (mods / workspace_id).resolve()
    assert target.name == workspace_id
    assert target.name != pk
    assert not (mods / pk).exists()
    assert not (mods / folder).exists()
    assert source.name == folder


def test_english_directory_keeps_original_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsEN")
    mid = "123456789"
    folder = "Better UI"
    source = _seed_mod(
        library,
        folder=folder,
        mod_id=mid,
        files={"ui.modinfo": "<Mod/>"},
    )
    _register(db, mod_id=mid, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / folder).resolve()
    assert source.name == folder
    assert not (mods / mid).exists()


def test_mixed_chinese_english_maps_to_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsMixed")
    workspace_id = "123456789"
    folder = "Better UI 中文版"
    source = _seed_mod(
        library,
        folder=folder,
        mod_id=workspace_id,
        files={"x.modinfo": "<Mod/>"},
    )
    _register(db, mod_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(workspace_id)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / workspace_id).resolve()
    assert source.name == folder
    assert source.is_dir()


def test_chinese_fileplan_uses_workspace_id_targets_directly(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPlan")
    pk = "987654321"
    workspace_id = "111000222"
    folder = "和而不同"
    source = _seed_mod(
        library,
        folder=folder,
        mod_id=pk,
        files={"a.txt": "1", "Assets/b.txt": "2"},
    )
    cfg = db.get_game_deploy_config(CIV6)
    assert cfg is not None
    # Plan-only: ctx.internal_id is the PK; workspace_id must drive the folder.
    ctx = DeployContext(
        internal_id=pk,
        workspace_id=workspace_id,
        app_id=CIV6,
        source=source,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        managed_path=source,
    )
    planned = FolderCopyStrategy().plan(ctx)
    assert planned.success is True
    assert Path(planned.target).resolve() == (mods / workspace_id).resolve()
    assert pk not in Path(planned.target).parts
    assert folder not in planned.target
    for entry in planned.files:
        assert workspace_id in entry.target.replace("\\", "/")
        assert pk not in Path(entry.target).parts
        assert folder not in Path(entry.target).parts
    # No second-phase rename — library still Chinese-named.
    assert source.name == folder


def test_chinese_existing_target_redeploy_success(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCNExist")
    workspace_id = "2465378070"
    folder = "Civ6 Plus：和而不同"
    files = {f"f{i}.txt": f"v{i}" for i in range(4)}
    source = _seed_mod(library, folder=folder, mod_id=workspace_id, files=files)
    _register(db, mod_id=workspace_id, title=folder, folder=source)

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(workspace_id)["success"] is True
    second = deployer.deploy_mod(workspace_id)
    assert second["success"] is True, second
    assert "没有可部署的文件" not in str(second.get("error") or "")
    assert int(second.get("verified_files") or 0) == len(files)
    target = mods / workspace_id
    for rel in files:
        assert (target / rel).is_file()
    assert source.name == folder


def test_chinese_manifest_equals_fileplan(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCNMan")
    workspace_id = "111222333"
    folder = "中文测试Mod"
    files = {"a.modinfo": "<a/>", "Assets/x.xml": "<x/>"}
    source = _seed_mod(library, folder=folder, mod_id=workspace_id, files=files)
    _register(db, mod_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(workspace_id)
    assert result["success"] is True, result
    man = load_manifest(source)
    assert man is not None
    assert len(man.files) == len(files)
    assert int(result.get("planned_files") or 0) == len(man.files)
    result_targets = {Path(f["target"]).resolve() for f in result.get("files") or []}
    manifest_targets = {Path(f.target).resolve() for f in man.files}
    assert result_targets == manifest_targets
    for t in manifest_targets:
        assert t.is_relative_to((mods / workspace_id).resolve())
        assert folder not in t.parts
    assert source.name == folder


def test_non_civ6_chinese_folder_keeps_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Chinese folder rename is Civ VI-only — other folder_copy games unchanged."""
    library = tmp_path / "library"
    mods = tmp_path / "OtherMods"
    mods.mkdir()
    app_id = 424242
    db.update_game_deploy_config(
        app_id,
        name="SomeGame",
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    folder = "中文目录"
    mid = "555666777"
    mod_dir = library / "SomeGame" / folder
    mod_dir.mkdir(parents=True)
    info = mod_dir / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        (
            "{\n"
            f'  "internal_id": "{mid}",\n'
            f'  "published_file_id": "{mid}",\n'
            f'  "title": "{folder}",\n'
            f'  "app_id": {app_id},\n'
            '  "game_name": "SomeGame"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    (mod_dir / "a.txt").write_text("a", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title=folder, app_id=app_id)
    )
    _prove_managed_folder(db, mid, mod_dir)

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / folder).resolve()
    assert mod_dir.name == folder
