"""Mod multi-file JSON manager (services.mod_files)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import FILE_TYPE_MAIN, FILE_TYPE_OPTIONAL, ModFileEntry
from services.mod_files import ModFileManager, scan_folder_to_mod_files


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "mod_files.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_add_remove_toggle(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9001", title="Pack"))
    mgr = ModFileManager(db)

    a = mgr.add_file(
        "9001",
        {
            "name": "Main File",
            "filename": "main.pak",
            "path": "files/main.pak",
            "type": "main",
            "enabled": True,
        },
    )
    assert a.id
    assert a.type == FILE_TYPE_MAIN

    b = mgr.add_file(
        "9001",
        ModFileEntry(
            name="Optional Hat",
            filename="hat.pak",
            path="files/hat.pak",
            type=FILE_TYPE_OPTIONAL,
            enabled=False,
        ),
    )
    files = mgr.get_files("9001")
    assert len(files) == 2
    assert mgr.get_enabled_files("9001")[0].filename == "main.pak"

    toggled = mgr.toggle_file("9001", b.id)
    assert toggled is not None
    assert toggled.enabled is True
    assert len(mgr.get_enabled_files("9001")) == 2

    assert mgr.remove_file("9001", a.id) is True
    assert len(mgr.get_files("9001")) == 1
    assert mgr.remove_file("9001", "missing") is False


def test_scan_folder_main_enabled_others_optional(tmp_path: Path) -> None:
    folder = tmp_path / "CharacterA"
    folder.mkdir()
    (folder / "Main.pak").write_bytes(b"M")
    (folder / "HatAddon.pak").write_bytes(b"H")
    (folder / "ClothesAddon.pak").write_bytes(b"C")

    bundle = scan_folder_to_mod_files(folder)
    assert len(bundle.files) == 3
    mains = [f for f in bundle.files if f.type == FILE_TYPE_MAIN]
    assert len(mains) == 1
    assert mains[0].enabled is True
    assert mains[0].filename == "Main.pak"
    optionals = [f for f in bundle.files if f.type == FILE_TYPE_OPTIONAL]
    assert len(optionals) == 2
    assert all(not f.enabled for f in optionals)
