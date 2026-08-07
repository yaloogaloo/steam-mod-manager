"""Palworld enhanced strategy: pak rules + folder_copy fallback (Cases A–E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    load_manifest,
    resolve_deploy_type,
)
from services.deploy_rules.palworld import PalworldStrategy
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

APP_ID = 1623730


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "palworld_enhanced.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _meta(mod: Path, mid: str) -> None:
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{mod.name}",\n'
        f'  "app_id": {APP_ID},\n'
        '  "game_name": "Palworld"\n'
        "}\n",
        encoding="utf-8",
    )


def test_resolve_deploy_type_uses_palworld_strategy() -> None:
    assert resolve_deploy_type(APP_ID, DEPLOY_TYPE_FOLDER_COPY) == DEPLOY_TYPE_PALWORLD_PAK
    assert resolve_deploy_type(999, DEPLOY_TYPE_FOLDER_COPY) == DEPLOY_TYPE_FOLDER_COPY


def test_case_a_folder_mod_fallback(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Case A: Config/dll only → folder_copy (no pak fail)."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    mod_path = tmp_path / "ue4ss_Mods"
    mod_path.mkdir()

    mod = library / "Palworld" / "FolderOnly"
    (mod / "Config").mkdir(parents=True)
    (mod / "Config" / "settings.ini").write_text("a=1", encoding="utf-8")
    (mod / "plugin.dll").write_bytes(b"DLL")
    (mod / "Info.json").write_text("{}", encoding="utf-8")
    (mod / "thumbnail.png").write_bytes(b"\x89PNG")
    _meta(mod, "3704000001")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="3704000001",
            title="FolderOnly",
            app_id=APP_ID,
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("3704000001")
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_PALWORLD_PAK

    dest = mod_path / "FolderOnly"
    assert (dest / "Config" / "settings.ini").read_text(encoding="utf-8") == "a=1"
    assert (dest / "plugin.dll").read_bytes() == b"DLL"
    assert not (install / "Pal" / "Content" / "Paks").exists() or list(
        (install / "Pal" / "Content" / "Paks").rglob("*.pak")
    ) == []

    man = load_manifest(mod)
    assert man is not None
    assert man.files
    assert all(f.type == "folder_copy" for f in man.files)


def test_case_b_ordinary_pak(tmp_path: Path, db: DatabaseManager) -> None:
    """Case B: root test.pak → Paks/~mods/."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    mod_path = tmp_path / "ue4ss_Mods"
    mod_path.mkdir()

    mod = library / "Palworld" / "PakOnly"
    mod.mkdir(parents=True)
    (mod / "test.pak").write_bytes(b"TEST")
    _meta(mod, "3704000002")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="3704000002", title="PakOnly", app_id=APP_ID)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("3704000002")
    assert result["success"] is True, result

    target = install / "Pal" / "Content" / "Paks" / "~mods" / "test.pak"
    assert target.is_file()
    assert target.read_bytes() == b"TEST"
    assert list(mod_path.rglob("*.pak")) == []

    man = load_manifest(mod)
    assert man is not None
    assert len(man.files) == 1
    assert man.files[0].type == "pak"


def test_case_c_logicmods(tmp_path: Path, db: DatabaseManager) -> None:
    """Case C: LogicMods/test.pak → Paks/LogicMods/."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()

    mod = library / "Palworld" / "LogicOnly"
    logic = mod / "LogicMods"
    logic.mkdir(parents=True)
    (logic / "test.pak").write_bytes(b"LOGIC")
    _meta(mod, "3704000003")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="3704000003", title="LogicOnly", app_id=APP_ID)
    )

    assert ModDeployer(library_root=library, db=db).deploy_mod("3704000003")["success"]
    target = install / "Pal" / "Content" / "Paks" / "LogicMods" / "test.pak"
    assert target.read_bytes() == b"LOGIC"
    man = load_manifest(mod)
    assert man is not None
    assert man.files[0].type == "pak"


def test_case_d_mixed_pak_and_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Case D: LogicMods + Paks + Config → all three deployed."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    mod_path = tmp_path / "ue4ss_Mods"
    mod_path.mkdir()

    mod = library / "Palworld" / "Mixed"
    (mod / "LogicMods").mkdir(parents=True)
    (mod / "LogicMods" / "a.pak").write_bytes(b"A")
    (mod / "Paks").mkdir()
    (mod / "Paks" / "b.pak").write_bytes(b"B")
    (mod / "Config").mkdir()
    (mod / "Config" / "c.ini").write_text("ok", encoding="utf-8")
    _meta(mod, "3704000004")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="3704000004", title="Mixed", app_id=APP_ID)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("3704000004")
    assert result["success"] is True, result

    assert (
        install / "Pal" / "Content" / "Paks" / "LogicMods" / "a.pak"
    ).read_bytes() == b"A"
    assert (
        install / "Pal" / "Content" / "Paks" / "~mods" / "b.pak"
    ).read_bytes() == b"B"
    assert (mod_path / "Mixed" / "Config" / "c.ini").read_text(
        encoding="utf-8"
    ) == "ok"
    # Special pak trees must not also land under folder_copy
    assert not (mod_path / "Mixed" / "LogicMods").exists()
    assert not (mod_path / "Mixed" / "Paks").exists()

    man = load_manifest(mod)
    assert man is not None
    types = {f.type for f in man.files}
    assert "pak" in types
    assert "folder_copy" in types
    assert len([f for f in man.files if f.type == "pak"]) == 2
    assert any(
        Path(f.target).name == "c.ini" and f.type == "folder_copy" for f in man.files
    )


def test_case_e_undeploy_only_manifest_targets(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Case E: undeploy deletes only manifest targets."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    mod_path = tmp_path / "ue4ss_Mods"
    mod_path.mkdir()

    mod = library / "Palworld" / "MixedUndeploy"
    (mod / "LogicMods").mkdir(parents=True)
    (mod / "LogicMods" / "a.pak").write_bytes(b"A")
    (mod / "Paks").mkdir()
    (mod / "Paks" / "b.pak").write_bytes(b"B")
    (mod / "Config").mkdir()
    (mod / "Config" / "c.ini").write_text("ok", encoding="utf-8")
    _meta(mod, "3704000005")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="3704000005", title="MixedUndeploy", app_id=APP_ID
        )
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("3704000005")["success"]

    foreign_pak = install / "Pal" / "Content" / "Paks" / "~mods" / "Other.pak"
    foreign_pak.write_bytes(b"keep")
    foreign_folder = mod_path / "OtherMod" / "x.txt"
    foreign_folder.parent.mkdir(parents=True)
    foreign_folder.write_text("keep", encoding="utf-8")

    assert dep.undeploy_mod("3704000005")["success"]

    assert not (
        install / "Pal" / "Content" / "Paks" / "LogicMods" / "a.pak"
    ).exists()
    assert not (install / "Pal" / "Content" / "Paks" / "~mods" / "b.pak").exists()
    assert not (mod_path / "MixedUndeploy").exists()
    assert foreign_pak.is_file()
    assert foreign_folder.is_file()
    assert (install / "Pal" / "Content" / "Paks" / "~mods").is_dir()
    assert load_manifest(mod) is None


def test_auto_pick_up_style_paks_subdir(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Auto PickUp layout under Paks/ still uses pak rules, not ue4ss tree."""
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir()
    ue4ss_mods = install / "Pal" / "Binaries" / "Win64" / "ue4ss" / "Mods"
    ue4ss_mods.mkdir(parents=True)

    mod = library / "Palworld" / "Auto PickUp"
    paks_dir = mod / "Paks"
    paks_dir.mkdir(parents=True)
    (paks_dir / "test.pak").write_bytes(b"TEST")
    (mod / "Info.json").write_text("{}", encoding="utf-8")
    _meta(mod, "3703542467")

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        mod_path=str(ue4ss_mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="3703542467",
            title="Auto PickUp",
            app_id=APP_ID,
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("3703542467")
    assert result["success"] is True
    assert (install / "Pal" / "Content" / "Paks" / "~mods" / "test.pak").is_file()
    assert not (ue4ss_mods / "Auto PickUp" / "Paks").exists()
    # Info.json may folder_copy when mod_path set — pak must not live under ue4ss
    assert list(ue4ss_mods.rglob("*.pak")) == []


def test_strategy_class_name_for_logs() -> None:
    assert PalworldStrategy.__name__ == "PalworldStrategy"
