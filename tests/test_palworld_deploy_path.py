"""Palworld deploy paths must be under install_path, not mod_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "palworld_path.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_meta(mod: Path, mid: str) -> None:
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{mod.name}",\n'
        '  "app_id": 1623730,\n'
        '  "game_name": "Palworld"\n'
        "}\n",
        encoding="utf-8",
    )


def test_root_pak_goes_to_tilde_mods_under_install(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    decoy_mod_path = tmp_path / "WRONG_MOD_PATH"
    decoy_mod_path.mkdir()

    mod = library / "Palworld" / "Loose"
    mod.mkdir(parents=True)
    (mod / "test.pak").write_bytes(b"pak-bytes")
    # Nested pak must NOT be deployed (only root-level)
    nested = mod / "sub"
    nested.mkdir()
    (nested / "nested.pak").write_bytes(b"nope")
    _write_meta(mod, "99101")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        mod_path=str(decoy_mod_path),
        deploy_type="palworld_pak",
    )
    db.upsert_mod(
        ModMetadata(published_file_id="99101", title="Loose", app_id=1623730)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("99101")
    assert result["success"] is True

    target = install / "Pal" / "Content" / "Paks" / "~mods" / "test.pak"
    assert target.is_file()
    assert target.read_bytes() == b"pak-bytes"
    assert not (decoy_mod_path / "test.pak").exists()
    assert not list(decoy_mod_path.rglob("*.pak"))
    assert not (install / "Pal" / "Content" / "Paks" / "~mods" / "nested.pak").exists()

    man = load_manifest(mod)
    assert man is not None
    assert any(Path(f.target) == target.resolve() for f in man.files)


def test_logicmods_go_under_paks_logicmods(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    decoy = tmp_path / "WRONG_MOD_PATH"
    decoy.mkdir()

    mod = library / "Palworld" / "LogicPack"
    logic = mod / "LogicMods"
    logic.mkdir(parents=True)
    (logic / "a.pak").write_bytes(b"logic")
    _write_meta(mod, "99102")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        mod_path=str(decoy),
        deploy_type="palworld_pak",
    )
    db.upsert_mod(
        ModMetadata(published_file_id="99102", title="LogicPack", app_id=1623730)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("99102")
    assert result["success"] is True

    target = install / "Pal" / "Content" / "Paks" / "LogicMods" / "a.pak"
    assert target.is_file()
    assert target.read_bytes() == b"logic"
    # Not flat under Paks/, not under ~mods, not under mod_path
    assert not (install / "Pal" / "Content" / "Paks" / "a.pak").exists()
    assert not (install / "Pal" / "Content" / "Paks" / "~mods" / "a.pak").exists()
    assert not list(decoy.rglob("*.pak"))


def test_never_copies_into_mod_path(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    mod_path = tmp_path / "ConfiguredModPath"
    mod_path.mkdir()

    mod = library / "Palworld" / "Both"
    mod.mkdir(parents=True)
    (mod / "root.pak").write_bytes(b"root")
    logic = mod / "LogicMods"
    logic.mkdir()
    (logic / "L.pak").write_bytes(b"L")
    _write_meta(mod, "99103")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mod_path),
        deploy_type="palworld_pak",
    )
    db.upsert_mod(ModMetadata(published_file_id="99103", title="Both", app_id=1623730))

    assert ModDeployer(library_root=library, db=db).deploy_mod("99103")["success"]
    assert list(mod_path.rglob("*")) == []  # untouched
    assert (install / "Pal" / "Content" / "Paks" / "~mods" / "root.pak").is_file()
    assert (install / "Pal" / "Content" / "Paks" / "LogicMods" / "L.pak").is_file()


def test_undeploy_removes_correct_install_targets(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()

    mod = library / "Palworld" / "CleanMe"
    mod.mkdir(parents=True)
    (mod / "test.pak").write_bytes(b"x")
    logic = mod / "LogicMods"
    logic.mkdir()
    (logic / "a.pak").write_bytes(b"y")
    _write_meta(mod, "99104")

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        deploy_type="palworld_pak",
    )
    db.upsert_mod(
        ModMetadata(published_file_id="99104", title="CleanMe", app_id=1623730)
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("99104")["success"]
    root_pak = install / "Pal" / "Content" / "Paks" / "~mods" / "test.pak"
    logic_pak = install / "Pal" / "Content" / "Paks" / "LogicMods" / "a.pak"
    assert root_pak.is_file() and logic_pak.is_file()

    # Foreign file in ~mods must survive
    foreign = install / "Pal" / "Content" / "Paks" / "~mods" / "Other.pak"
    foreign.write_bytes(b"keep")

    und = dep.undeploy_mod("99104")
    assert und["success"] is True
    assert not root_pak.exists()
    assert not logic_pak.exists()
    assert foreign.read_bytes() == b"keep"
    assert load_manifest(mod) is None
