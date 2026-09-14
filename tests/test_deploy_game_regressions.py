"""Palworld / Duckov / Stardew deploy regressions (tmp fixtures, no real Mods)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from services.deploy_rules import (
    DEPLOY_TYPE_DUCKOV,
    DEPLOY_TYPE_PALWORLD_PAK,
    DEPLOY_TYPE_STARDEW_VALLEY,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "reg.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _register(
    db: DatabaseManager,
    mod_dir: Path,
    *,
    mid: str,
    title: str,
    app_id: int,
    game: str,
) -> None:
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game
    )
    prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title=title,
        app_id=app_id,
        game_name=game,
    )


def test_palworld_18mb_fixture_pipeline(tmp_path: Path, db: DatabaseManager) -> None:
    """~18MB fixture: plan → copy → validate without unbounded scans."""
    library = tmp_path / "mod"
    mod = library / "Palworld" / "BigPakMod"
    (mod / "LogicMods").mkdir(parents=True)
    payload = b"x" * (18 * 1024 * 1024)
    (mod / "LogicMods" / "big.pak").write_bytes(payload)

    install = tmp_path / "game"
    (install / "Pal" / "Content" / "Paks").mkdir(parents=True)
    mods = tmp_path / "Mods"
    mods.mkdir()
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_PALWORLD_PAK,
    )
    _register(
        db, mod, mid="3780000001", title="Big", app_id=1623730, game="Palworld"
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod("3780000001")
    assert out.get("success") is True
    assert out.get("status") == "SUCCESS"
    assert out.get("copied_files", 0) >= 1
    target_pak = (
        install / "Pal" / "Content" / "Paks" / "LogicMods" / "big.pak"
    )
    assert target_pak.is_file()
    assert target_pak.stat().st_size == len(payload)


def test_duckov_info_ini_regression(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mod = library / "Duckov" / "GoodMod"
    mod.mkdir(parents=True)
    (mod / "info.ini").write_text("[Mod]\nName=Good\n", encoding="utf-8")
    (mod / "payload.bin").write_bytes(b"data")

    mods = tmp_path / "Mods"
    mods.mkdir()
    db.update_game_deploy_config(
        3167020,
        name="Duckov",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_DUCKOV,
    )
    _register(
        db, mod, mid="3167000001", title="Good", app_id=3167020, game="Duckov"
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod("3167000001")
    assert out.get("success") is True
    dest = mods / "GoodMod"
    assert (dest / "info.ini").is_file()
    assert not (dest / "GoodMod" / "info.ini").exists()


def test_stardew_nested_manifest(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mod = library / "Stardew" / "Nested"
    nested = mod / "CoolMod"
    nested.mkdir(parents=True)
    (nested / "manifest.json").write_text('{"Name":"Cool"}', encoding="utf-8")
    (nested / "Cool.dll").write_bytes(b"dll")

    mods = tmp_path / "Mods"
    mods.mkdir()
    db.update_game_deploy_config(
        413150,
        name="StardewValley",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_STARDEW_VALLEY,
    )
    _register(
        db, mod, mid="4131500001", title="Nested", app_id=413150, game="StardewValley"
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod("4131500001")
    assert out.get("success") is True
    assert (mods / "CoolMod" / "manifest.json").is_file()
