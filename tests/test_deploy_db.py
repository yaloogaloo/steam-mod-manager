"""Phase 1: games/mods deploy columns — migration + API."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_phase1.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _legacy_schema_sql() -> str:
    """Pre-deploy schema (no install/mod paths, no deploy_* on mods)."""
    return """
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
            display_name TEXT NOT NULL DEFAULT '',
            custom_description TEXT NOT NULL DEFAULT '',
            user_notes TEXT NOT NULL DEFAULT '',
            favorite INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (app_id) REFERENCES games(app_id)
        );
        INSERT INTO games (app_id, name, header_url, description, updated_at)
        VALUES (0, '', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO games (app_id, name, header_url, description, updated_at)
        VALUES (1623730, 'Palworld', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO mods (
            mod_id, app_id, title, preview_url, description,
            display_name, custom_description, user_notes, favorite, updated_at
        )
        VALUES (
            999001, 1623730, 'Legacy Mod', '', '',
            '', '', '', 0, '2020-01-01T00:00:00+00:00'
        );
    """


def test_fresh_db_has_deploy_columns(db: DatabaseManager) -> None:
    game_cols = {
        str(r[1]) for r in db._conn.execute("PRAGMA table_info(games)").fetchall()
    }
    mod_cols = {
        str(r[1]) for r in db._conn.execute("PRAGMA table_info(mods)").fetchall()
    }
    assert {"install_path", "mod_path", "deploy_type", "workshop_path"} <= game_cols
    assert {"deploy_status", "deploy_time", "deploy_path"} <= mod_cols


def test_migration_adds_deploy_columns_to_legacy_db(tmp_path: Path) -> None:
    path = tmp_path / "legacy_deploy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_legacy_schema_sql())
    conn.commit()
    conn.close()

    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)

    game_cols = {
        str(r[1])
        for r in manager._conn.execute("PRAGMA table_info(games)").fetchall()
    }
    mod_cols = {
        str(r[1])
        for r in manager._conn.execute("PRAGMA table_info(mods)").fetchall()
    }
    assert "install_path" in game_cols
    assert "mod_path" in game_cols
    assert "deploy_type" in game_cols
    assert "workshop_path" in game_cols
    assert "deploy_status" in mod_cols
    assert "deploy_time" in mod_cols
    assert "deploy_path" in mod_cols

    # Existing rows keep data; new columns get defaults
    cfg = manager.get_game_deploy_config(1623730)
    assert cfg is not None
    assert cfg.name == "Palworld"
    assert cfg.install_path == ""
    assert cfg.mod_path == ""
    assert cfg.deploy_type == DEPLOY_TYPE_FOLDER_COPY

    info = manager.get_mod_deploy_info(999001)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    assert info.deploy_path == ""
    assert info.app_id == 1623730

    manager.close()
    DatabaseManager.reset_instance()


def test_update_and_get_game_deploy_config(db: DatabaseManager) -> None:
    saved = db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=r"D:/SteamLibrary/steamapps/common/Palworld",
        mod_path=r"D:/SteamLibrary/steamapps/common/Palworld/Pal/Binaries/Win64/Mods",
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    assert saved.app_id == 1623730
    assert saved.install_path.endswith("Palworld")
    assert saved.mod_path.endswith("Mods")

    loaded = db.get_game_deploy_config(1623730)
    assert loaded == saved

    # Partial update leaves other fields
    patched = db.update_game_deploy_config(
        1623730,
        mod_path=r"E:/Other/Mods",
    )
    assert patched.install_path == saved.install_path
    assert patched.mod_path == r"E:/Other/Mods"
    assert patched.name == "Palworld"


def test_steam_upsert_game_preserves_deploy_paths(db: DatabaseManager) -> None:
    db.update_game_deploy_config(
        100,
        name="OldName",
        install_path="/game",
        mod_path="/game/mods",
    )
    db.upsert_game(
        GameInfo(app_id=100, name="New Steam Name", header_image="h", short_description="d")
    )
    cfg = db.get_game_deploy_config(100)
    assert cfg is not None
    assert cfg.name == "New Steam Name"  # Steam name still updates
    assert cfg.install_path == "/game"
    assert cfg.mod_path == "/game/mods"


def test_update_and_get_mod_deploy_status(db: DatabaseManager) -> None:
    db.update_game_deploy_config(50, name="Game", install_path="/g", mod_path="/g/m")
    db.upsert_mod(
        ModMetadata(published_file_id="5001", title="Mod A", app_id=50)
    )

    before = db.get_mod_deploy_info(5001)
    assert before is not None
    assert before.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED

    after = db.update_mod_deploy_status(
        5001,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=r"/g/m/ModA",
    )
    assert after.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert after.deploy_path == r"/g/m/ModA"
    assert after.deploy_time

    again = db.get_mod_deploy_info(5001)
    assert again == after


def test_steam_upsert_mod_preserves_deploy_status(db: DatabaseManager) -> None:
    db.update_game_deploy_config(77, name="G")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="T1", app_id=77))
    db.update_mod_deploy_status(
        7001,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="/mods/T1",
        deploy_time="2024-01-01T00:00:00+00:00",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="7001",
            title="T1 Updated",
            description="new",
            app_id=77,
        )
    )
    info = db.get_mod_deploy_info(7001)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_path == "/mods/T1"
    assert info.deploy_time == "2024-01-01T00:00:00+00:00"
    meta = db.get_mod(7001)
    assert meta is not None
    assert meta.title == "T1 Updated"


def test_reject_deploy_config_for_placeholder_app(db: DatabaseManager) -> None:
    with pytest.raises(ValueError):
        db.update_game_deploy_config(0, install_path="/x")
