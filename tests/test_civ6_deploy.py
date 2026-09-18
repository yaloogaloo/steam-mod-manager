"""Civilization VI (AppID 289070) — Steam Workshop folder_copy deploy."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import CIVILIZATION_VI_APP_IDS, PLATFORM_STEAM
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
from services.deploy_rules.generic import deploy_folder_name
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

CIV6 = CIVILIZATION_VI_APP_ID
assert CIV6 == 289070
assert CIV6 in CIVILIZATION_VI_APP_IDS


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "civ6_deploy.db")
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
    files: dict[str, str],
) -> Path:
    mod_dir = library / "文明Ⅵ" / folder
    mod_dir.mkdir(parents=True)
    info = mod_dir / INFO_DIR_NAME
    info.mkdir()
    (info / "manager_only.txt").write_text("skip", encoding="utf-8")
    for rel, data in files.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return mod_dir


def _register(
    db: DatabaseManager,
    *,
    external_id: str,
    title: str,
    folder: Path,
) -> tuple[str, str]:
    created = create_steam_test_mod(
        db,
        external_id=str(external_id),
        title=title,
        app_id=CIV6,
        game_name="文明Ⅵ",
    )
    pk = prove_managed_folder(
        db,
        folder,
        handle=created.mod_id,
        title=title,
        app_id=CIV6,
        game_name="文明Ⅵ",
    )
    return pk, str(created.internal_id)


# ---------------------------------------------------------------------------
# Strategy registration
# ---------------------------------------------------------------------------


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
    ctx = DeployContext(
        internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4",
        workspace_id="289070001",
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
        files={
            "file1.txt": "one",
            "file2.txt": "two",
            "Assets/x.txt": "x",
            "Assets/y.txt": "y",
        },
    )
    pk, frozen = _register(db, external_id="289070101", title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY

    target = mods / "ModA"
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
    source = _seed_mod(library, folder=folder, files=files)
    pk, frozen = _register(db, external_id="289070102", title=folder, folder=source)

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(frozen)
    assert first["success"] is True, first

    # All targets already present — redeploy must still SUCCESS.
    second = deployer.deploy_mod(frozen)
    assert second["success"] is True, second
    assert second["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY
    assert int(second.get("verified_files") or 0) == len(files)

    target = mods / "ModExisting"
    for rel in files:
        assert (target / rel).is_file()
    assert load_manifest(source) is not None


def test_partial_existing_target_success(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPartial")
    folder = "ModPartial"
    files = {f"f{i}.txt": f"content-{i}" for i in range(10)}
    source = _seed_mod(library, folder=folder, files=files)
    pk, frozen = _register(db, external_id="289070103", title=folder, folder=source)

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(frozen)
    assert first["success"] is True, first

    target = mods / "ModPartial"
    # Leave a partial set of owned targets (prior deploy), then redeploy.
    for i in range(6):
        (target / f"f{i}.txt").unlink()

    result = deployer.deploy_mod(frozen)
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
    source = _seed_mod(library, folder=folder, files=files)
    pk, frozen = _register(db, external_id="289070104", title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
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
        files={"modinfo": "<Mod/>", "Assets/tex.png": "png"},
    )
    pk, frozen = _register(db, external_id="289070105", title=folder, folder=source)

    # Confirm Steam Workshop identity defaults (platform steam).
    display = db.get_mod_display_info(pk)
    assert display is not None
    assert display.platform == PLATFORM_STEAM

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    expected = "SteamWorkshopMod"
    assert target == (configured / expected).resolve()
    assert str(configured.resolve()) in str(target)
    # Must not invent Steam/Documents Civ VI paths.
    assert "Sid Meier" not in str(target)
    assert "Documents" not in str(target)
    assert (configured / expected / "modinfo").is_file()
    assert (source / "modinfo").is_file()  # library untouched


# ---------------------------------------------------------------------------
# Deploy folder is always mod_{workspace_id} (library folder unchanged)
# ---------------------------------------------------------------------------


def test_chinese_directory_maps_to_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.mod_path_normalizer import contains_chinese

    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCN")
    workspace_id = "123456789"
    folder = "三国全面战争"
    expected = deploy_folder_name(workspace_id)
    assert contains_chinese(folder)
    assert expected == "mod_123456789"

    source = _seed_mod(
        library,
        folder=folder,
        files={"file1.modinfo": "<Mod/>", "Assets/a.xml": "<ok/>"},
    )
    pk, frozen = _register(db, external_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert target.name == expected
    assert target.name != workspace_id
    assert target.name != pk
    assert (target / "file1.modinfo").is_file()
    assert (target / "Assets" / "a.xml").is_file()
    assert not (mods / folder).exists()
    assert not (mods / workspace_id).exists()
    assert source.is_dir()
    assert source.name == folder
    assert (source / "file1.modinfo").is_file()


def test_chinese_directory_uses_workspace_folder_not_pk(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Deploy folder is mod_{workspace_id}, never PK, Frozen UUID, or Chinese."""
    from services.mod_path_normalizer import contains_chinese

    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPkWs")
    workspace_id = "3681020076"
    folder = "测试中文Mod"
    expected = deploy_folder_name(workspace_id)
    assert contains_chinese(folder)
    assert expected == "mod_3681020076"

    source = _seed_mod(
        library,
        folder=folder,
        files={"file1.modinfo": "<Mod/>", "Assets/a.xml": "<ok/>"},
    )
    pk, frozen = _register(db, external_id=workspace_id, title=folder, folder=source)
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert str(info.mod_id) == pk
    assert str(info.workspace_id) == workspace_id
    assert pk != workspace_id

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result

    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert target.name == expected
    assert target.name != pk
    assert target.name != workspace_id
    assert target.name != frozen
    assert not (mods / pk).exists()
    assert not (mods / workspace_id).exists()
    assert not (mods / folder).exists()
    assert source.name == folder


def test_english_directory_keeps_original_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsEN")
    mid = "123456789"
    folder = "Better UI"
    expected = folder
    source = _seed_mod(
        library,
        folder=folder,
        files={"ui.modinfo": "<Mod/>"},
    )
    pk, frozen = _register(db, external_id=mid, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / expected).resolve()
    assert source.name == folder
    assert (mods / folder).exists()
    assert not (mods / mid).exists()
    assert not (mods / deploy_folder_name(mid)).exists()


def test_mixed_chinese_english_maps_to_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsMixed")
    workspace_id = "123456789"
    folder = "Better UI 中文版"
    expected = deploy_folder_name(workspace_id)
    source = _seed_mod(
        library,
        folder=folder,
        files={"x.modinfo": "<Mod/>"},
    )
    pk, frozen = _register(db, external_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / expected).resolve()
    assert Path(result["target"]).name != workspace_id
    assert source.name == folder
    assert source.is_dir()


def test_chinese_fileplan_uses_workspace_folder_directly(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsPlan")
    pk = "987654321"
    workspace_id = "111000222"
    folder = "和而不同"
    expected = deploy_folder_name(workspace_id)
    source = _seed_mod(
        library,
        folder=folder,
        files={"a.txt": "1", "Assets/b.txt": "2"},
    )
    cfg = db.get_game_deploy_config(CIV6)
    assert cfg is not None
    ctx = DeployContext(
        internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4",
        workspace_id=workspace_id,
        app_id=CIV6,
        source=source,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        managed_path=source,
    )
    planned = FolderCopyStrategy().plan(ctx)
    assert planned.success is True
    assert Path(planned.target).resolve() == (mods / expected).resolve()
    assert pk not in Path(planned.target).parts
    assert workspace_id not in Path(planned.target).parts
    assert folder not in planned.target
    for entry in planned.files:
        assert expected in entry.target.replace("\\", "/")
        assert pk not in Path(entry.target).parts
        assert workspace_id not in Path(entry.target).parts
        assert folder not in Path(entry.target).parts
    assert source.name == folder


def test_chinese_existing_target_redeploy_success(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCNExist")
    workspace_id = "2465378070"
    folder = "Civ6 Plus：和而不同"
    expected = deploy_folder_name(workspace_id)
    files = {f"f{i}.txt": f"v{i}" for i in range(4)}
    source = _seed_mod(library, folder=folder, files=files)
    pk, frozen = _register(db, external_id=workspace_id, title=folder, folder=source)

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(frozen)["success"] is True
    second = deployer.deploy_mod(frozen)
    assert second["success"] is True, second
    assert "没有可部署的文件" not in str(second.get("error") or "")
    assert int(second.get("verified_files") or 0) == len(files)
    target = mods / expected
    for rel in files:
        assert (target / rel).is_file()
    assert not (mods / workspace_id).exists()
    assert source.name == folder


def test_chinese_manifest_equals_fileplan(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_civ6(db, tmp_path / "CivModsCNMan")
    workspace_id = "111222333"
    folder = "中文测试Mod"
    expected = deploy_folder_name(workspace_id)
    files = {"a.modinfo": "<a/>", "Assets/x.xml": "<x/>"}
    source = _seed_mod(library, folder=folder, files=files)
    pk, frozen = _register(db, external_id=workspace_id, title=folder, folder=source)

    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    man = load_manifest(source)
    assert man is not None
    assert len(man.files) == len(files)
    assert int(result.get("planned_files") or 0) == len(man.files)
    result_targets = {Path(f["target"]).resolve() for f in result.get("files") or []}
    manifest_targets = {Path(f.target).resolve() for f in man.files}
    assert result_targets == manifest_targets
    for t in manifest_targets:
        assert t.is_relative_to((mods / expected).resolve())
        assert folder not in t.parts
        assert workspace_id not in t.parts
    assert source.name == folder


def test_non_civ6_chinese_folder_uses_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """FolderCopy always uses mod_{workspace_id}, including unconfigured games."""
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
    folder = "测试MOD"
    mid = "555666777"
    expected = deploy_folder_name(mid)
    mod_dir = library / "SomeGame" / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / INFO_DIR_NAME).mkdir()
    (mod_dir / "a.txt").write_text("a", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mid, title=folder, app_id=app_id, game_name="SomeGame"
    )
    prove_managed_folder(
        db, mod_dir, handle=created.mod_id, title=folder, app_id=app_id, game_name="SomeGame"
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == (mods / expected).resolve()
    assert Path(result["target"]).name == expected
    assert not (mods / "ceshi_MOD").exists()
    assert not (mods / folder).exists()
    assert mod_dir.name == folder
