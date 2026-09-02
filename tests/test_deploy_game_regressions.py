"""Palworld / Duckov / Stardew deploy regressions (tmp fixtures, no real Mods)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_DUCKOV,
    DEPLOY_TYPE_PALWORLD_PAK,
    DEPLOY_TYPE_STARDEW_VALLEY,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "reg.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _meta(mod_dir: Path, *, mid: str, app_id: int, game: str) -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "T{mid}",\n'
        f'  "app_id": {app_id},\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )


def test_palworld_18mb_fixture_pipeline(tmp_path: Path, db: DatabaseManager) -> None:
    """~18MB fixture: plan → copy → validate without unbounded scans."""
    library = tmp_path / "mod"
    mod = library / "Palworld" / "BigPakMod"
    (mod / "LogicMods").mkdir(parents=True)
    payload = b"x" * (18 * 1024 * 1024)
    (mod / "LogicMods" / "big.pak").write_bytes(payload)
    _meta(mod, mid="3780000001", app_id=1623730, game="Palworld")

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
    db.upsert_mod(
        ModMetadata(published_file_id="3780000001", title="Big", app_id=1623730)
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
    _meta(mod, mid="3167000001", app_id=3167020, game="Duckov")

    mods = tmp_path / "Mods"
    mods.mkdir()
    db.update_game_deploy_config(
        3167020,
        name="Duckov",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_DUCKOV,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="3167000001", title="Good", app_id=3167020)
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
    _meta(mod, mid="4131500001", app_id=413150, game="StardewValley")

    mods = tmp_path / "Mods"
    mods.mkdir()
    db.update_game_deploy_config(
        413150,
        name="StardewValley",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_STARDEW_VALLEY,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="4131500001", title="Nested", app_id=413150)
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod("4131500001")
    assert out.get("success") is True
    assert (mods / "CoolMod" / "manifest.json").is_file()
