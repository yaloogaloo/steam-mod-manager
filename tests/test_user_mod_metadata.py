"""Tests: user Mod metadata in SQLite (persist + Steam sync preserve)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, ModDisplayInfo
from core.models import ModMetadata
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "test_mods.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_migration_adds_user_columns(tmp_path: Path) -> None:
    """Legacy DB without user columns gains them on open."""
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
        VALUES (111, 1, 'Steam Title', '', 'desc', '2020-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)
    # Placeholder game for app_id=0 FK (created by _init_schema)
    cols = {
        str(r[1])
        for r in manager._conn.execute("PRAGMA table_info(mods)").fetchall()
    }
    assert "display_name" in cols
    assert "custom_description" in cols
    assert "user_notes" in cols
    assert "favorite" in cols

    info = manager.get_mod_display_info(111)
    assert info is not None
    assert info.steam_name == "Steam Title"
    assert info.display_name == "Steam Title"
    manager.close()
    DatabaseManager.reset_instance()


def test_display_name_override_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    DatabaseManager.reset_instance()
    db1 = DatabaseManager.instance(path)
    created = create_steam_test_mod(db1, external_id="222", title="Steam Original")
    pk = str(created.mod_id)
    db1.update_mod_user_metadata(
        pk,
        {
            "display_name": "我的显示名",
            "custom_description": "自定义",
            "user_notes": "备注A",
            "favorite": True,
        },
    )
    db1.close()
    DatabaseManager.reset_instance()

    db2 = DatabaseManager.instance(path)
    info = db2.get_mod_display_info(pk)
    assert info is not None
    assert info.user_display_name == "我的显示名"
    assert info.display_name == "我的显示名"
    assert info.steam_name == "Steam Original"
    assert info.custom_description == "自定义"
    assert info.user_notes == "备注A"
    assert info.favorite is True
    db2.close()
    DatabaseManager.reset_instance()


def test_steam_upsert_preserves_user_fields(db: DatabaseManager) -> None:
    from core.game_info import GameInfo

    db.upsert_game(GameInfo(app_id=10, name="Game Ten"))
    db.upsert_game(GameInfo(app_id=20, name="Game Twenty"))
    created = create_steam_test_mod(
        db, external_id="333", title="Old Steam", app_id=10, game_name="Game Ten"
    )
    pk = str(created.mod_id)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "User Name",
            "custom_description": "User desc",
            "user_notes": "Keep me",
            "favorite": 1,
        },
    )

    db.upsert_mod(
        ModMetadata(
            published_file_id="333",
            title="New Steam Title",
            description="New Steam Desc",
            preview_url="http://new",
            app_id=20,
        )
    )

    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.steam_name == "New Steam Title"
    assert info.steam_description == "New Steam Desc"
    assert info.preview_url == "http://new"
    assert info.app_id == 20
    assert info.user_display_name == "User Name"
    assert info.display_name == "User Name"
    assert info.custom_description == "User desc"
    assert info.user_notes == "Keep me"
    assert info.favorite is True


def test_steam_and_user_name_both_available(db: DatabaseManager) -> None:
    created = create_steam_test_mod(db, external_id="444", title="Steam Workshop Name")
    pk = str(created.mod_id)
    db.update_mod_user_metadata(pk, {"display_name": "Local Nickname"})
    info = db.get_mod_display_info(pk)
    assert isinstance(info, ModDisplayInfo)
    assert info.display_name == "Local Nickname"
    assert info.steam_name == "Steam Workshop Name"
    assert info.user_display_name == "Local Nickname"


def test_empty_display_name_falls_back_to_steam(db: DatabaseManager) -> None:
    created = create_steam_test_mod(db, external_id="555", title="Only Steam")
    pk = str(created.mod_id)
    db.update_mod_user_metadata(pk, {"display_name": "  "})
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.display_name == "Only Steam"
    assert info.user_display_name == ""


def test_batch_upsert_preserves_user_fields(db: DatabaseManager) -> None:
    created = create_steam_test_mod(db, external_id="666", title="A")
    pk = str(created.mod_id)
    db.update_mod_user_metadata(pk, {"display_name": "Nick", "user_notes": "n"})
    db.upsert_mods(
        [ModMetadata(published_file_id="666", title="B", description="d2")]
    )
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.steam_name == "B"
    assert info.display_name == "Nick"
    assert info.user_notes == "n"
