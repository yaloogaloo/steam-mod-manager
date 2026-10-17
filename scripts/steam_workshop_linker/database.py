"""Read-only SQLite access to the SMM production ``mods`` table.

Never imports ``core.db_manager``. Never writes. Opens with URI ``mode=ro``
so WAL/SHM files are not created or modified.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


class DatabaseError(RuntimeError):
    """Cannot open or query the production database read-only."""


@dataclass(frozen=True)
class ModRow:
    mod_id: int
    internal_id: str
    workspace_id: str
    app_id: int
    platform: str
    last_known_path: str
    folder_present: int


_LOOKUP_SQL = """
SELECT
    mod_id,
    internal_id,
    workspace_id,
    app_id,
    platform,
    last_known_path,
    folder_present
FROM mods
WHERE lower(trim(platform)) = 'steam'
  AND app_id = ?
  AND trim(workspace_id) = ?
"""

_LIST_SQL = """
SELECT DISTINCT trim(workspace_id) AS workspace_id
FROM mods
WHERE lower(trim(platform)) = 'steam'
  AND app_id = ?
  AND trim(workspace_id) != ''
"""


def parse_app_id(value: str | int) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return int(text)


def readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise DatabaseError(f"database not found: {path}")
    uri = readonly_uri(path)
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open database read-only: {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseError(f"cannot set query_only: {exc}") from exc
    return conn


def _row_from_sqlite(raw: sqlite3.Row) -> ModRow:
    return ModRow(
        mod_id=int(raw["mod_id"]),
        internal_id=str(raw["internal_id"] or "").strip(),
        workspace_id=str(raw["workspace_id"] or "").strip(),
        app_id=int(raw["app_id"] or 0),
        platform=str(raw["platform"] or "").strip(),
        last_known_path=str(raw["last_known_path"] or "").strip(),
        folder_present=int(raw["folder_present"] or 0),
    )


def lookup_steam_mod(
    conn: sqlite3.Connection,
    *,
    app_id: str | int,
    workspace_id: str,
) -> list[ModRow]:
    parsed = parse_app_id(app_id)
    if parsed is None:
        return []
    wid = str(workspace_id or "").strip()
    if not wid:
        return []
    cur = conn.execute(_LOOKUP_SQL, (parsed, wid))
    return [_row_from_sqlite(row) for row in cur.fetchall()]


def list_steam_workspace_ids(conn: sqlite3.Connection, app_id: str | int) -> list[str]:
    parsed = parse_app_id(app_id)
    if parsed is None:
        return []
    cur = conn.execute(_LIST_SQL, (parsed,))
    out: list[str] = []
    for row in cur.fetchall():
        wid = str(row["workspace_id"] or "").strip()
        if wid:
            out.append(wid)
    return out


def default_database_path() -> Path:
    """``<project_root>/data/mod_manager.db`` — no SMM runtime import."""
    return Path(__file__).resolve().parents[2] / "data" / "mod_manager.db"
