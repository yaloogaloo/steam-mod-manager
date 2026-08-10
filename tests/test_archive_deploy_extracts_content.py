"""Archive FileEntries deploy extracted content, not the zip itself."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.importers.archive import ArchiveImporter
from services.importers.importer_base import ImportContext

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "archive_deploy.db")
    manager.upsert_game(
        GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame")
    )
    yield manager
    DatabaseManager.reset_instance()


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_archive_deploy_extracts_content(tmp_path: Path, db: DatabaseManager) -> None:
    """
    Nexus archive import keeps zip as source; deploy extracts members.

    Deploy target must contain mod.dll / config.ini — never the .zip.
    """
    zpath = _make_zip(
        tmp_path / "mod.zip",
        {"mod.dll": b"MZ", "config.ini": b"a=1"},
    )
    library = tmp_path / "library"
    library.mkdir()
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()

    ctx = ImportContext(game_id=100, game_name="SomeGame")
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_id="8801",
        title="ArchiveMod",
        library_root=library,
        context=ctx,
    )
    assert result.success, result.error
    assert result.files_count == 1
    managed = Path(result.managed_path)
    assert (managed / "mod.zip").is_file()

    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    deploy = ModDeployer(library_root=library, db=db).deploy_mod(result.mod_id)
    assert deploy["success"] is True, deploy

    dest = install_mods / "ArchiveMod"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
    assert not (dest / "mod.zip").exists()
    assert list(dest.rglob("*.zip")) == []


def test_steam_empty_bundle_deploy_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    """Steam-style empty mod_files still deploys the whole managed folder."""
    library = tmp_path / "library"
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()

    mod = library / "SomeGame" / "SteamMod"
    mod.mkdir(parents=True)
    (mod / "content.pak").write_bytes(b"PAK")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"8802","title":"SteamMod","app_id":100}\n',
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="8802", title="SteamMod", app_id=100))
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    result = ModDeployer(library_root=library, db=db).deploy_mod("8802")
    assert result["success"] is True, result
    assert (install_mods / "SteamMod" / "content.pak").is_file()
