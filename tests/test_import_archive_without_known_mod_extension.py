"""Archive import must accept packs without known Mod extensions (E5)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_ROLE_UNKNOWN,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_NEXUS,
)
from services.importers.archive import ArchiveImporter, NO_MOD_FILES_MSG
from services.importers.importer_base import ImportContext
from services.importers.source_files import META_ARCHIVE_NAME, META_INTERNAL_PATH

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "archive_any.db")
    yield manager
    DatabaseManager.reset_instance()


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_import_archive_without_known_mod_extension(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """
    zip with only readme.txt + folder/data.bin must import successfully.

    Archive is one FileEntry source unit — members are not flattened.
    """
    zpath = _make_zip(
        tmp_path / "misc.zip",
        {"readme.txt": b"hello", "folder/data.bin": b"\x00\x01\x02"},
    )

    lib = tmp_path / "library"
    lib.mkdir()
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/9001",
        nexus_id="9001",
        title="Docs Pack",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert NO_MOD_FILES_MSG not in (result.error or "")
    assert result.files_count == 1

    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 1
    entry = files[0]
    assert entry.filename == "misc.zip"
    assert entry.metadata.get(META_ARCHIVE_NAME) == "misc.zip"
    assert entry.metadata.get(META_INTERNAL_PATH) == ""
    assert entry.source_type == SOURCE_TYPE_NEXUS
    assert entry.file_role == FILE_ROLE_UNKNOWN
    assert entry.display_name
    assert (Path(result.managed_path) / "misc.zip").is_file()


def test_github_archive_without_known_mod_extension(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """GitHub archive path: zip imports as one source unit; role stays unknown."""
    zpath = _make_zip(
        tmp_path / "gh-docs.zip",
        {"NOTES.md": b"# notes", "assets/blob.dat": b"\xff\xfe"},
    )
    lib = tmp_path / "gh_lib"
    lib.mkdir()
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_GITHUB,
        github_url="https://github.com/user/docs-pack",
        title="GH Docs",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert result.files_count == 1
    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 1
    assert files[0].metadata.get(META_ARCHIVE_NAME) == "gh-docs.zip"
    assert files[0].source_type == SOURCE_TYPE_GITHUB
    assert files[0].file_role == FILE_ROLE_UNKNOWN


def test_multi_file_zip_is_single_archive_entry(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Multi-file zip → one archive FileEntry (not one entry per member)."""
    zpath = _make_zip(
        tmp_path / "multi.zip",
        {
            "a.txt": b"1",
            "b.bin": b"2",
            "sub/c.cfg": b"3",
        },
    )
    lib = tmp_path / "multi_lib"
    lib.mkdir()
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_id="9002",
        title="Multi",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 1
    assert files[0].metadata.get(META_ARCHIVE_NAME) == "multi.zip"
    assert files[0].file_role == FILE_ROLE_UNKNOWN
