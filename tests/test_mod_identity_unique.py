"""Mod identity uniqueness: (platform, external_id) UNIQUE + register guards."""

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
from core.game_info import GameInfo
from core.models import ModMetadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "identity.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_unique_index_created(db: DatabaseManager) -> None:
    rows = db._conn.execute("PRAGMA index_list(mods)").fetchall()
    names = {str(r["name"]) for r in rows}
    assert "uq_mods_platform_external" in names
    info = db._conn.execute(
        "PRAGMA index_info(uq_mods_platform_external)"
    ).fetchall()
    cols = [str(r["name"]) for r in info]
    assert cols == ["platform", "external_id"]


def test_same_nexus_id_twice_reuses_row(db: DatabaseManager) -> None:
    first = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Pal Analyzer",
        app_id=1623730,
        game_name="Palworld",
    )
    second = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Pal Analyzer v2",
        app_id=1623730,
        game_name="Palworld",
    )
    assert first.mod_id == second.mod_id
    assert second.steam_name == "Pal Analyzer v2"
    count = db._conn.execute(
        "SELECT COUNT(*) AS c FROM mods WHERE platform=? AND external_id=?",
        (PLATFORM_NEXUS, "336"),
    ).fetchone()["c"]
    assert int(count) == 1


def test_concurrent_double_insert_recovers(db: DatabaseManager) -> None:
    """Simulate lost race: two allocated ids, second identity write hits UNIQUE."""
    a = db.allocate_mod_id()
    db.update_mod_platform_info(
        a,
        platform=PLATFORM_NEXUS,
        external_id="998",
        title="PlaceholderA",
        app_id=1623730,
    )
    b = db.allocate_mod_id()
    assert b == a + 1

    db.update_mod_platform_info(
        a,
        platform=PLATFORM_NEXUS,
        external_id="999",
        title="First",
        app_id=1623730,
    )
    with pytest.raises(ValueError, match="already exists"):
        db.update_mod_platform_info(
            b,
            platform=PLATFORM_NEXUS,
            external_id="999",
            title="Second",
            app_id=1623730,
        )

    # register_external_mod recovers via find + Integrity/ValueError path
    calls = {"n": 0}
    real_find = db.find_mod_by_external

    def flaky_find(platform: str, external_id: str):
        calls["n"] += 1
        # First lookup (pre-insert) pretends missing; later lookups succeed.
        if calls["n"] == 1:
            return None
        return real_find(platform, external_id)

    db.find_mod_by_external = flaky_find  # type: ignore[method-assign]
    try:
        # Force a fresh allocate path while identity already exists under `a`
        result = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="999",
            title="Racer",
            app_id=1623730,
            game_name="Palworld",
            mod_id=b,
        )
    finally:
        db.find_mod_by_external = real_find  # type: ignore[method-assign]

    assert result.mod_id == str(a)
    count = db._conn.execute(
        "SELECT COUNT(*) AS c FROM mods WHERE platform=? AND external_id=?",
        (PLATFORM_NEXUS, "999"),
    ).fetchone()["c"]
    assert int(count) == 1


def test_same_external_id_different_platforms(db: DatabaseManager) -> None:
    nexus = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="42",
        title="Nexus 42",
        app_id=1623730,
        game_name="Palworld",
    )
    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="42",
        source_url="https://github.com/org/42",
        title="GitHub 42",
        app_id=1623730,
        game_name="Palworld",
    )
    assert nexus.mod_id != github.mod_id
    assert nexus.platform == PLATFORM_NEXUS
    assert github.platform == PLATFORM_GITHUB

    steam = db.register_external_mod(
        platform=PLATFORM_STEAM,
        external_id="42",
        title="Steam 42",
        app_id=1623730,
    )
    assert steam.mod_id == "42"
    assert steam.platform == PLATFORM_STEAM


def test_forbid_non_steam_low_mod_id(db: DatabaseManager) -> None:
    with pytest.raises(ValueError, match="NON_STEAM_MOD_ID_BASE"):
        db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="77",
            title="Bad",
            app_id=1623730,
            game_name="Palworld",
            mod_id=12345,
        )
    # Explicit high id still allowed
    ok = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="78",
        title="Ok",
        app_id=1623730,
        game_name="Palworld",
        mod_id=NON_STEAM_MOD_ID_BASE + 50,
    )
    assert int(ok.mod_id) == NON_STEAM_MOD_ID_BASE + 50


def test_update_platform_identity_conflict(db: DatabaseManager) -> None:
    a = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="1",
        title="A",
        app_id=1623730,
        game_name="Palworld",
    )
    b = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="2",
        title="B",
        app_id=1623730,
        game_name="Palworld",
    )
    with pytest.raises(ValueError, match="already exists"):
        db.update_mod_platform_info(
            b.mod_id,
            platform=PLATFORM_NEXUS,
            external_id="1",
        )
    # Unchanged identity still ok
    again = db.update_mod_platform_info(
        a.mod_id,
        platform=PLATFORM_NEXUS,
        external_id="1",
        title="A2",
    )
    assert again.steam_name == "A2"


def test_duplicate_warning_skips_unique_index(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import sqlite3

    path = tmp_path / "dup.db"
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
        INSERT INTO games(app_id, name, header_url, description, updated_at)
        VALUES (0, '', '', '', 't');
        CREATE TABLE mods (
            mod_id INTEGER PRIMARY KEY,
            app_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT 'steam',
            source_url TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            mod_files TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        INSERT INTO mods(mod_id, app_id, title, platform, external_id, updated_at)
        VALUES
          (9000000000000000, 0, 'A', 'nexus', 'dup', 't'),
          (9000000000000001, 0, 'B', 'nexus', 'dup', 't');
        """
    )
    conn.commit()
    conn.close()

    DatabaseManager.reset_instance()
    with caplog.at_level(logging.WARNING, logger="core.db_manager"):
        manager = DatabaseManager(path)
    assert any("Duplicate mod identity" in r.message for r in caplog.records)
    names = {
        str(r["name"])
        for r in manager._conn.execute("PRAGMA index_list(mods)").fetchall()
    }
    assert "uq_mods_platform_external" not in names
    manager.close()
    DatabaseManager.reset_instance()
