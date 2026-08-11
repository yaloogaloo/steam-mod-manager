"""Empty payload import is allowed; deploy must refuse missing content."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import MISSING_CONTENT_DEPLOY_ERROR, ModDeployer
from services.file_ops import (
    MISSING_CONTENT_METADATA_KEY,
    apply_missing_content_marker,
    clear_missing_content_if_present,
    is_missing_mod_content,
    read_info_metadata_dict,
    read_is_missing_content,
)
from services.importers.archive import ArchiveImporter
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "test.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_empty_directory_import_marks_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    empty = tmp_path / "empty_mod"
    empty.mkdir()

    result = NexusImporter(db=db).import_mod(
        source_folder=empty,
        title="Empty Dir Mod",
        nexus_id="900001",
        library_root=library,
        app_id=1623730,
        game_name="Palworld",
        context=ImportContext(game_id=1623730, game_name="Palworld"),
    )
    assert result.success
    assert result.managed_path
    managed = Path(result.managed_path)
    assert is_missing_mod_content(managed)
    meta = read_info_metadata_dict(managed) or {}
    assert meta.get(MISSING_CONTENT_METADATA_KEY) is True


def test_empty_zip_import_marks_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w"):
        pass

    result = ArchiveImporter(db=db).import_mod(
        archive_path=empty_zip,
        platform=PLATFORM_NEXUS,
        title="Empty Zip Mod",
        nexus_id="900002",
        library_root=library,
        app_id=1623730,
        game_name="Palworld",
        context=ImportContext(game_id=1623730, game_name="Palworld"),
    )
    assert result.success, result.error
    assert result.managed_path
    managed = Path(result.managed_path)
    assert read_is_missing_content(managed)
    meta = read_info_metadata_dict(managed) or {}
    assert meta.get(MISSING_CONTENT_METADATA_KEY) is True


def test_deploy_blocks_missing_content(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    empty = tmp_path / "empty_mod2"
    empty.mkdir()

    result = NexusImporter(db=db).import_mod(
        source_folder=empty,
        title="No Deploy",
        nexus_id="900003",
        library_root=library,
        app_id=1623730,
        game_name="Palworld",
        context=ImportContext(game_id=1623730, game_name="Palworld"),
    )
    assert result.success
    mid = str(result.mod_id)
    apply_missing_content_marker(result.managed_path)

    install = tmp_path / "game" / "mods"
    install.mkdir(parents=True)
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(tmp_path / "game"),
        mod_path=str(install),
        deploy_type="palworld_pak",
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert out.get("success") is False
    assert MISSING_CONTENT_DEPLOY_ERROR in str(out.get("error") or "")
    assert out.get("is_missing_content") is True


def test_clear_missing_content_when_payload_appears(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        '{"published_file_id":"1","is_missing_content":true}',
        encoding="utf-8",
    )
    assert is_missing_mod_content(folder)
    assert not clear_missing_content_if_present(folder)
    (folder / "payload.pak").write_bytes(b"x")
    assert clear_missing_content_if_present(folder)
    meta = read_info_metadata_dict(folder) or {}
    assert meta.get(MISSING_CONTENT_METADATA_KEY) is False
    assert not read_is_missing_content(folder)
