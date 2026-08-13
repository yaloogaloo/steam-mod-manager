"""Directory batch import — multi-Mod parent folders + sidecar filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.importers.directory_batch import (
    discover_mod_directories,
    extract_directory_sidecars,
)
from services.importers.materialize import materialize_imported_mod
from services.importers.nexus import NexusImporter


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "batch_import.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_discover_multi_mod_parent(tmp_path: Path) -> None:
    parent = tmp_path / "mods"
    a = parent / "CoolModA"
    b = parent / "CoolModB"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "main.pak").write_bytes(b"pak-a")
    (b / "main.pak").write_bytes(b"pak-b")

    roots = discover_mod_directories(parent)
    assert [p.name for p in roots] == ["CoolModA", "CoolModB"]


def test_discover_single_mod_with_scripts(tmp_path: Path) -> None:
    mod = tmp_path / "MyMod"
    scripts = mod / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "main.lua").write_text("-- lua", encoding="utf-8")

    roots = discover_mod_directories(mod)
    assert roots == [mod]


def test_extract_sidecars_silent_when_missing(tmp_path: Path) -> None:
    mod = tmp_path / "Bare"
    mod.mkdir()
    (mod / "mod.pak").write_bytes(b"x")
    sidecars = extract_directory_sidecars(mod)
    assert sidecars.cover is None
    assert sidecars.offline_page is None
    assert sidecars.ignore_paths == ()


def test_materialize_excludes_cover_and_mhtml(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    src = tmp_path / "src_mod"
    src.mkdir()
    (src / "content.pak").write_bytes(b"pak")
    cover = src / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    mhtml = src / "page.mhtml"
    mhtml.write_text("From: <saved>\nSnapshot-Content-Location: https://x\n", encoding="utf-8")

    library = tmp_path / "library"
    dest = materialize_imported_mod(
        library_root=library,
        mod_id="900001",
        title="SidecarMod",
        game_name="Palworld",
        source_folder=src,
        allow_invalid_game_name=True,
    )

    assert (dest / "content.pak").is_file()
    assert not (dest / "cover.png").exists()
    assert not (dest / "page.mhtml").exists()
    # Cover should be installed under .info/
    info_covers = list((dest / ".info").glob("cover.*"))
    assert info_covers


def test_nexus_batch_parent_imports_each_subdir(
    tmp_path: Path, db: DatabaseManager
) -> None:
    parent = tmp_path / "batch"
    mod_a = parent / "AlphaMod"
    mod_b = parent / "BetaMod"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "a.pak").write_bytes(b"a")
    (mod_b / "b.pak").write_bytes(b"b")
    (mod_a / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    library = tmp_path / "lib"
    ctx = {"game_id": 1623730, "game_name": "Palworld"}
    importer = NexusImporter(db=db)

    from services.importers.directory_batch import discover_mod_directories

    results = []
    for folder in discover_mod_directories(parent):
        results.append(
            importer.import_mod(
                source_folder=folder,
                title=folder.name,
                nexus_id=folder.name,
                library_root=library,
                context=ctx,
                is_batch_mode=True,
            )
        )

    assert all(r.success for r in results)
    assert len(results) == 2
    assert all(r.source_url == "" for r in results)
    managed_a = Path(results[0].managed_path)
    assert (managed_a / "a.pak").is_file()
    assert not (managed_a / "preview.png").exists()
