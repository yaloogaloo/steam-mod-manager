"""Mod enable / disable + deploy gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "enable.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_enable_disable_roundtrip(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="801", title="E"))
    assert db.is_mod_enabled(801) is True
    assert db.disable_mod(801) is False
    assert db.is_mod_enabled(801) is False
    assert db.enable_mod(801) is True
    info = db.get_mod_display_info(801)
    assert info is not None
    assert info.enabled is True


def test_disabled_cannot_deploy(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "801"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"801","title":"E","app_id":1,"game_name":"Game"}',
        encoding="utf-8",
    )
    db.update_game_deploy_config(
        1, name="Game", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    db.upsert_mod(ModMetadata(published_file_id="801", title="E", app_id=1))
    db.disable_mod(801)

    out = ModDeployer(library_root=library, db=db).deploy_mod(801)
    assert out["success"] is False
    assert out["error"] == "Mod disabled"
