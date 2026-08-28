"""Deploy only enabled mod_files entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import FILE_TYPE_MAIN, FILE_TYPE_OPTIONAL, ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer, resolve_deploy_sources
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_files.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_resolve_deploy_sources_none_when_empty(db: DatabaseManager, tmp_path: Path) -> None:
    db.upsert_mod(ModMetadata(published_file_id="1", title="Steam"))
    source = tmp_path / "mod"
    source.mkdir()
    assert resolve_deploy_sources("1", source, db=db) is None


def test_disabled_files_not_deployed(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()

    mod = library / "SomeGame" / "Multi"
    mod.mkdir(parents=True)
    (mod / "main.bin").write_bytes(b"MAIN")
    (mod / "hat.bin").write_bytes(b"HAT")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"8001","title":"Multi","app_id":100}\n',
        encoding="utf-8",
    )

    from core.game_info import GameInfo

    db.upsert_game(GameInfo(app_id=100, name="SomeGame"))
    db.upsert_mod(ModMetadata(published_file_id="8001", title="Multi", app_id=100))
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))
    db.set_mod_files(
        "8001",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="Main File",
                    filename="main.bin",
                    path="main.bin",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                ),
                ModFileEntry(
                    name="Optional Hat",
                    filename="hat.bin",
                    path="hat.bin",
                    type=FILE_TYPE_OPTIONAL,
                    enabled=False,
                ),
            ]
        ),
    )

    allowed = resolve_deploy_sources("8001", mod, db=db)
    assert allowed is not None
    assert "main.bin" in allowed
    assert "hat.bin" not in allowed

    result = ModDeployer(library_root=library, db=db).deploy_mod("8001")
    assert result["success"] is True, result

    dest = install_mods / "Multi"
    assert (dest / "main.bin").is_file()
    assert not (dest / "hat.bin").exists()
