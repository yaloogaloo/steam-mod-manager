"""Archive import: zip extract, mod-root resolve, Nexus import path."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import PLATFORM_NEXUS, DatabaseManager
from services.file_ops import ModFileManager
from services.importers.archive import (
    NO_MOD_FILES_MSG,
    ArchiveImporter,
    extract_archive,
    find_mod_root,
)
from services.importers.importer_base import ImportContext


PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "archive_import.db")
    yield manager
    DatabaseManager.reset_instance()


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_find_mod_root_single_folder(tmp_path: Path) -> None:
    root = tmp_path / "extract"
    mod = root / "ModName"
    mod.mkdir(parents=True)
    (mod / "test.pak").write_bytes(b"pak")
    assert find_mod_root(root) == mod.resolve()


def test_find_mod_root_flat(tmp_path: Path) -> None:
    root = tmp_path / "extract"
    root.mkdir()
    (root / "xxx.pak").write_bytes(b"pak")
    (root / "LogicMods").mkdir()
    assert find_mod_root(root) == root.resolve()


def test_find_mod_root_nested(tmp_path: Path) -> None:
    root = tmp_path / "extract"
    nested = root / "release" / "package" / "ModName"
    nested.mkdir(parents=True)
    (nested / "test.pak").write_bytes(b"pak")
    assert find_mod_root(root) == nested.resolve()


def test_find_mod_root_empty(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (root / "readme.txt").write_text("hi", encoding="utf-8")
    assert find_mod_root(root) is None


def test_zip_import_success(tmp_path: Path, db: DatabaseManager) -> None:
    zpath = _make_zip(
        tmp_path / "abc.zip",
        {"ModName/test.pak": b"DATA", "ModName/Optional/hat.pak": b"H"},
    )
    lib = tmp_path / "library"
    lib.mkdir()
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/55",
        nexus_id="55",
        title="From Zip",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_NEXUS
    assert result.files_count >= 1
    assert ModFileManager(lib).list_managed_mods()


def test_zip_nested_root_and_no_mod_files(tmp_path: Path, db: DatabaseManager) -> None:
    nested = _make_zip(
        tmp_path / "nested.zip",
        {"release/package/ModName/core.pak": b"X"},
    )
    extracted = extract_archive(nested, dest_dir=tmp_path / "out1")
    root = find_mod_root(extracted)
    assert root is not None
    assert root.name == "ModName"

    empty = _make_zip(tmp_path / "empty.zip", {"docs/readme.txt": b"hi"})
    imp = ArchiveImporter(db=db)
    result = imp.import_mod(
        archive_path=empty,
        platform=PLATFORM_NEXUS,
        nexus_id="99",
        library_root=tmp_path / "lib2",
        context=PALWORLD,
    )
    assert not result.success
    assert NO_MOD_FILES_MSG in result.error or "未找到" in result.error


def test_cleanup_import_cache_after_success(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.importers import archive as archive_mod

    cache = tmp_path / "import_cache"
    cache.mkdir()
    monkeypatch.setattr(archive_mod, "import_cache_root", lambda: cache)

    zpath = _make_zip(tmp_path / "ok.zip", {"Mod/core.pak": b"X"})
    # sibling cover
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (tmp_path / "thumbnail.png").write_bytes(png)

    lib = tmp_path / "lib"
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/777",
        nexus_id="777",
        title="Covered",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    # Successful import removes the uuid extract folder under cache
    leftovers = [p for p in cache.iterdir() if p.is_dir()]
    assert leftovers == []
    # Cover installed from sibling
    assert result.managed_path
    from services.file_ops import COVER_BASENAME, INFO_DIR_NAME

    covers = list(Path(result.managed_path).joinpath(INFO_DIR_NAME).glob(f"{COVER_BASENAME}.*"))
    assert covers
