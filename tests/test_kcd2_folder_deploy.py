"""Kingdom Come: Deliverance II keeps the Mod folder. Pak flatten is opt-in."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_KCD2,
    DEPLOY_TYPE_PAK_MOD_PATH,
    FolderCopyStrategy,
    PakModPathStrategy,
    resolve_deploy_type,
    resolve_strategy,
)
from services.deploy_rules.base import DeployContext
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

KCD2 = 1771300
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "kcd2_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure(db: DatabaseManager, app_id: int, name: str, mod_path: Path) -> None:
    mod_path.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        app_id,
        name=name,
        install_path=str(mod_path.parent / "install"),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )


def _seed(library: Path, game: str, folder: str, files: dict[str, bytes]) -> Path:
    mod_dir = library / game / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / INFO_DIR_NAME).mkdir()
    for rel, data in files.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return mod_dir


def _register(
    db: DatabaseManager,
    *,
    app_id: int,
    game_name: str,
    external_id: str,
    title: str,
    folder: Path,
) -> str:
    created = create_steam_test_mod(
        db,
        external_id=external_id,
        title=title,
        app_id=app_id,
        game_name=game_name,
    )
    prove_managed_folder(
        db,
        folder,
        handle=created.mod_id,
        title=title,
        app_id=app_id,
        game_name=game_name,
    )
    return str(created.internal_id)


def test_kcd2_pak_and_manifest_stay_inside_mod_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "ModA",
        {"ModA.pak": b"PAK", "mod.manifest": b"MANIFEST"},
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="1771300001",
        title="ModA",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_KCD2
    assert (mods / "ModA" / "ModA.pak").read_bytes() == b"PAK"
    assert (mods / "ModA" / "mod.manifest").read_bytes() == b"MANIFEST"
    assert not (mods / "ModA.pak").exists()


def test_kcd2_multiple_paks_keep_mod_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "ModA",
        {"mod.manifest": b"MANIFEST", "A.pak": b"A", "B.pak": b"B"},
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="1771300002",
        title="ModA",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert (mods / "ModA" / "mod.manifest").read_bytes() == b"MANIFEST"
    assert (mods / "ModA" / "A.pak").read_bytes() == b"A"
    assert (mods / "ModA" / "B.pak").read_bytes() == b"B"
    assert not (mods / "A.pak").exists()
    assert not (mods / "B.pak").exists()


def test_bg3_flat_pak_layout_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    mods = tmp_path / "BG3Mods"
    _configure(db, BG3, "Baldur's Gate 3", mods)
    source = tmp_path / "src" / "CoolPakMod"
    source.mkdir(parents=True)
    (source / "MyCoolMod.pak").write_bytes(b"PAKDATA")
    cfg = db.get_game_deploy_config(BG3)
    assert cfg is not None
    ctx = DeployContext(
        internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4",
        workspace_id="1086940001",
        app_id=BG3,
        source=source,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        managed_path=source,
    )
    strategy = resolve_strategy(ctx)
    assert isinstance(strategy, PakModPathStrategy)
    planned = strategy.plan(ctx)
    assert planned.success is True
    assert planned.deploy_type == DEPLOY_TYPE_PAK_MOD_PATH
    targets = {Path(entry.target).resolve() for entry in planned.files}
    assert (mods / "MyCoolMod.pak").resolve() in targets
    assert (mods / "CoolPakMod" / "MyCoolMod.pak").resolve() not in targets


def test_kcd2_manifest_directory_is_the_game_mod_root(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "ModA",
        {
            "mod.manifest": b"MANIFEST",
            "Data/EBAPMod.pak": b"PAK",
        },
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="ModA",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert (mods / "ModA" / "mod.manifest").read_bytes() == b"MANIFEST"
    assert (mods / "ModA" / "Data" / "EBAPMod.pak").read_bytes() == b"PAK"
    assert not (mods / "mod_245").exists()
    assert not (mods / "EBAPMod.pak").exists()
    assert not (mods / "ModA.pak").exists()


def test_kcd2_archive_top_level_directory_is_not_wrapped(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import zipfile

    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = library / "KingdomComeDeliverance2" / "中文包装"
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    zip_path = source / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ModA/mod.manifest", b"MANIFEST")
        zf.writestr("ModA/Data/EBAPMod.pak", b"PAK")
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="中文包装",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert (mods / "ModA" / "mod.manifest").read_bytes() == b"MANIFEST"
    assert (mods / "ModA" / "Data" / "EBAPMod.pak").read_bytes() == b"PAK"
    assert not (mods / "mod_245").exists()
    assert not (mods / "中文包装").exists()
    assert not (mods / "EBAPMod.pak").exists()


def test_kcd2_flat_archive_uses_library_folder_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import zipfile

    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = library / "KingdomComeDeliverance2" / "FlatMod"
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    with zipfile.ZipFile(source / "pack.zip", "w") as zf:
        zf.writestr("mod.manifest", b"MANIFEST")
        zf.writestr("Data/x.pak", b"PAK")
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="FlatMod",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert (mods / "FlatMod" / "mod.manifest").read_bytes() == b"MANIFEST"
    assert (mods / "FlatMod" / "Data" / "x.pak").read_bytes() == b"PAK"
    assert not (mods / "mod_245").exists()
    assert not (mods / "mod_245" / "FlatMod").exists()


def test_kcd2_sibling_manifests_fail_closed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "Pack",
        {
            "ModA/mod.manifest": b"A",
            "ModB/mod.manifest": b"B",
        },
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="Pack",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is False
    assert not (mods / "ModA").exists()
    assert not (mods / "ModB").exists()
    assert not (mods / "Pack").exists()
    assert not (mods / "mod_245").exists()


def test_kcd2_nested_manifest_keeps_outer_root(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "ModA",
        {
            "mod.manifest": b"ROOT",
            "optional/mod.manifest": b"NESTED",
            "Data/A.pak": b"PAK",
        },
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="ModA",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert (mods / "ModA" / "mod.manifest").read_bytes() == b"ROOT"
    assert (mods / "ModA" / "optional" / "mod.manifest").read_bytes() == b"NESTED"
    assert (mods / "ModA" / "Data" / "A.pak").read_bytes() == b"PAK"
    assert not (mods / "optional").exists()
    assert not (mods / "mod_245").exists()


def test_kcd2_without_manifest_fails_closed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "Mods"
    _configure(db, KCD2, "KingdomComeDeliverance2", mods)
    source = _seed(
        library,
        "KingdomComeDeliverance2",
        "ModA",
        {"Data/EBAPMod.pak": b"PAK"},
    )
    frozen = _register(
        db,
        app_id=KCD2,
        game_name="KingdomComeDeliverance2",
        external_id="245",
        title="ModA",
        folder=source,
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is False
    assert not (mods / "ModA").exists()
    assert not (mods / "mod_245").exists()
    assert not (mods / "EBAPMod.pak").exists()


def test_kcd2_capability_does_not_replace_other_folder_copy_games() -> None:
    assert resolve_deploy_type(KCD2, "folder_copy") == DEPLOY_TYPE_KCD2
    assert resolve_deploy_type(100, "folder_copy") == DEPLOY_TYPE_FOLDER_COPY
    assert resolve_deploy_type(BG3, "folder_copy") == DEPLOY_TYPE_FOLDER_COPY


def test_generic_folder_copy_with_pak_is_not_flattened(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mods = tmp_path / "GameMods"
    _configure(db, 100, "SomeGame", mods)
    source = tmp_path / "src" / "ModA"
    source.mkdir(parents=True)
    (source / "ModA.pak").write_bytes(b"PAK")
    cfg = db.get_game_deploy_config(100)
    assert cfg is not None
    ctx = DeployContext(
        internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4",
        workspace_id="100001",
        app_id=100,
        source=source,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        managed_path=source,
    )
    strategy = resolve_strategy(ctx)
    assert isinstance(strategy, FolderCopyStrategy)
    planned = strategy.plan(ctx)
    assert planned.success is True
    assert any(
        Path(entry.target).resolve() == (mods / "ModA" / "ModA.pak").resolve()
        for entry in planned.files
    )
