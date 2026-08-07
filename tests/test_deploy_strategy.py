"""Deploy strategy suite: generic / palworld / manifest / undeploy isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import MANIFEST_FILENAME, load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_strategy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_meta(mod_dir: Path, *, mid: str, title: str, app_id: int, game: str) -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {app_id},\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )


def _generic_mod(library: Path, *, mid: str = "91001") -> Path:
    mod = library / "SomeGame" / "CoolMod"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("A", encoding="utf-8")
    (mod / "sub").mkdir()
    (mod / "sub" / "b.txt").write_text("B", encoding="utf-8")
    _write_meta(mod, mid=mid, title="CoolMod", app_id=100, game="SomeGame")
    (mod / INFO_DIR_NAME / "secret.txt").write_text("nope", encoding="utf-8")
    return mod


def test_generic_deploy_and_manifest(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    source = _generic_mod(library)

    db.update_game_deploy_config(
        100,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(ModMetadata(published_file_id="91001", title="CoolMod", app_id=100))

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod("91001")

    assert result["success"] is True
    assert result["copied_files"] == 2
    assert result["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY
    target = Path(result["target"])
    assert (target / "a.txt").read_text(encoding="utf-8") == "A"
    assert (target / "sub" / "b.txt").read_text(encoding="utf-8") == "B"
    assert not (target / INFO_DIR_NAME).exists()

    manifest = load_manifest(source)
    assert manifest is not None
    assert manifest.mod_id == "91001"
    assert manifest.deploy_type == DEPLOY_TYPE_FOLDER_COPY
    assert len(manifest.files) == 2
    targets = {Path(f.target) for f in manifest.files}
    assert target / "a.txt" in targets
    assert target / "sub" / "b.txt" in targets

    info = db.get_mod_deploy_info("91001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert (source / INFO_DIR_NAME / MANIFEST_FILENAME).is_file()


def test_generic_undeploy(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    source = _generic_mod(library, mid="91002")

    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="91002", title="CoolMod", app_id=100))

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91002")["success"] is True
    target = mods_root / "CoolMod"
    assert (target / "a.txt").is_file()

    und = deployer.undeploy_mod("91002")
    assert und["success"] is True
    assert not (target / "a.txt").exists()
    assert not (target / "sub" / "b.txt").exists()
    # Empty dirs pruned under mod_path; CoolMod folder should be gone
    assert not target.exists()
    assert not (source / INFO_DIR_NAME / MANIFEST_FILENAME).exists()

    info = db.get_mod_deploy_info("91002")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    assert info.deploy_time == ""
    assert info.deploy_path == ""


def test_palworld_logicmods_to_paks(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "PalworldInstall"
    install.mkdir()
    mod = library / "Palworld" / "LogicPack"
    logic = mod / "LogicMods"
    logic.mkdir(parents=True)
    (logic / "MyLogic.pak").write_bytes(b"logic-pak")
    _write_meta(mod, mid="92001", title="LogicPack", app_id=1623730, game="Palworld")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        mod_path=str(tmp_path / "unused"),
        deploy_type=DEPLOY_TYPE_PALWORLD_PAK,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="92001", title="LogicPack", app_id=1623730)
    )

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod("92001")
    assert result["success"] is True
    assert result["deploy_type"] == DEPLOY_TYPE_PALWORLD_PAK

    paks = install / "Pal" / "Content" / "Paks"
    assert (paks / "LogicMods" / "MyLogic.pak").read_bytes() == b"logic-pak"
    # Must not land in ~mods or flat under Paks/
    assert not (paks / "~mods" / "MyLogic.pak").exists()
    assert not (paks / "MyLogic.pak").exists()

    man = load_manifest(mod)
    assert man is not None
    assert len(man.files) == 1


def test_palworld_loose_pak_to_tilde_mods(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "PalworldInstall"
    install.mkdir()
    mod = library / "Palworld" / "LoosePak"
    mod.mkdir(parents=True)
    (mod / "Cool.pak").write_bytes(b"cool")
    (mod / "readme.txt").write_text("ignore", encoding="utf-8")
    _write_meta(mod, mid="92002", title="LoosePak", app_id=1623730, game="Palworld")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        deploy_type=DEPLOY_TYPE_PALWORLD_PAK,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="92002", title="LoosePak", app_id=1623730)
    )

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod("92002")
    assert result["success"] is True

    tilde = install / "Pal" / "Content" / "Paks" / "~mods"
    assert tilde.is_dir()
    assert (tilde / "Cool.pak").read_bytes() == b"cool"
    # Non-pak not deployed
    assert not (tilde / "readme.txt").exists()


def test_undeploy_only_own_files_leaves_others(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()

    a = _generic_mod(library, mid="93001")
    # Second mod
    b = library / "SomeGame" / "OtherMod"
    b.mkdir(parents=True)
    (b / "x.txt").write_text("X", encoding="utf-8")
    _write_meta(b, mid="93002", title="OtherMod", app_id=100, game="SomeGame")

    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="93001", title="CoolMod", app_id=100))
    db.upsert_mod(ModMetadata(published_file_id="93002", title="OtherMod", app_id=100))

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("93001")["success"]
    assert deployer.deploy_mod("93002")["success"]

    # Foreign file sitting next to deployed trees (must survive)
    shared = mods_root / "shared_keep.txt"
    shared.write_text("keep-me", encoding="utf-8")
    other_target = mods_root / "OtherMod" / "x.txt"
    assert other_target.is_file()

    und = deployer.undeploy_mod("93001")
    assert und["success"] is True
    assert not (mods_root / "CoolMod").exists()
    assert other_target.is_file()
    assert shared.read_text(encoding="utf-8") == "keep-me"
    assert load_manifest(a) is None
    assert load_manifest(b) is not None


def test_palworld_undeploy_isolation(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "PalworldInstall"
    install.mkdir()
    paks = install / "Pal" / "Content" / "Paks"
    tilde = paks / "~mods"
    tilde.mkdir(parents=True)
    foreign = tilde / "SomeoneElse.pak"
    foreign.write_bytes(b"foreign")

    mod = library / "Palworld" / "Mine"
    mod.mkdir(parents=True)
    (mod / "Mine.pak").write_bytes(b"mine")
    _write_meta(mod, mid="94001", title="Mine", app_id=1623730, game="Palworld")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        deploy_type=DEPLOY_TYPE_PALWORLD_PAK,
    )
    db.upsert_mod(ModMetadata(published_file_id="94001", title="Mine", app_id=1623730))

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("94001")["success"]
    assert (tilde / "Mine.pak").is_file()

    assert deployer.undeploy_mod("94001")["success"]
    assert not (tilde / "Mine.pak").exists()
    assert foreign.read_bytes() == b"foreign"
    # Must not wipe ~mods or Paks
    assert tilde.is_dir()
    assert paks.is_dir()


def test_manifest_json_shape(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    source = _generic_mod(library, mid="95001")
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="95001", title="CoolMod", app_id=100))

    ModDeployer(library_root=library, db=db).deploy_mod("95001")
    raw = json.loads(
        (source / INFO_DIR_NAME / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert set(raw) >= {"mod_id", "deploy_time", "deploy_type", "files"}
    assert raw["mod_id"] == "95001"
    assert isinstance(raw["files"], list)
    assert raw["files"]
    assert "source" in raw["files"][0]
    assert "target" in raw["files"][0]
