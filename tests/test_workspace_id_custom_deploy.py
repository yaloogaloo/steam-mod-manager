"""Workspace ID assignment and custom deploy path."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    resolve_workspace_id,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "ws.db")
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    yield manager
    DatabaseManager.reset_instance()


def test_resolve_workspace_id_rules() -> None:
    assert (
        resolve_workspace_id(PLATFORM_STEAM, mod_id="3761838546") == "3761838546"
    )
    assert (
        resolve_workspace_id(
            PLATFORM_NEXUS,
            source_url="https://www.nexusmods.com/palworld/mods/336",
        )
        == "336"
    )
    assert resolve_workspace_id(PLATFORM_GITHUB, mod_id="1") == ""
    assert resolve_workspace_id(PLATFORM_OTHER, mod_id="1") == ""
    assert (
        resolve_workspace_id(
            PLATFORM_STEAM, mod_id="1", existing="keep-me"
        )
        == "keep-me"
    )


def test_steam_upsert_sets_workspace_id(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="4242", title="Steam Mod", app_id=100))
    info = db.get_mod_display_info(4242)
    assert info is not None
    assert info.workspace_id == "4242"


def test_nexus_register_workspace_from_url(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="999",
        source_url="https://www.nexusmods.com/game/mods/999",
        title="Nexus Mod",
        app_id=100,
        game_name="SomeGame",
    )
    assert info.workspace_id == "999"


def test_github_gets_generated_numeric_workspace(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="GH",
        app_id=100,
        game_name="SomeGame",
    )
    assert info.workspace_id
    assert info.workspace_id.isdigit()
    assert info.workspace_id != info.mod_id


def test_custom_deploy_path_copies_contents_not_shell(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    managed = library / "SomeGame" / "SpecialMod"
    managed.mkdir(parents=True)
    (managed / "payload.txt").write_text("hello", encoding="utf-8")
    nested = managed / "sub"
    nested.mkdir()
    (nested / "inner.bin").write_text("bin", encoding="utf-8")
    (managed / INFO_DIR_NAME).mkdir()
    (managed / INFO_DIR_NAME / "mod.json").write_text(
        '{"published_file_id":"88001","title":"SpecialMod","app_id":100}',
        encoding="utf-8",
    )

    db.upsert_mod(
        ModMetadata(published_file_id="88001", title="SpecialMod", app_id=100)
    )
    custom = tmp_path / "game_root" / "custom_target"
    custom.mkdir(parents=True)
    db.update_mod_user_metadata(
        88001,
        {
            "display_name": "SpecialMod",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": str(custom),
        },
    )

    # Intentionally leave game mod_path empty — custom path must still deploy.
    db.update_game_deploy_config(100, name="SomeGame", mod_path="")

    result = ModDeployer(library, db=db).deploy_mod(88001)
    assert result.get("success") is True, result
    assert (custom / "payload.txt").is_file()
    assert (custom / "sub" / "inner.bin").is_file()
    # Must NOT nest the managed shell folder name.
    assert not (custom / "SpecialMod").exists()
    assert not (custom / INFO_DIR_NAME).exists()
