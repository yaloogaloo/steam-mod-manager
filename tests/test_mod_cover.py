"""Explicit Mod cover import / replace — no auto image scan."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import COVER_BASENAME, INFO_DIR_NAME, ModFileManager
from services.importers.archive import ArchiveImporter
from services.importers.image_picker import (
    apply_cover_to_mod,
    install_cover_file,
    suggest_sibling_covers,
)
from services.importers.importer_base import ImportContext
from services.importers.materialize import materialize_imported_mod
from services.importers.nexus import NexusImporter


PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


def _write_png(path: Path, size: int = 64) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + (b"\x00" * max(0, size - len(png))))


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cover.db")
    yield manager
    DatabaseManager.reset_instance()


def test_import_with_explicit_cover(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.zip").write_bytes(b"PK")
    cover = tmp_path / "cover.png"
    _write_png(cover)

    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="AutoPickUp",
        nexus_url="https://www.nexusmods.com/palworld/mods/501",
        nexus_id="501",
        library_root=lib,
        context=PALWORLD,
        cover_source=cover,
    )
    assert result.success, result.error
    assert result.managed_path
    dest = Path(result.managed_path)
    installed = dest / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert installed.is_file()

    meta = ModFileManager(lib).load_metadata(dest)
    assert meta is not None
    assert meta.cover_path == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"

    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.cover_path == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"

    files = db.get_mod_files(result.mod_id)
    assert "cover.png" not in [f.filename for f in files.files]
    assert "mod.zip" in [f.filename for f in files.files]


def test_import_without_cover_auto_binds_directory_image(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Directory images become cover + are excluded from the managed copy."""
    del db
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    _write_png(src / "icon.png")
    _write_png(src / "texture.png")

    lib = tmp_path / "library"
    dest = materialize_imported_mod(
        library_root=lib,
        mod_id="9000000000000999",
        title="AutoCover",
        game_name="Palworld",
        source_folder=src,
        context=PALWORLD,
    )
    meta = ModFileManager(lib).load_metadata(dest)
    assert meta is not None
    assert (meta.cover_path or "").startswith(f"{INFO_DIR_NAME}/{COVER_BASENAME}")
    assert list((dest / INFO_DIR_NAME).glob(f"{COVER_BASENAME}.*"))
    assert (dest / "mod.pak").is_file()
    assert not (dest / "icon.png").exists()
    assert not (dest / "texture.png").exists()


def test_archive_sibling_not_auto_bound(tmp_path: Path, db: DatabaseManager) -> None:
    zpath = tmp_path / "PalAnalyzer.zip"
    with ZipFile(zpath, "w") as zf:
        zf.writestr("mod.pak", b"pak")
    sibling = tmp_path / "PalAnalyzer.png"
    _write_png(sibling)

    suggested = suggest_sibling_covers(zpath)
    assert suggested and suggested[0].name == "PalAnalyzer.png"

    lib = tmp_path / "lib"
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/777",
        nexus_id="777",
        title="PalAnalyzer",
        library_root=lib,
        context=PALWORLD,
        # no cover_source — sibling must not auto-install
    )
    assert result.success, result.error
    dest = Path(result.managed_path)
    assert not list((dest / INFO_DIR_NAME).glob(f"{COVER_BASENAME}.*"))
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert (row.cover_path or "") == ""


def test_archive_with_explicit_cover(tmp_path: Path, db: DatabaseManager) -> None:
    zpath = tmp_path / "PalAnalyzer.zip"
    with ZipFile(zpath, "w") as zf:
        zf.writestr("mod.pak", b"pak")
    cover = tmp_path / "PalAnalyzer.png"
    _write_png(cover)

    lib = tmp_path / "lib"
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_url="https://www.nexusmods.com/palworld/mods/778",
        nexus_id="778",
        title="PalAnalyzer",
        library_root=lib,
        context=PALWORLD,
        cover_source=cover,
    )
    assert result.success, result.error
    dest = Path(result.managed_path)
    assert (dest / INFO_DIR_NAME / f"{COVER_BASENAME}.png").is_file()
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.cover_path.endswith(f"{COVER_BASENAME}.png")


def test_replace_cover_updates_db(tmp_path: Path, db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="88",
        source_url="https://www.nexusmods.com/palworld/mods/88",
        title="Swap",
        app_id=1623730,
        game_name="Palworld",
    )
    dest = tmp_path / "library" / "Palworld" / "Swap"
    (dest / INFO_DIR_NAME).mkdir(parents=True)
    (dest / INFO_DIR_NAME / "mod.json").write_text(
        json.dumps({"published_file_id": info.mod_id, "title": "Swap"}),
        encoding="utf-8",
    )

    c1 = tmp_path / "c1.png"
    c2 = tmp_path / "c2.webp"
    _write_png(c1)
    c2.write_bytes(b"RIFF....WEBP")

    rel1 = apply_cover_to_mod(dest, c1, mod_id=info.mod_id)
    assert rel1.endswith(".png")
    assert db.get_mod_display_info(info.mod_id).cover_path == rel1

    rel2 = apply_cover_to_mod(dest, c2, mod_id=info.mod_id)
    assert rel2.endswith(".webp")
    assert db.get_mod_display_info(info.mod_id).cover_path == rel2
    assert (dest / INFO_DIR_NAME / f"{COVER_BASENAME}.webp").is_file()
    assert not (dest / INFO_DIR_NAME / f"{COVER_BASENAME}.png").exists()


def test_cover_not_in_mod_files_scan(tmp_path: Path) -> None:
    from services.importers.local_scanner import scan_mod_directory

    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / "a.zip").write_bytes(b"PK")
    (folder / "a.pak").write_bytes(b"pak")
    _write_png(folder / "cover.png")
    bundle = scan_mod_directory(folder)
    assert [f.filename for f in bundle.files] == ["a.zip"]


def test_install_cover_file_writes_info(tmp_path: Path) -> None:
    dest = tmp_path / "mod"
    dest.mkdir()
    src = tmp_path / "art.jpg"
    _write_png(src)  # content irrelevant; extension drives name
    # use real jpg suffix
    src = tmp_path / "art.png"
    _write_png(src)
    installed = install_cover_file(src, dest)
    assert installed is not None
    assert installed.name == f"{COVER_BASENAME}.png"
