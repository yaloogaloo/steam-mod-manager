"""Generic Mod platform fields: steam / nexus / github + multi-file JSON."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    NON_STEAM_MOD_ID_BASE,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    DatabaseManager,
)
from core.mod_platform import ModFileEntry, ModFilesBundle, steam_workshop_url
from core.models import ModMetadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "platform.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_migration_adds_platform_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE games (
            app_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            header_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE mods (
            mod_id INTEGER PRIMARY KEY,
            app_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT INTO mods (mod_id, app_id, title, preview_url, description, updated_at)
        VALUES (4242, 0, 'Legacy Steam Mod', '', '', '2020-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)
    cols = {
        str(r[1])
        for r in manager._conn.execute("PRAGMA table_info(mods)").fetchall()
    }
    assert "platform" in cols
    assert "source_url" in cols
    assert "external_id" in cols
    assert "mod_files" in cols

    info = manager.get_mod_display_info(4242)
    assert info is not None
    assert info.platform == PLATFORM_STEAM
    assert info.external_id == "4242"
    assert "filedetails/?id=4242" in info.source_url
    manager.close()
    DatabaseManager.reset_instance()


def test_steam_upsert_sets_platform_fields(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="555", title="Cool"))
    info = db.get_mod_display_info(555)
    assert info is not None
    assert info.platform == PLATFORM_STEAM
    assert info.external_id == "555"
    assert info.source_url == steam_workshop_url(555)
    assert info.mod_files.files == []


def test_nexus_multi_file_stays_one_mod(db: DatabaseManager) -> None:
    bundle = ModFilesBundle(
        files=[
            ModFileEntry(name="Main File", type="main", path="CharacterA.zip", enabled=True),
            ModFileEntry(
                name="Hat Optional", type="optional", path="Hat.zip", enabled=False
            ),
        ]
    )
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="12345",
        source_url="https://www.nexusmods.com/game/mods/12345",
        title="Character Pack",
        app_id=1623730,
        game_name="Palworld",
        mod_files=bundle,
    )
    assert int(info.mod_id) >= NON_STEAM_MOD_ID_BASE
    assert info.platform == PLATFORM_NEXUS
    assert info.external_id == "12345"
    files = db.get_mod_files(info.mod_id)
    assert len(files.files) == 2
    assert files.files[0].path == "CharacterA.zip"
    assert files.files[1].enabled is False

    # Re-register same Nexus id → same row
    again = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="12345",
        source_url="https://www.nexusmods.com/game/mods/12345",
        title="Character Pack v2",
        app_id=1623730,
        game_name="Palworld",
    )
    assert again.mod_id == info.mod_id
    assert again.steam_name == "Character Pack v2"


def test_github_register(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="Repo Mod",
        app_id=1623730,
        game_name="Palworld",
    )
    assert info.platform == PLATFORM_GITHUB
    assert info.external_id == "owner/repo"
    found = db.find_mod_by_external(PLATFORM_GITHUB, "owner/repo", app_id=1623730)
    assert found is not None
    assert found.mod_id == info.mod_id


def test_set_mod_files_roundtrip(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="77", title="Pak"))
    db.set_mod_files(
        77,
        {
            "files": [
                {"name": "A", "type": "main", "path": "a.pak", "enabled": True},
            ]
        },
    )
    got = db.get_mod_files(77)
    assert len(got.files) == 1
    assert got.files[0].name == "A"
    assert got.to_dict()["files"][0]["path"] == "a.pak"


def test_allocate_mod_id_high_range(db: DatabaseManager) -> None:
    a = db.allocate_mod_id()
    assert a == NON_STEAM_MOD_ID_BASE
    db.update_mod_platform_info(
        a, platform=PLATFORM_NEXUS, external_id="1", title="X"
    )
    b = db.allocate_mod_id()
    assert b == a + 1
