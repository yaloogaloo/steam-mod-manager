"""Platform importers: Steam / Nexus / GitHub."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    DatabaseManager,
)
from services.importers import (
    GithubImporter,
    NexusImporter,
    SteamImporter,
    detect_importer,
)
from services.importers.importer_base import ImportContext


PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "importers.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_steam_importer(db: DatabaseManager) -> None:
    imp = SteamImporter(db=db)
    assert imp.detect("3761838546")
    assert imp.detect(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546"
    )
    result = imp.import_mod(workshop_id="3761838546", title="Cool Steam Mod")
    assert result.success
    assert result.platform == PLATFORM_STEAM
    assert result.external_id == "3761838546"
    assert "filedetails/?id=3761838546" in result.source_url
    assert result.files_count == 0
    info = db.get_mod_display_info("3761838546")
    assert info is not None
    assert info.mod_files.files == []


def test_nexus_importer_multi_file(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "CharacterA"
    folder.mkdir()
    (folder / "Main.pak").write_bytes(b"M")
    (folder / "HatAddon.pak").write_bytes(b"H")
    (folder / "ClothesAddon.pak").write_bytes(b"C")

    imp = NexusImporter(db=db)
    assert imp.detect("https://www.nexusmods.com/game/mods/999")
    result = imp.import_mod(
        source_folder=folder,
        title="Character Replacement Mod",
        nexus_url="https://www.nexusmods.com/game/mods/999",
        nexus_id="999",
        context=PALWORLD,
    )
    assert result.success
    assert result.platform == PLATFORM_NEXUS
    assert result.external_id == "999"
    assert result.files_count == 3
    files = db.get_mod_files(result.mod_id).files
    enabled = [f for f in files if f.enabled]
    assert len(enabled) == 1
    assert enabled[0].filename == "Main.pak"


def test_github_importer(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "mod.zip").write_bytes(b"Z")

    imp = GithubImporter(db=db)
    assert imp.detect("https://github.com/user/project")
    result = imp.import_mod(
        github_url="https://github.com/user/project",
        source_folder=folder,
        title="Repo Mod",
        context=PALWORLD,
    )
    assert result.success
    assert result.platform == PLATFORM_GITHUB
    assert result.external_id == "user/project"
    assert result.files_count == 1


def test_detect_importer_order(db: DatabaseManager) -> None:
    assert isinstance(detect_importer("12345", db=db), SteamImporter)
    assert isinstance(
        detect_importer("https://www.nexusmods.com/x/mods/1", db=db), NexusImporter
    )
    assert isinstance(
        detect_importer("https://github.com/a/b", db=db), GithubImporter
    )
