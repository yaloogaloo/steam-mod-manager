"""Import mode boundary: Single never discovers children; Multi discovers once."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_OTHER
from services.importers.directory_batch import discover_mod_directories
from ui.import_thread import ImportWorker


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mode_boundary.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    DatabaseManager.reset_instance()


def _ctx() -> dict:
    return {
        "game_id": 1623730,
        "game_name": "Palworld",
        "app_id": 1623730,
        "context": {"game_id": 1623730, "game_name": "Palworld"},
    }


def _run(
    *,
    library: Path,
    folder: Path,
    is_batch_mode: bool,
    platform: str = PLATFORM_NEXUS,
    title: str = "",
) -> object:
    worker = ImportWorker(
        platform=platform,
        library_root=library,
        params={
            "folder": str(folder),
            "use_archive": False,
            "title": (title if title else ("" if is_batch_mode else folder.name)),
            "is_batch_mode": is_batch_mode,
            "nexus_url": "",
            "nexus_id": folder.name,
            "cover_source": "",
            "offline_html_path": "",
            **_ctx(),
        },
    )
    return worker._do_import()


def _managed_mod_dirs(library: Path) -> list[Path]:
    game = library / "Palworld"
    if not game.is_dir():
        return []
    return sorted(p for p in game.iterdir() if p.is_dir())


def test_a_single_custom_subdirs_is_one_mod(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    src = tmp_path / "MyMod"
    for name in ("子目录A", "子目录B", "子目录C"):
        child = src / name
        child.mkdir(parents=True)
        (child / f"file_{name}.txt").write_text("x", encoding="utf-8")
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=src, is_batch_mode=False)
    assert result.success
    assert int(result.imported_count or 0) <= 1
    dirs = _managed_mod_dirs(lib)
    assert len(dirs) == 1
    root = dirs[0]
    assert (root / "子目录A" / "file_子目录A.txt").is_file()
    assert (root / "子目录B" / "file_子目录B.txt").is_file()
    assert (root / "子目录C" / "file_子目录C.txt").is_file()


def test_b_single_inner_only_subdirs_is_one_mod(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    src = tmp_path / "MyMod"
    for name in ("assets", "scripts", "config", "data"):
        d = src / name
        d.mkdir(parents=True)
        (d / "keep.txt").write_text("x", encoding="utf-8")
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=src, is_batch_mode=False)
    assert result.success
    assert len(_managed_mod_dirs(lib)) == 1


def test_c_single_empty_directory_is_one_candidate(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    src = tmp_path / "EmptyMod"
    src.mkdir()
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=src, is_batch_mode=False, platform=PLATFORM_OTHER)
    assert result.success
    assert len(_managed_mod_dirs(lib)) == 1


def test_d_single_files_and_subdirs_is_one_mod(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    src = tmp_path / "MyMod"
    (src / "子目录A").mkdir(parents=True)
    (src / "子目录B").mkdir(parents=True)
    (src / "子目录A" / "a.txt").write_text("a", encoding="utf-8")
    (src / "子目录B" / "b.txt").write_text("b", encoding="utf-8")
    (src / "README.txt").write_text("readme", encoding="utf-8")
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=src, is_batch_mode=False)
    assert result.success
    dirs = _managed_mod_dirs(lib)
    assert len(dirs) == 1
    assert (dirs[0] / "README.txt").is_file()
    assert (dirs[0] / "子目录A" / "a.txt").is_file()


def test_e_multi_three_mods_not_inner_dirs(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    parent = tmp_path / "Mods"
    (parent / "ModA" / "assets").mkdir(parents=True)
    (parent / "ModA" / "scripts").mkdir(parents=True)
    (parent / "ModB" / "assets").mkdir(parents=True)
    (parent / "ModB" / "scripts").mkdir(parents=True)
    (parent / "ModC" / "config").mkdir(parents=True)
    (parent / "ModC" / "data").mkdir(parents=True)
    (parent / "ModA" / "assets" / "a.txt").write_text("a", encoding="utf-8")
    (parent / "ModB" / "scripts" / "b.txt").write_text("b", encoding="utf-8")
    (parent / "ModC" / "config" / "c.txt").write_text("c", encoding="utf-8")
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=parent, is_batch_mode=True)
    assert result.success
    assert int(result.imported_count or 0) == 3
    names = {p.name for p in _managed_mod_dirs(lib)}
    assert names == {"ModA", "ModB", "ModC"}


def test_f_multi_custom_inner_dirs_stay_content(tmp_path: Path, db: DatabaseManager) -> None:
    del db
    parent = tmp_path / "Mods"
    (parent / "ModA" / "Foo").mkdir(parents=True)
    (parent / "ModA" / "Bar").mkdir(parents=True)
    (parent / "ModB" / "Foo").mkdir(parents=True)
    (parent / "ModB" / "Bar").mkdir(parents=True)
    (parent / "ModA" / "Foo" / "a.txt").write_text("a", encoding="utf-8")
    (parent / "ModB" / "Bar" / "b.txt").write_text("b", encoding="utf-8")
    lib = tmp_path / "lib"
    result = _run(library=lib, folder=parent, is_batch_mode=True)
    assert result.success
    assert int(result.imported_count or 0) == 2
    names = {p.name for p in _managed_mod_dirs(lib)}
    assert names == {"ModA", "ModB"}
    assert "Foo" not in names
    assert "Bar" not in names


def test_g_single_archive_stays_one_mod(tmp_path: Path, db: DatabaseManager, monkeypatch) -> None:
    del db
    src = tmp_path / "pack"
    for name in ("assets", "scripts", "config"):
        d = src / name
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    archive = tmp_path / "MyMod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    lib = tmp_path / "lib"
    spy = MagicMock(wraps=discover_mod_directories)
    monkeypatch.setattr("ui.import_thread.discover_mod_directories", spy)
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "source_path": str(archive),
            "use_archive": True,
            "archive_paths": [str(archive)],
            "title": "MyMod",
            "is_batch_mode": False,
            "nexus_url": "",
            "nexus_id": "mymod-zip",
            **_ctx(),
        },
    )
    result = worker._do_import()
    assert result.success
    assert spy.call_count == 0
    assert len(_managed_mod_dirs(lib)) == 1


def test_h_single_never_calls_discover_even_if_heuristic_would_split(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    del db
    src = tmp_path / "MyMod"
    for name in ("Foo", "Bar", "Baz"):
        d = src / name
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    # Heuristic would split this folder in Batch mode.
    assert len(discover_mod_directories(src)) > 1
    lib = tmp_path / "lib"
    spy = MagicMock(side_effect=discover_mod_directories)
    monkeypatch.setattr("ui.import_thread.discover_mod_directories", spy)
    result = _run(library=lib, folder=src, is_batch_mode=False)
    assert result.success
    assert spy.call_count == 0
    assert len(_managed_mod_dirs(lib)) == 1


def test_h_batch_does_call_discover(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    del db
    parent = tmp_path / "Mods"
    for name in ("ModA", "ModB"):
        d = parent / name
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    lib = tmp_path / "lib"
    spy = MagicMock(wraps=discover_mod_directories)
    monkeypatch.setattr("ui.import_thread.discover_mod_directories", spy)
    result = _run(library=lib, folder=parent, is_batch_mode=True)
    assert result.success
    assert spy.call_count >= 1
    assert int(result.imported_count or 0) == 2
