"""Mod lifecycle — version columns, migration, enable baseline."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, ModVersionInfo
from core.models import ModMetadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "lifecycle.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_version_migration(tmp_path: Path) -> None:
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
        INSERT INTO mods VALUES (77, 0, 'Old', '', '', '2020-01-01T00:00:00+00:00');
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
    for name in (
        "mod_version",
        "installed_version",
        "version_source",
        "version_checked_at",
        "enabled",
    ):
        assert name in cols
    ver = manager.get_mod_version(77)
    assert ver.mod_version == ""
    assert ver.installed_version == ""
    assert manager.is_mod_enabled(77) is True
    manager.close()
    DatabaseManager.reset_instance()


def test_version_storage_and_update_flag(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="101", title="Auto Pickup"))
    st = db.update_mod_version(
        101,
        mod_version="1.2.0",
        installed_version="1.1.0",
        version_source="nexus",
        touch_checked_at=True,
    )
    assert st.mod_version == "1.2.0"
    assert st.installed_version == "1.1.0"
    assert st.has_update is True
    assert st.status_label == "Update Available"
    assert st.version_checked_at

    # Updating latest must not wipe installed unless explicitly passed
    st2 = db.update_mod_version(101, mod_version="1.3.0", touch_checked_at=True)
    assert st2.mod_version == "1.3.0"
    assert st2.installed_version == "1.1.0"

    st3 = db.update_mod_version(101, installed_version="1.3.0")
    assert st3.has_update is False
    assert st3.status_label == "Up to date"


def test_get_mod_version_dict(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="102", title="X"))
    db.update_mod_version(102, mod_version="2.0", installed_version="2.0")
    d = db.get_mod_version(102).to_dict()
    assert d["mod_id"] == "102"
    assert d["has_update"] is False
    assert d["status"] == "Up to date"
