"""Mod enable / disable + deploy gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "enable.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_enable_disable_roundtrip(db: DatabaseManager) -> None:
    pk = create_steam_test_mod(db, external_id="801", title="E").mod_id

    assert db.is_mod_enabled(pk) is True
    assert db.disable_mod(pk) is False
    assert db.is_mod_enabled(pk) is False
    assert db.enable_mod(pk) is True
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.enabled is True


def test_disabled_cannot_deploy(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "801"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    db.update_game_deploy_config(
        1, name="Game", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    created = create_steam_test_mod(db, external_id="801", title="E", app_id=1)
    pk = str(created.mod_id)
    from tests.helpers.identity import prove_managed_folder

    prove_managed_folder(db, folder, handle=pk, title="E", app_id=1, game_name="Game")

    db.disable_mod(pk)

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is False
    assert out["error"] == "Mod disabled"
