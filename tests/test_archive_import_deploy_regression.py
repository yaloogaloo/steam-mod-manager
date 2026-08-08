"""Archive Import → Deploy regression: peel wrapper, no zip in managed/deploy."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.importers.archive import ArchiveImporter
from services.importers.importer_base import ImportContext

GAME = ImportContext(game_id=100, game_name="SomeGame")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "archive_deploy_reg.db")
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


def test_archive_import_deploy_regression(tmp_path: Path, db: DatabaseManager) -> None:
    """
    test.zip → test/mod.dll + test/config.ini

    Import peels wrapper; managed is flat; deploy copies dll, never zip / nest.
    """
    zpath = _make_zip(
        tmp_path / "test.zip",
        {"test/mod.dll": b"MZ", "test/config.ini": b"a=1"},
    )
    library = tmp_path / "library"
    library.mkdir()
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()

    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_id="8801",
        title="ArchiveMod",
        library_root=library,
        context=GAME,
    )
    assert result.success, result.error
    managed = Path(result.managed_path)
    assert managed.is_dir()

    # Managed: peeled contents, no zip, no nested test/ wrapper
    assert (managed / "mod.dll").is_file()
    assert (managed / "config.ini").is_file()
    assert not (managed / "test.zip").exists()
    assert not (managed / "test" / "mod.dll").exists()
    assert not (managed / "test").exists()
    assert list(managed.rglob("*.zip")) == []

    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))
    deploy = ModDeployer(library_root=library, db=db).deploy_mod(result.mod_id)
    assert deploy["success"] is True, deploy

    dest = install_mods / "ArchiveMod"
    assert (dest / "mod.dll").is_file()
    assert not (dest / "test.zip").exists()
    assert not (dest / "test").exists()
    assert not (dest / "test" / "mod.dll").exists()
    assert list(dest.rglob("*.zip")) == []
