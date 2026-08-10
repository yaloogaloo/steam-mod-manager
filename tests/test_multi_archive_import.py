"""Multi-archive Nexus/GitHub import keeps archives as FileEntry sources."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_ROLE_UNKNOWN,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    SOURCE_TYPE_NEXUS,
)
from core.game_info import GameInfo
from services.importers.archive import ArchiveImporter
from services.importers.importer_base import ImportContext
from services.importers.source_files import META_ARCHIVE_NAME, META_INTERNAL_PATH
from services.importers.steam import SteamImporter

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "multi_archive.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_multi_archive_import(tmp_path: Path, db: DatabaseManager) -> None:
    """Two zips → one Mod with two archive-source FileEntries (not extracted flats)."""
    main = _make_zip(tmp_path / "Main.zip", {"Mod/core.pak": b"MAIN"})
    optional = _make_zip(tmp_path / "Optional.zip", {"Addon/hat.pak": b"OPT"})
    lib = tmp_path / "library"
    lib.mkdir()

    result = ArchiveImporter(db=db).import_mod(
        archive_paths=[main, optional],
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/501",
        nexus_id="501",
        title="MultiPack",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert result.files_count == 2

    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 2
    names = {f.metadata.get(META_ARCHIVE_NAME) for f in files}
    assert names == {"Main.zip", "Optional.zip"}
    assert all(f.metadata.get(META_INTERNAL_PATH) == "" for f in files)
    assert all(f.source_type == SOURCE_TYPE_NEXUS for f in files)
    assert all(f.file_role == FILE_ROLE_UNKNOWN for f in files)
    assert all(f.display_name for f in files)

    managed = Path(result.managed_path)
    assert (managed / "Main.zip").is_file()
    assert (managed / "Optional.zip").is_file()
    # Must not flatten archive members as top-level managed files.
    assert not (managed / "core.pak").exists()
    assert not (managed / "hat.pak").exists()
    assert not list(managed.rglob("*.pak"))


def test_single_archive_is_source_unit(tmp_path: Path, db: DatabaseManager) -> None:
    """One zip → one FileEntry for the archive itself (not each member)."""
    zpath = _make_zip(
        tmp_path / "Pack.zip",
        {"a.txt": b"1", "b.bin": b"2", "sub/c.cfg": b"3"},
    )
    lib = tmp_path / "lib"
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_id="502",
        title="Pack",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 1
    entry = files[0]
    assert entry.metadata.get(META_ARCHIVE_NAME) == "Pack.zip"
    assert entry.metadata.get(META_INTERNAL_PATH) == ""
    assert entry.file_role == FILE_ROLE_UNKNOWN
    assert (Path(result.managed_path) / "Pack.zip").is_file()


def test_github_multi_archive_unknown_role(tmp_path: Path, db: DatabaseManager) -> None:
    a = _make_zip(tmp_path / "release.zip", {"x.pak": b"1"})
    b = _make_zip(tmp_path / "dev.zip", {"y.pak": b"2"})
    result = ArchiveImporter(db=db).import_mod(
        archive_paths=[a, b],
        platform=PLATFORM_GITHUB,
        github_url="https://github.com/user/multi-pack",
        title="GH Multi",
        library_root=tmp_path / "gh_lib",
        context=PALWORLD,
    )
    assert result.success, result.error
    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 2
    assert all(f.file_role == FILE_ROLE_UNKNOWN for f in files)
    assert {f.metadata.get(META_ARCHIVE_NAME) for f in files} == {
        "release.zip",
        "dev.zip",
    }


def test_steam_import_unaffected_by_archive_model(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Steam Workshop folder import stays whole-mod / empty mod_files."""
    lib = tmp_path / "steam_lib"
    result = SteamImporter(db=db).import_mod(
        workshop_id="3761838599",
        title="SteamOnly",
        library_root=lib,
        game_name="Palworld",
        app_id=1623730,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_STEAM
    assert result.files_count == 0
    assert db.get_mod_files(result.mod_id).files == []
