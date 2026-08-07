"""Mod lifecycle status columns + API."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    ModStatus,
)
from core.models import ModMetadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "mod_status.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_migration_adds_status_columns(tmp_path: Path) -> None:
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
        INSERT INTO mods VALUES (55, 0, 'Old', '', '', '2020-01-01T00:00:00+00:00');
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
    assert "is_invalid" in cols
    assert "invalid_reason" in cols
    assert "conflict_status" in cols
    assert "conflict_note" in cols
    assert "last_check_time" in cols
    st = manager.get_mod_status(55)
    assert st.invalid is False
    assert st.conflict_status == CONFLICT_STATUS_NONE
    manager.close()
    DatabaseManager.reset_instance()


def test_mark_invalid_and_restore(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="100", title="X"))
    st = db.update_mod_status(
        100, invalid=True, invalid_reason="作者已删除", touch_check_time=True
    )
    assert st.invalid is True
    assert st.invalid_reason == "作者已删除"
    assert st.last_check_time

    st2 = db.update_mod_status(100, invalid=False)
    assert st2.invalid is False
    assert st2.invalid_reason == ""
    assert db.get_mod_status(100).to_dict()["invalid"] is False


def test_conflict_status_persist(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="200", title="Y"))
    st = db.update_mod_status(
        200,
        conflict_status=CONFLICT_STATUS_CONFLICT,
        conflict_note="与BetterGraphics冲突",
        touch_check_time=True,
    )
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT
    assert "BetterGraphics" in st.conflict_note

    cleared = db.update_mod_status(200, conflict_status=CONFLICT_STATUS_NONE)
    assert cleared.conflict_status == CONFLICT_STATUS_NONE
    assert cleared.conflict_note == ""


def test_mod_status_model_roundtrip() -> None:
    raw = {
        "invalid": True,
        "invalid_reason": "游戏版本不兼容",
        "conflict_status": "warning",
        "conflict_note": "maybe",
        "last_check_time": "2026-01-01T00:00:00+00:00",
    }
    st = ModStatus.from_dict(raw)
    assert st.run_label == "失效"
    assert st.to_dict()["conflict_status"] == "warning"
    assert ModStatus.from_dict(st.to_dict()).invalid_reason == "游戏版本不兼容"
