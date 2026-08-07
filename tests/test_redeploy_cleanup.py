"""Redeploy must remove files that disappeared from the new source set."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "redeploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_redeploy_removes_stale_files_and_rewrites_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()

    mod = library / "G" / "ShrinkMod"
    mod.mkdir(parents=True)
    (mod / "A.pak").write_bytes(b"A")
    (mod / "B.pak").write_bytes(b"B")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        '{\n  "published_file_id": "96001",\n  "title": "ShrinkMod",\n'
        '  "app_id": 42,\n  "game_name": "G"\n}\n',
        encoding="utf-8",
    )

    db.update_game_deploy_config(42, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="96001", title="ShrinkMod", app_id=42))

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod("96001")
    assert first["success"] is True

    target = mods_root / "ShrinkMod"
    assert (target / "A.pak").is_file()
    assert (target / "B.pak").is_file()
    old_man = load_manifest(mod)
    assert old_man is not None
    assert len(old_man.files) == 2

    # New version drops B.pak
    (mod / "B.pak").unlink()

    red = deployer.redeploy_mod("96001")
    assert red["success"] is True
    assert (target / "A.pak").is_file()
    assert not (target / "B.pak").exists()

    new_man = load_manifest(mod)
    assert new_man is not None
    assert len(new_man.files) == 1
    assert new_man.files[0].target.endswith("A.pak")

    info = db.get_mod_deploy_info("96001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED


def test_redeploy_aborts_when_undeploy_fails(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    mod = library / "G" / "M"
    mod.mkdir(parents=True)
    (mod / "x.txt").write_text("x", encoding="utf-8")
    (mod / INFO_DIR_NAME).mkdir()
    (mod / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        '{\n  "published_file_id": "96002",\n  "title": "M",\n'
        '  "app_id": 42,\n  "game_name": "G"\n}\n',
        encoding="utf-8",
    )
    db.update_game_deploy_config(42, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="96002", title="M", app_id=42))

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("96002")["success"]

    monkeypatch.setattr(
        deployer,
        "undeploy_mod",
        lambda *_a, **_k: {"success": False, "error": "simulated undeploy fail"},
    )
    red = deployer.redeploy_mod("96002")
    assert red["success"] is False
    assert "重新部署中止" in red["error"]
