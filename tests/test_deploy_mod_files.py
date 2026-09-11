"""Deploy only enabled mod_files entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import FILE_TYPE_MAIN, FILE_TYPE_OPTIONAL, ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer, resolve_deploy_sources
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_files.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_resolve_deploy_sources_none_when_empty(db: DatabaseManager, tmp_path: Path) -> None:
    create_steam_test_mod(db, external_id="1", title="Steam")

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

    db.upsert_game(GameInfo(app_id=100, name="SomeGame"))
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))
    created = create_steam_test_mod(db, external_id="8001", title="Multi", app_id=100)
    write_info_sidecar(
        mod,
        internal_id=str(created.mod_id),
        title="Multi",
        external_id="8001",
        workspace_id=str(created.workspace_id or "8001"),
        app_id=100,
        game_name="SomeGame",
    )
    bind_managed_path(db, created.mod_id, mod, title="Multi", game_name="SomeGame")

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
