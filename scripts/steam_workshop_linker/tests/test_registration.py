from __future__ import annotations

from pathlib import Path

from database import lookup_steam_mod, open_readonly
from helpers import make_db


def test_registered_one_row(tmp_path: Path) -> None:
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {
                "app_id": 262060,
                "platform": "steam",
                "workspace_id": "111",
                "internal_id": "aaaa-1111",
                "last_known_path": str(tmp_path / "ModA"),
            }
        ],
    )
    conn = open_readonly(db)
    try:
        rows = lookup_steam_mod(conn, app_id="262060", workspace_id="111")
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0].internal_id == "aaaa-1111"
    assert rows[0].mod_id >= 1


def test_unregistered_zero_rows(tmp_path: Path) -> None:
    db = make_db(tmp_path / "mod_manager.db", [])
    conn = open_readonly(db)
    try:
        rows = lookup_steam_mod(conn, app_id="262060", workspace_id="999")
    finally:
        conn.close()
    assert rows == []


def test_ambiguous_duplicate_workspace_id(tmp_path: Path) -> None:
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {"app_id": 262060, "workspace_id": "111", "internal_id": "a"},
            {"app_id": 262060, "workspace_id": "111", "internal_id": "b"},
        ],
    )
    conn = open_readonly(db)
    try:
        rows = lookup_steam_mod(conn, app_id="262060", workspace_id="111")
    finally:
        conn.close()
    assert len(rows) == 2


def test_nexus_same_workspace_id_does_not_match_steam(tmp_path: Path) -> None:
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {
                "app_id": 262060,
                "platform": "nexus",
                "workspace_id": "111",
                "internal_id": "nexus-111",
            }
        ],
    )
    conn = open_readonly(db)
    try:
        rows = lookup_steam_mod(conn, app_id="262060", workspace_id="111")
    finally:
        conn.close()
    assert rows == []


def test_readonly_open_does_not_create_wal(tmp_path: Path) -> None:
    db = make_db(tmp_path / "mod_manager.db", [{"workspace_id": "1"}])
    before = {p.name for p in tmp_path.iterdir()}
    conn = open_readonly(db)
    conn.execute("SELECT COUNT(*) FROM mods").fetchone()
    conn.close()
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before
    assert not (tmp_path / "mod_manager.db-wal").exists()
    assert not (tmp_path / "mod_manager.db-shm").exists()
