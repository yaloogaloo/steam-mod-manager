"""SQLite snapshot layer for Steam game / Mod metadata."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .game_info import GameInfo
from .models import ModMetadata
from .mod_platform import (
    DEFAULT_MOD_FILES_JSON,
    NON_STEAM_MOD_ID_BASE,
    OFFLINE_STATUS_NONE,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    SUPPORTED_PLATFORMS,
    ModFileEntry,
    ModFilesBundle,
    normalize_offline_status,
    normalize_platform,
    steam_workshop_url,
)
from .mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
    ModStatus,
    normalize_conflict_status,
)
from .paths import database_path
from .sanitize import sanitize_folder_name

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    app_id      INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    header_url  TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    install_path TEXT NOT NULL DEFAULT '',
    mod_path    TEXT NOT NULL DEFAULT '',
    deploy_type TEXT NOT NULL DEFAULT 'folder_copy',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mods (
    mod_id      INTEGER PRIMARY KEY,
    app_id      INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL DEFAULT '',
    preview_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    custom_description TEXT NOT NULL DEFAULT '',
    user_notes TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    deploy_status TEXT NOT NULL DEFAULT 'not_deployed',
    deploy_time TEXT NOT NULL DEFAULT '',
    deploy_path TEXT NOT NULL DEFAULT '',
    deploy_error TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'steam',
    source_url TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    mod_files TEXT NOT NULL DEFAULT '{}',
    is_invalid INTEGER NOT NULL DEFAULT 0,
    invalid_reason TEXT NOT NULL DEFAULT '',
    conflict_status TEXT NOT NULL DEFAULT 'none',
    conflict_note TEXT NOT NULL DEFAULT '',
    last_check_time TEXT NOT NULL DEFAULT '',
    mod_version TEXT NOT NULL DEFAULT '',
    installed_version TEXT NOT NULL DEFAULT '',
    version_source TEXT NOT NULL DEFAULT '',
    version_checked_at TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    offline_status TEXT NOT NULL DEFAULT 'none',
    offline_provider TEXT NOT NULL DEFAULT '',
    offline_updated_at TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES games(app_id)
);

CREATE INDEX IF NOT EXISTS idx_mods_app_id ON mods(app_id);

CREATE TABLE IF NOT EXISTS mod_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id      INTEGER NOT NULL,
    tag_type    TEXT NOT NULL,
    tag_value   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mod_tags_mod_id ON mod_tags(mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_tags_type ON mod_tags(tag_type);

CREATE TABLE IF NOT EXISTS mod_relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_mod_id   INTEGER NOT NULL,
    target_mod_id   INTEGER NOT NULL,
    relation_type   TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mod_relations_source ON mod_relations(source_mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_relations_target ON mod_relations(target_mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_relations_type ON mod_relations(relation_type);
"""

# Columns added after the initial schema — applied on every startup.
_GAMES_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("install_path", "TEXT NOT NULL DEFAULT ''"),
    ("mod_path", "TEXT NOT NULL DEFAULT ''"),
    ("deploy_type", "TEXT NOT NULL DEFAULT 'folder_copy'"),
)

_MODS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("display_name", "TEXT NOT NULL DEFAULT ''"),
    ("custom_description", "TEXT NOT NULL DEFAULT ''"),
    ("user_notes", "TEXT NOT NULL DEFAULT ''"),
    ("favorite", "INTEGER NOT NULL DEFAULT 0"),
    ("deploy_status", "TEXT NOT NULL DEFAULT 'not_deployed'"),
    ("deploy_time", "TEXT NOT NULL DEFAULT ''"),
    ("deploy_path", "TEXT NOT NULL DEFAULT ''"),
    ("deploy_error", "TEXT NOT NULL DEFAULT ''"),
    ("platform", "TEXT NOT NULL DEFAULT 'steam'"),
    ("source_url", "TEXT NOT NULL DEFAULT ''"),
    ("external_id", "TEXT NOT NULL DEFAULT ''"),
    ("mod_files", "TEXT NOT NULL DEFAULT '{}'"),
    ("is_invalid", "INTEGER NOT NULL DEFAULT 0"),
    ("invalid_reason", "TEXT NOT NULL DEFAULT ''"),
    ("conflict_status", "TEXT NOT NULL DEFAULT 'none'"),
    ("conflict_note", "TEXT NOT NULL DEFAULT ''"),
    ("last_check_time", "TEXT NOT NULL DEFAULT ''"),
    ("mod_version", "TEXT NOT NULL DEFAULT ''"),
    ("installed_version", "TEXT NOT NULL DEFAULT ''"),
    ("version_source", "TEXT NOT NULL DEFAULT ''"),
    ("version_checked_at", "TEXT NOT NULL DEFAULT ''"),
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("offline_status", "TEXT NOT NULL DEFAULT 'none'"),
    ("offline_provider", "TEXT NOT NULL DEFAULT ''"),
    ("offline_updated_at", "TEXT NOT NULL DEFAULT ''"),
)

DEPLOY_STATUS_NOT_DEPLOYED = "not_deployed"
DEPLOY_STATUS_DEPLOYED = "deployed"
DEPLOY_STATUS_FAILED = "failed"
DEPLOY_TYPE_FOLDER_COPY = "folder_copy"
DEPLOY_TYPE_PALWORLD_PAK = "palworld_pak"
SUPPORTED_DEPLOY_TYPES = (DEPLOY_TYPE_FOLDER_COPY, DEPLOY_TYPE_PALWORLD_PAK)

# User tags / conflict relations (SQLite only — never written to .info)
TAG_TYPE_INVALID = "invalid"
TAG_TYPE_CONFLICT = "conflict"
TAG_TYPE_CATEGORY = "category"
RELATION_TYPE_CONFLICT = "conflict"

_MOD_SELECT_COLS = (
    "mod_id, app_id, title, preview_url, description, "
    "display_name, custom_description, user_notes, favorite, "
    "platform, source_url, external_id, mod_files, "
    "is_invalid, invalid_reason, conflict_status, conflict_note, last_check_time, "
    "mod_version, installed_version, version_source, version_checked_at, "
    "enabled, "
    "offline_status, offline_provider, offline_updated_at, "
    "updated_at"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ModVersionInfo:
    """Author / installed version snapshot for one Mod (SQLite only)."""

    mod_id: str = ""
    mod_version: str = ""
    installed_version: str = ""
    version_source: str = ""
    version_checked_at: str = ""

    @property
    def has_update(self) -> bool:
        latest = (self.mod_version or "").strip()
        installed = (self.installed_version or "").strip()
        if not latest or not installed:
            return False
        return latest != installed

    @property
    def status_label(self) -> str:
        if self.has_update:
            return "Update Available"
        if (self.mod_version or "").strip() or (self.installed_version or "").strip():
            return "Up to date"
        return "Unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "mod_version": self.mod_version or "",
            "installed_version": self.installed_version or "",
            "version_source": self.version_source or "",
            "version_checked_at": self.version_checked_at or "",
            "has_update": self.has_update,
            "status": self.status_label,
        }


@dataclass(frozen=True)
class ModDisplayInfo:
    """Steam + user-facing Mod fields for UI."""

    mod_id: str
    steam_name: str
    steam_description: str
    preview_url: str
    display_name: str  # resolved: user override or steam_name
    custom_description: str
    user_notes: str
    favorite: bool
    user_display_name: str = ""  # raw override (may be empty)
    app_id: int = 0
    platform: str = PLATFORM_STEAM
    source_url: str = ""
    external_id: str = ""
    mod_files_json: str = DEFAULT_MOD_FILES_JSON
    is_invalid: bool = False
    invalid_reason: str = ""
    conflict_status: str = CONFLICT_STATUS_NONE
    conflict_note: str = ""
    last_check_time: str = ""
    mod_version: str = ""
    installed_version: str = ""
    version_source: str = ""
    version_checked_at: str = ""
    enabled: bool = True
    offline_status: str = OFFLINE_STATUS_NONE
    offline_provider: str = ""
    offline_updated_at: str = ""

    @property
    def mod_files(self) -> ModFilesBundle:
        return ModFilesBundle.from_json(self.mod_files_json)

    @property
    def status(self) -> ModStatus:
        return ModStatus(
            invalid=self.is_invalid,
            invalid_reason=self.invalid_reason,
            conflict_status=self.conflict_status,
            conflict_note=self.conflict_note,
            last_check_time=self.last_check_time,
        )

    @property
    def version_info(self) -> ModVersionInfo:
        return ModVersionInfo(
            mod_id=self.mod_id,
            mod_version=self.mod_version,
            installed_version=self.installed_version,
            version_source=self.version_source,
            version_checked_at=self.version_checked_at,
        )


@dataclass(frozen=True)
class ModSearchFields:
    """Read-only fields used by Mod Library search / status filters."""

    mod_id: str
    steam_name: str
    display_name: str  # resolved: user override or steam title
    user_notes: str
    favorite: bool
    deploy_status: str = DEPLOY_STATUS_NOT_DEPLOYED
    game_name: str = ""
    platform: str = PLATFORM_STEAM
    source_url: str = ""
    external_id: str = ""
    is_invalid: bool = False
    conflict_status: str = CONFLICT_STATUS_NONE
    enabled: bool = True
    category_tags: str = ""


@dataclass(frozen=True)
class GameDeployConfig:
    """Per-game deploy paths (SQLite ``games`` row)."""

    app_id: int
    install_path: str = ""
    mod_path: str = ""
    deploy_type: str = DEPLOY_TYPE_FOLDER_COPY
    name: str = ""


@dataclass(frozen=True)
class ModDeployInfo:
    """Per-mod deploy status recorded after a successful deploy."""

    mod_id: str
    deploy_status: str = DEPLOY_STATUS_NOT_DEPLOYED
    deploy_time: str = ""
    deploy_path: str = ""
    app_id: int = 0
    deploy_error: str = ""


@dataclass(frozen=True)
class ModTag:
    """One user tag row from ``mod_tags``."""

    id: int
    mod_id: str
    tag_type: str
    tag_value: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ModRelation:
    """One relation row from ``mod_relations`` (e.g. conflict pair)."""

    id: int
    source_mod_id: str
    target_mod_id: str
    relation_type: str
    note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ModTagFlags:
    """Compact tag summary for library cards / filters."""

    invalid: bool = False
    conflict: bool = False
    invalid_reason: str = ""
    tag_values: tuple[str, ...] = ()


class DatabaseManager:
    """
    Thread-safe SQLite access for permanent AppID / ModID snapshots.

    Steam IDs are stable — once stored, rows are reused indefinitely unless
    an explicit upsert refreshes them.

    User-editable fields (display_name / custom_description / user_notes /
    favorite) live only in SQLite and are never overwritten by Steam sync.
    """

    _instance: DatabaseManager | None = None
    _instance_lock = threading.Lock()

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else database_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, db_path: str | Path | None = None) -> DatabaseManager:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(db_path=db_path)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Close and drop the process-wide singleton (tests / shutdown)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_games_table()
            self._migrate_mods_table()
            self._backfill_steam_platform_fields()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mods_platform ON mods(platform)"
            )
            self._ensure_unique_platform_external_index()
            # Allow mods.app_id = 0 (unknown) under FOREIGN KEY to games.
            self._conn.execute(
                """
                INSERT OR IGNORE INTO games
                    (app_id, name, header_url, description, updated_at)
                VALUES (0, '', '', '', ?)
                """,
                (_utc_now(),),
            )
            self._conn.commit()

    def _migrate_games_table(self) -> None:
        """Add deploy-config columns to existing databases (idempotent)."""
        existing = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(games)").fetchall()
        }
        for name, decl in _GAMES_MIGRATIONS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE games ADD COLUMN {name} {decl}")

    def _migrate_mods_table(self) -> None:
        """Add user-metadata / deploy / platform columns (idempotent)."""
        existing = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        for name, decl in _MODS_MIGRATIONS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE mods ADD COLUMN {name} {decl}")

    def _backfill_steam_platform_fields(self) -> None:
        """
        Existing Workshop rows: platform=steam, external_id=Workshop ID, source_url.
        Does not overwrite non-empty custom source_url / external_id for other platforms.
        """
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "platform" not in cols:
            return
        self._conn.execute(
            """
            UPDATE mods SET
                platform = CASE
                    WHEN platform IS NULL OR TRIM(platform) = '' THEN 'steam'
                    ELSE platform
                END,
                external_id = CASE
                    WHEN (external_id IS NULL OR TRIM(external_id) = '')
                         AND (platform IS NULL OR TRIM(platform) = ''
                              OR platform = 'steam')
                    THEN CAST(mod_id AS TEXT)
                    ELSE external_id
                END,
                source_url = CASE
                    WHEN (source_url IS NULL OR TRIM(source_url) = '')
                         AND (platform IS NULL OR TRIM(platform) = ''
                              OR platform = 'steam')
                    THEN 'https://steamcommunity.com/sharedfiles/filedetails/?id='
                         || CAST(mod_id AS TEXT)
                    ELSE source_url
                END,
                mod_files = CASE
                    WHEN mod_files IS NULL OR TRIM(mod_files) = '' THEN '{}'
                    ELSE mod_files
                END
            """
        )

    def _ensure_unique_platform_external_index(self) -> None:
        """
        Enforce UNIQUE(platform, external_id) when the database is clean.

        Duplicate rows are never auto-deleted — only warned. The unique index
        is skipped until duplicates are resolved manually.
        """
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "platform" not in cols or "external_id" not in cols:
            return

        dup_rows = self._conn.execute(
            """
            SELECT platform, external_id, COUNT(*) AS cnt
            FROM mods
            GROUP BY platform, external_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        if dup_rows:
            for row in dup_rows:
                logger.warning(
                    "Duplicate mod identity platform=%r external_id=%r count=%s "
                    "(UNIQUE index not applied; resolve manually)",
                    row["platform"],
                    row["external_id"],
                    row["cnt"],
                )
            return

        # Replace the old non-unique helper index with a UNIQUE constraint index.
        self._conn.execute("DROP INDEX IF EXISTS idx_mods_external")
        try:
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_mods_platform_external
                ON mods(platform, external_id)
                """
            )
        except sqlite3.IntegrityError as exc:
            logger.warning(
                "Failed to create uq_mods_platform_external (duplicates remain): %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Games
    # ------------------------------------------------------------------

    def get_game(self, app_id: int) -> GameInfo | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT app_id, name, header_url, description FROM games WHERE app_id = ?",
                (int(app_id),),
            ).fetchone()
        if row is None:
            return None
        return _game_from_row(row)

    def upsert_game(self, info: GameInfo) -> None:
        if not info.app_id or not info.name:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO games (app_id, name, header_url, description, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    name = excluded.name,
                    header_url = excluded.header_url,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (
                    int(info.app_id),
                    info.name,
                    info.header_image or "",
                    info.short_description or "",
                    _utc_now(),
                ),
            )
            self._conn.commit()

    def list_games(self) -> list[GameInfo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT app_id, name, header_url, description FROM games ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [_game_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Games — deploy configuration (never overwritten by Steam upsert)
    # ------------------------------------------------------------------

    def get_game_deploy_config(self, game_id: int | str) -> GameDeployConfig | None:
        """Return deploy paths for ``app_id`` (``game_id``), or None if missing."""
        app_id = int(game_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT app_id, name, install_path, mod_path, deploy_type
                FROM games WHERE app_id = ?
                """,
                (app_id,),
            ).fetchone()
        if row is None:
            return None
        return _game_deploy_from_row(row)

    def update_game_deploy_config(
        self,
        game_id: int | str,
        *,
        install_path: str | None = None,
        mod_path: str | None = None,
        deploy_type: str | None = None,
        name: str | None = None,
    ) -> GameDeployConfig:
        """
        Persist game-level deploy paths.

        Creates a games row when missing. ``None`` kwargs leave existing
        values unchanged (new rows use empty / default). Steam ``upsert_game``
        never touches these columns.
        """
        app_id = int(game_id)
        if app_id == 0:
            raise ValueError("Cannot store deploy config for placeholder app_id=0")

        with self._lock:
            row = self._conn.execute(
                """
                SELECT app_id, name, install_path, mod_path, deploy_type
                FROM games WHERE app_id = ?
                """,
                (app_id,),
            ).fetchone()

            if row is None:
                new_install = "" if install_path is None else str(install_path).strip()
                new_mod = "" if mod_path is None else str(mod_path).strip()
                new_type = (
                    DEPLOY_TYPE_FOLDER_COPY
                    if deploy_type is None
                    else (str(deploy_type).strip() or DEPLOY_TYPE_FOLDER_COPY)
                )
                new_name = "" if name is None else str(name).strip()
                self._conn.execute(
                    """
                    INSERT INTO games (
                        app_id, name, header_url, description,
                        install_path, mod_path, deploy_type, updated_at
                    )
                    VALUES (?, ?, '', '', ?, ?, ?, ?)
                    """,
                    (app_id, new_name, new_install, new_mod, new_type, _utc_now()),
                )
            else:
                new_install = (
                    str(row["install_path"] or "")
                    if install_path is None
                    else str(install_path).strip()
                )
                new_mod = (
                    str(row["mod_path"] or "")
                    if mod_path is None
                    else str(mod_path).strip()
                )
                new_type = (
                    str(row["deploy_type"] or DEPLOY_TYPE_FOLDER_COPY)
                    if deploy_type is None
                    else (str(deploy_type).strip() or DEPLOY_TYPE_FOLDER_COPY)
                )
                new_name = (
                    str(row["name"] or "") if name is None else str(name).strip()
                )
                self._conn.execute(
                    """
                    UPDATE games SET
                        name = ?,
                        install_path = ?,
                        mod_path = ?,
                        deploy_type = ?,
                        updated_at = ?
                    WHERE app_id = ?
                    """,
                    (new_name, new_install, new_mod, new_type, _utc_now(), app_id),
                )
            self._conn.commit()
            out = self._conn.execute(
                """
                SELECT app_id, name, install_path, mod_path, deploy_type
                FROM games WHERE app_id = ?
                """,
                (app_id,),
            ).fetchone()
        assert out is not None
        return _game_deploy_from_row(out)

    # ------------------------------------------------------------------
    # Mods — Steam snapshot (read / upsert Steam fields only)
    # ------------------------------------------------------------------

    def get_mod(self, mod_id: int | str) -> ModMetadata | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (int(mod_id),),
            ).fetchone()
        if row is None:
            return None
        return _mod_from_row(row)

    def get_mods_by_ids(self, mod_ids: Iterable[int | str]) -> dict[str, ModMetadata]:
        ids = [int(i) for i in mod_ids if str(i).strip().isdigit()]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {_MOD_SELECT_COLS}
                FROM mods WHERE mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {str(row["mod_id"]): _mod_from_row(row) for row in rows}

    def upsert_mod(self, meta: ModMetadata) -> None:
        """Upsert Steam fields only — never overwrite user metadata columns."""
        if not meta.published_file_id or not str(meta.published_file_id).isdigit():
            return
        if not meta.title:
            return
        mid = int(meta.published_file_id)
        source = steam_workshop_url(mid)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mods (
                    mod_id, app_id, title, preview_url, description,
                    display_name, custom_description, user_notes, favorite,
                    platform, source_url, external_id, mod_files, updated_at
                )
                VALUES (?, ?, ?, ?, ?, '', '', '', 0, ?, ?, ?, '{}', ?)
                ON CONFLICT(mod_id) DO UPDATE SET
                    app_id = excluded.app_id,
                    title = excluded.title,
                    preview_url = excluded.preview_url,
                    description = excluded.description,
                    platform = 'steam',
                    external_id = excluded.external_id,
                    source_url = CASE
                        WHEN mods.source_url = '' OR mods.platform = 'steam'
                        THEN excluded.source_url
                        ELSE mods.source_url
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    mid,
                    int(meta.app_id or 0),
                    meta.title,
                    meta.preview_url or "",
                    meta.description or "",
                    PLATFORM_STEAM,
                    source,
                    str(mid),
                    _utc_now(),
                ),
            )
            self._conn.commit()

    def upsert_mods(self, metas: Iterable[ModMetadata]) -> int:
        """Batch Steam upsert — user columns are preserved on conflict."""
        rows: list[tuple[Any, ...]] = []
        for meta in metas:
            if not meta.published_file_id or not str(meta.published_file_id).isdigit():
                continue
            if not meta.title:
                continue
            mid = int(meta.published_file_id)
            rows.append(
                (
                    mid,
                    int(meta.app_id or 0),
                    meta.title,
                    meta.preview_url or "",
                    meta.description or "",
                    PLATFORM_STEAM,
                    steam_workshop_url(mid),
                    str(mid),
                    _utc_now(),
                )
            )
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO mods (
                    mod_id, app_id, title, preview_url, description,
                    display_name, custom_description, user_notes, favorite,
                    platform, source_url, external_id, mod_files, updated_at
                )
                VALUES (?, ?, ?, ?, ?, '', '', '', 0, ?, ?, ?, '{}', ?)
                ON CONFLICT(mod_id) DO UPDATE SET
                    app_id = excluded.app_id,
                    title = excluded.title,
                    preview_url = excluded.preview_url,
                    description = excluded.description,
                    platform = 'steam',
                    external_id = excluded.external_id,
                    source_url = CASE
                        WHEN mods.source_url = '' OR mods.platform = 'steam'
                        THEN excluded.source_url
                        ELSE mods.source_url
                    END,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def missing_mod_ids(self, mod_ids: Iterable[int | str]) -> list[str]:
        """Return IDs that are not yet stored (or stored without a title)."""
        wanted = [str(i).strip() for i in mod_ids if str(i).strip().isdigit()]
        if not wanted:
            return []
        existing = self.get_mods_by_ids(wanted)
        missing: list[str] = []
        for mid in wanted:
            cached = existing.get(mid)
            if cached is None or not cached.title:
                missing.append(mid)
        return missing

    # ------------------------------------------------------------------
    # Mods — platform / source / multi-file (generic Mod manager)
    # ------------------------------------------------------------------

    def allocate_mod_id(self) -> int:
        """
        Allocate an internal ``mod_id`` for non-Steam Mods.

        Uses a high positive range so values never collide with Workshop IDs.
        Steam Mods continue to use Workshop ID as ``mod_id``.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MAX(mod_id) AS mx FROM mods
                WHERE mod_id >= ?
                """,
                (NON_STEAM_MOD_ID_BASE,),
            ).fetchone()
            mx = int(row["mx"] or 0) if row is not None else 0
            if mx < NON_STEAM_MOD_ID_BASE:
                return int(NON_STEAM_MOD_ID_BASE)
            return mx + 1

    def find_mod_by_external(
        self,
        platform: str,
        external_id: str,
    ) -> ModDisplayInfo | None:
        """Locate a Mod by platform + external reference (Workshop / Nexus / repo)."""
        plat = normalize_platform(platform)
        ext = str(external_id or "").strip()
        if not ext:
            return None
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {_MOD_SELECT_COLS} FROM mods
                WHERE platform = ? AND external_id = ?
                LIMIT 1
                """,
                (plat, ext),
            ).fetchone()
        if row is None:
            return None
        return _display_info_from_row(row)

    def update_mod_platform_info(
        self,
        mod_id: int | str,
        *,
        platform: str | None = None,
        source_url: str | None = None,
        external_id: str | None = None,
        title: str | None = None,
        app_id: int | None = None,
    ) -> ModDisplayInfo:
        """
        Create or update platform identity fields (never writes ``.info``).

        Changing ``platform`` / ``external_id`` is allowed only when the new
        ``(platform, external_id)`` pair is free (or is this same row).
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            assert row is not None
            old_plat = normalize_platform(str(row["platform"] or PLATFORM_STEAM))
            old_ext = str(row["external_id"] or "").strip()
            plat = (
                normalize_platform(platform)
                if platform is not None
                else old_plat
            )
            url = (
                str(source_url).strip()
                if source_url is not None
                else str(row["source_url"] or "")
            )
            ext = (
                str(external_id).strip()
                if external_id is not None
                else old_ext
            )
            new_title = (
                str(title).strip()
                if title is not None
                else str(row["title"] or "")
            )
            new_app = int(app_id) if app_id is not None else int(row["app_id"] or 0)

            if plat != old_plat or ext != old_ext:
                if not ext:
                    raise ValueError(
                        "external_id is required when changing platform identity"
                    )
                conflict = self._conn.execute(
                    """
                    SELECT mod_id FROM mods
                    WHERE platform = ? AND external_id = ? AND mod_id != ?
                    LIMIT 1
                    """,
                    (plat, ext, mid),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        f"Mod identity already exists: platform={plat} "
                        f"external_id={ext} (mod_id={conflict['mod_id']})"
                    )

            try:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        platform = ?,
                        source_url = ?,
                        external_id = ?,
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        app_id = ?,
                        updated_at = ?
                    WHERE mod_id = ?
                    """,
                    (plat, url, ext, new_title, new_title, new_app, now, mid),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ValueError(
                    f"Mod identity already exists: platform={plat} "
                    f"external_id={ext}"
                ) from exc
            out = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        assert out is not None
        return _display_info_from_row(out)

    def get_mod_files(self, mod_id: int | str) -> ModFilesBundle:
        try:
            mid = int(str(mod_id).strip())
        except (TypeError, ValueError):
            return ModFilesBundle()
        with self._lock:
            row = self._conn.execute(
                "SELECT mod_files FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        if row is None:
            return ModFilesBundle()
        return ModFilesBundle.from_json(str(row["mod_files"] or ""))

    def set_mod_files(
        self,
        mod_id: int | str,
        bundle: ModFilesBundle | Mapping[str, Any] | str,
    ) -> ModFilesBundle:
        """Replace ``mod_files`` JSON for one Mod (multi-file stays one Mod row)."""
        mid = int(str(mod_id).strip())
        if isinstance(bundle, ModFilesBundle):
            parsed = bundle
        elif isinstance(bundle, str):
            parsed = ModFilesBundle.from_json(bundle)
        else:
            parsed = ModFilesBundle.from_dict(bundle)
        payload = parsed.to_json()
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET mod_files = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                (payload, now, mid),
            )
            self._conn.commit()
        return parsed

    def update_mod_offline_status(
        self,
        mod_id: int | str,
        *,
        status: str | None = None,
        provider: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        """
        Patch offline page status columns (SQLite only).

        Omitted kwargs leave existing values. ``updated_at`` defaults to now
        when any field is written.
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            row = self._conn.execute(
                """
                SELECT offline_status, offline_provider, offline_updated_at
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
            keys = set(row.keys()) if row is not None else set()
            cur_status = (
                normalize_offline_status(str(row["offline_status"] or ""))
                if row is not None and "offline_status" in keys
                else OFFLINE_STATUS_NONE
            )
            cur_provider = (
                str(row["offline_provider"] or "")
                if row is not None and "offline_provider" in keys
                else ""
            )
            new_status = (
                normalize_offline_status(status)
                if status is not None
                else cur_status
            )
            new_provider = (
                str(provider).strip() if provider is not None else cur_provider
            )
            new_updated = (
                str(updated_at).strip()
                if updated_at is not None
                else now
            )
            self._conn.execute(
                """
                UPDATE mods SET
                    offline_status = ?,
                    offline_provider = ?,
                    offline_updated_at = ?,
                    updated_at = ?
                WHERE mod_id = ?
                """,
                (new_status, new_provider, new_updated, now, mid),
            )
            self._conn.commit()

    def register_external_mod(
        self,
        *,
        platform: str,
        external_id: str,
        source_url: str = "",
        title: str = "",
        app_id: int = 0,
        game_name: str = "",
        mod_files: ModFilesBundle | Mapping[str, Any] | str | None = None,
        mod_id: int | None = None,
    ) -> ModDisplayInfo:
        """
        Create (or reuse) a non-Steam / multi-platform Mod row.

        Identity is ``(platform, external_id)``. Steam callers should keep using
        ``upsert_mod`` with Workshop ID as ``mod_id``.

        Non-Steam platforms require a real ``app_id`` (game context). Platform
        names such as ``GitHub`` / ``Nexus Mods`` are rejected as *game_name*.
        Uniqueness is enforced by ``uq_mods_platform_external`` when present.
        """
        from services.importers.importer_base import (
            MISSING_GAME_CONTEXT,
            is_invalid_game_name,
        )

        plat = normalize_platform(platform)
        ext = str(external_id or "").strip()
        if not ext:
            raise ValueError("external_id is required")
        resolved_app = int(app_id or 0)
        resolved_game = str(game_name or "").strip()

        if plat != PLATFORM_STEAM:
            if resolved_app <= 0:
                raise ValueError(MISSING_GAME_CONTEXT)
            if is_invalid_game_name(resolved_game):
                if resolved_game:
                    raise ValueError(MISSING_GAME_CONTEXT)
            if mod_id is not None:
                requested = int(mod_id)
                if requested < int(NON_STEAM_MOD_ID_BASE):
                    raise ValueError(
                        "non-Steam mod_id must be >= NON_STEAM_MOD_ID_BASE "
                        f"({NON_STEAM_MOD_ID_BASE})"
                    )
            if resolved_game:
                self.upsert_game(
                    GameInfo(
                        app_id=resolved_app,
                        name=resolved_game,
                        folder_name=resolved_game,
                    )
                )

        if plat == PLATFORM_STEAM and ext.isdigit():
            self.upsert_mod(
                ModMetadata(
                    published_file_id=ext,
                    title=title or f"Unknown_Mod_{ext}",
                    app_id=resolved_app,
                )
            )
            info = self.get_mod_display_info(ext)
            assert info is not None
            if source_url:
                return self.update_mod_platform_info(
                    ext, platform=PLATFORM_STEAM, source_url=source_url, external_id=ext
                )
            return info

        existing = self.find_mod_by_external(plat, ext)
        if existing is not None:
            mid = int(existing.mod_id)
        else:
            mid = int(mod_id) if mod_id is not None else self.allocate_mod_id()

        try:
            info = self.update_mod_platform_info(
                mid,
                platform=plat,
                source_url=source_url,
                external_id=ext,
                title=title or None,
                app_id=resolved_app,
            )
        except (sqlite3.IntegrityError, ValueError):
            raced = self.find_mod_by_external(plat, ext)
            if raced is None or int(raced.mod_id) == mid:
                raise
            mid = int(raced.mod_id)
            info = raced
            if title or source_url or resolved_app:
                info = self.update_mod_platform_info(
                    mid,
                    source_url=source_url or None,
                    title=title or None,
                    app_id=resolved_app if resolved_app else None,
                )

        if mod_files is not None:
            self.set_mod_files(mid, mod_files)
            refreshed = self.get_mod_display_info(mid)
            if refreshed is not None:
                return refreshed
        return info

    # ------------------------------------------------------------------
    # Mods — lifecycle status (invalid / conflict)
    # ------------------------------------------------------------------

    def get_mod_status(self, mod_id: int | str) -> ModStatus:
        """Return lifecycle status flags for ``mod_id`` (defaults when missing)."""
        if not str(mod_id).strip().isdigit():
            return ModStatus()
        mid = int(mod_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT is_invalid, invalid_reason, conflict_status,
                       conflict_note, last_check_time
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return ModStatus()
        keys = set(row.keys())
        return ModStatus(
            invalid=bool(int(row["is_invalid"] or 0)) if "is_invalid" in keys else False,
            invalid_reason=(
                str(row["invalid_reason"] or "") if "invalid_reason" in keys else ""
            ),
            conflict_status=normalize_conflict_status(
                str(row["conflict_status"] or CONFLICT_STATUS_NONE)
                if "conflict_status" in keys
                else CONFLICT_STATUS_NONE
            ),
            conflict_note=(
                str(row["conflict_note"] or "") if "conflict_note" in keys else ""
            ),
            last_check_time=(
                str(row["last_check_time"] or "") if "last_check_time" in keys else ""
            ),
        )

    def update_mod_status(
        self,
        mod_id: int | str,
        *,
        invalid: bool | None = None,
        invalid_reason: str | None = None,
        conflict_status: str | None = None,
        conflict_note: str | None = None,
        last_check_time: str | None = None,
        touch_check_time: bool = False,
    ) -> ModStatus:
        """
        Patch lifecycle status columns. Omitted kwargs leave existing values.

        When ``touch_check_time`` is True and ``last_check_time`` is None,
        sets ``last_check_time`` to now (UTC).
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            current = self.get_mod_status(mid)
            new_invalid = (
                bool(invalid) if invalid is not None else current.invalid
            )
            new_reason = (
                str(invalid_reason)
                if invalid_reason is not None
                else current.invalid_reason
            )
            if not new_invalid and invalid is False:
                # Clearing invalid may also clear reason when caller passes ""
                pass
            if invalid is False and invalid_reason is None:
                new_reason = ""
            new_cstatus = (
                normalize_conflict_status(conflict_status)
                if conflict_status is not None
                else current.conflict_status
            )
            new_cnote = (
                str(conflict_note)
                if conflict_note is not None
                else current.conflict_note
            )
            if conflict_status == CONFLICT_STATUS_NONE and conflict_note is None:
                new_cnote = ""
            if last_check_time is not None:
                new_check = str(last_check_time)
            elif touch_check_time:
                new_check = now
            else:
                new_check = current.last_check_time
            self._conn.execute(
                """
                UPDATE mods SET
                    is_invalid = ?,
                    invalid_reason = ?,
                    conflict_status = ?,
                    conflict_note = ?,
                    last_check_time = ?,
                    updated_at = ?
                WHERE mod_id = ?
                """,
                (
                    1 if new_invalid else 0,
                    new_reason,
                    new_cstatus,
                    new_cnote,
                    new_check,
                    now,
                    mid,
                ),
            )
            self._conn.commit()
        return self.get_mod_status(mid)

    # ------------------------------------------------------------------
    # Mods — version tracking
    # ------------------------------------------------------------------

    def get_mod_version(self, mod_id: int | str) -> ModVersionInfo:
        """Return author / installed version fields (defaults when missing)."""
        mid_s = str(mod_id).strip()
        if not mid_s.isdigit():
            return ModVersionInfo(mod_id=mid_s)
        mid = int(mid_s)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_version, installed_version, version_source,
                       version_checked_at
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return ModVersionInfo(mod_id=mid_s)
        keys = set(row.keys())
        return ModVersionInfo(
            mod_id=mid_s,
            mod_version=(
                str(row["mod_version"] or "") if "mod_version" in keys else ""
            ),
            installed_version=(
                str(row["installed_version"] or "")
                if "installed_version" in keys
                else ""
            ),
            version_source=(
                str(row["version_source"] or "") if "version_source" in keys else ""
            ),
            version_checked_at=(
                str(row["version_checked_at"] or "")
                if "version_checked_at" in keys
                else ""
            ),
        )

    def update_mod_version(
        self,
        mod_id: int | str,
        *,
        mod_version: str | None = None,
        installed_version: str | None = None,
        version_source: str | None = None,
        version_checked_at: str | None = None,
        touch_checked_at: bool = False,
    ) -> ModVersionInfo:
        """
        Patch version columns. Omitted kwargs leave existing values.

        ``installed_version`` is never auto-overwritten when only
        ``mod_version`` is updated.
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            current = self.get_mod_version(mid)
            new_mod_v = (
                str(mod_version)
                if mod_version is not None
                else current.mod_version
            )
            new_inst = (
                str(installed_version)
                if installed_version is not None
                else current.installed_version
            )
            new_src = (
                str(version_source)
                if version_source is not None
                else current.version_source
            )
            if version_checked_at is not None:
                new_checked = str(version_checked_at)
            elif touch_checked_at:
                new_checked = now
            else:
                new_checked = current.version_checked_at
            self._conn.execute(
                """
                UPDATE mods SET
                    mod_version = ?,
                    installed_version = ?,
                    version_source = ?,
                    version_checked_at = ?,
                    updated_at = ?
                WHERE mod_id = ?
                """,
                (new_mod_v, new_inst, new_src, new_checked, now, mid),
            )
            self._conn.commit()
        return self.get_mod_version(mid)

    # ------------------------------------------------------------------
    # Mods — enable / disable
    # ------------------------------------------------------------------

    def is_mod_enabled(self, mod_id: int | str) -> bool:
        if not str(mod_id).strip().isdigit():
            return True
        mid = int(mod_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        if row is None:
            return True
        keys = set(row.keys())
        if "enabled" not in keys:
            return True
        return bool(int(row["enabled"] if row["enabled"] is not None else 1))

    def enable_mod(self, mod_id: int | str) -> bool:
        return self._set_mod_enabled(mod_id, True)

    def disable_mod(self, mod_id: int | str) -> bool:
        return self._set_mod_enabled(mod_id, False)

    def _set_mod_enabled(self, mod_id: int | str, enabled: bool) -> bool:
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET enabled = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                (1 if enabled else 0, now, mid),
            )
            self._conn.commit()
        return self.is_mod_enabled(mid)

    # ------------------------------------------------------------------
    # Mods — user metadata
    # ------------------------------------------------------------------

    def get_mod_display_info(self, mod_id: int | str) -> ModDisplayInfo | None:
        """
        Resolved display payload.

        ``display_name`` is the user override when set; otherwise Steam title.
        """
        if not str(mod_id).strip().isdigit():
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (int(mod_id),),
            ).fetchone()
        if row is None:
            return None
        return _display_info_from_row(row)

    def get_mods_search_fields(
        self,
        mod_ids: Iterable[int | str],
    ) -> dict[str, ModSearchFields]:
        """
        Batch-read search / filter fields for the Mod Library grid.

        Read-only — no writes, no network. Missing IDs are omitted.
        """
        ids: list[int] = []
        seen: set[int] = set()
        for raw in mod_ids:
            text = str(raw).strip()
            if not text.isdigit():
                continue
            mid = int(text)
            if mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    m.mod_id,
                    m.title,
                    m.display_name,
                    m.user_notes,
                    m.favorite,
                    m.deploy_status,
                    m.platform,
                    m.source_url,
                    m.external_id,
                    m.is_invalid,
                    m.conflict_status,
                    m.enabled,
                    COALESCE(g.name, '') AS game_name
                FROM mods AS m
                LEFT JOIN games AS g ON g.app_id = m.app_id
                WHERE m.mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()

        # Category tags (tag_type=category) for filter/search
        cat_map: dict[str, list[str]] = {str(i): [] for i in ids}
        with self._lock:
            tag_rows = self._conn.execute(
                f"""
                SELECT mod_id, tag_value FROM mod_tags
                WHERE mod_id IN ({placeholders}) AND tag_type = ?
                ORDER BY id
                """,
                [*ids, TAG_TYPE_CATEGORY],
            ).fetchall()
        for tr in tag_rows:
            mid_k = str(tr["mod_id"])
            val = str(tr["tag_value"] or "").strip()
            if val and mid_k in cat_map:
                cat_map[mid_k].append(val)

        out: dict[str, ModSearchFields] = {}
        for row in rows:
            steam = str(row["title"] or "").strip()
            user_display = str(row["display_name"] or "").strip()
            mid = str(row["mod_id"])
            status = (
                str(row["deploy_status"] or "").strip() or DEPLOY_STATUS_NOT_DEPLOYED
            )
            keys = set(row.keys())
            enabled = True
            if "enabled" in keys:
                enabled = bool(int(row["enabled"] if row["enabled"] is not None else 1))
            out[mid] = ModSearchFields(
                mod_id=mid,
                steam_name=steam,
                display_name=user_display or steam or f"Unknown_Mod_{mid}",
                user_notes=str(row["user_notes"] or ""),
                favorite=bool(int(row["favorite"] or 0)),
                deploy_status=status,
                game_name=str(row["game_name"] or "").strip(),
                platform=normalize_platform(
                    str(row["platform"] or PLATFORM_STEAM)
                    if "platform" in keys
                    else PLATFORM_STEAM
                ),
                source_url=str(row["source_url"] or "") if "source_url" in keys else "",
                external_id=(
                    str(row["external_id"] or "") if "external_id" in keys else ""
                ),
                is_invalid=(
                    bool(int(row["is_invalid"] or 0)) if "is_invalid" in keys else False
                ),
                conflict_status=normalize_conflict_status(
                    str(row["conflict_status"] or CONFLICT_STATUS_NONE)
                    if "conflict_status" in keys
                    else CONFLICT_STATUS_NONE
                ),
                enabled=enabled,
                category_tags=" ".join(cat_map.get(mid) or []),
            )
        return out

    def update_mod_user_metadata(
        self,
        mod_id: int | str,
        data: Mapping[str, Any],
    ) -> ModDisplayInfo:
        """
        Persist user-editable fields only.

        Creates a stub row when the Mod is not yet in SQLite so edits survive
        before the next Steam sync.
        """
        mid = int(mod_id)
        display_name = str(data.get("display_name", "") or "").strip()
        custom_description = str(data.get("custom_description", "") or "")
        user_notes = str(data.get("user_notes", "") or "")
        favorite_raw = data.get("favorite", 0)
        favorite = 1 if favorite_raw in (True, 1, "1", "true", "True") else 0
        now = _utc_now()

        with self._lock:
            existing = self._conn.execute(
                "SELECT mod_id FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO mods (
                        mod_id, app_id, title, preview_url, description,
                        display_name, custom_description, user_notes, favorite,
                        platform, source_url, external_id, mod_files, updated_at
                    )
                    VALUES (?, 0, '', '', '', ?, ?, ?, ?, 'steam', ?, ?, '{}', ?)
                    """,
                    (
                        mid,
                        display_name,
                        custom_description,
                        user_notes,
                        favorite,
                        steam_workshop_url(mid) if mid > 0 and mid < NON_STEAM_MOD_ID_BASE else "",
                        str(mid) if mid > 0 and mid < NON_STEAM_MOD_ID_BASE else "",
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        display_name = ?,
                        custom_description = ?,
                        user_notes = ?,
                        favorite = ?,
                        updated_at = ?
                    WHERE mod_id = ?
                    """,
                    (
                        display_name,
                        custom_description,
                        user_notes,
                        favorite,
                        now,
                        mid,
                    ),
                )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        assert row is not None
        return _display_info_from_row(row)

    # ------------------------------------------------------------------
    # Mods — deploy status (written by deployer; not by Steam sync)
    # ------------------------------------------------------------------

    def get_mod_deploy_info(self, mod_id: int | str) -> ModDeployInfo | None:
        if not str(mod_id).strip().isdigit():
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id, app_id, deploy_status, deploy_time, deploy_path,
                       deploy_error
                FROM mods WHERE mod_id = ?
                """,
                (int(mod_id),),
            ).fetchone()
        if row is None:
            return None
        return _mod_deploy_from_row(row)

    def list_deployed_mod_ids(self) -> list[str]:
        """Mod IDs currently marked ``deployed`` (for lightweight startup audit)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE deploy_status = ?
                ORDER BY mod_id
                """,
                (DEPLOY_STATUS_DEPLOYED,),
            ).fetchall()
        return [str(r["mod_id"]) for r in rows]

    def update_mod_deploy_status(
        self,
        mod_id: int | str,
        *,
        deploy_status: str = DEPLOY_STATUS_DEPLOYED,
        deploy_path: str = "",
        deploy_time: str | None = None,
        deploy_error: str | None = None,
        app_id: int | None = None,
    ) -> ModDeployInfo:
        """
        Record deploy outcome for a Mod.

        Creates a stub mods row when missing (same pattern as user metadata).
        Steam ``upsert_mod`` never overwrites these columns.

        When *deploy_error* is ``None``, the existing error text is left
        unchanged unless status is ``deployed`` / ``not_deployed`` (cleared).
        Pass ``deploy_error=""`` to clear explicitly.
        """
        mid = int(mod_id)
        status = str(deploy_status or "").strip() or DEPLOY_STATUS_NOT_DEPLOYED
        path = str(deploy_path or "").strip()
        when = deploy_time if deploy_time is not None else _utc_now()
        now = _utc_now()

        with self._lock:
            existing = self._conn.execute(
                "SELECT mod_id, app_id, deploy_error FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()

            if deploy_error is not None:
                err = str(deploy_error)
            elif status in (DEPLOY_STATUS_DEPLOYED, DEPLOY_STATUS_NOT_DEPLOYED):
                err = ""
            elif existing is not None:
                err = str(existing["deploy_error"] or "")
            else:
                err = ""

            if existing is None:
                resolved_app = 0 if app_id is None else int(app_id)
                self._conn.execute(
                    """
                    INSERT INTO mods (
                        mod_id, app_id, title, preview_url, description,
                        display_name, custom_description, user_notes, favorite,
                        deploy_status, deploy_time, deploy_path, deploy_error,
                        platform, source_url, external_id, mod_files,
                        updated_at
                    )
                    VALUES (?, ?, '', '', '', '', '', '', 0, ?, ?, ?, ?, 'steam', ?, ?, '{}', ?)
                    """,
                    (
                        mid,
                        resolved_app,
                        status,
                        when,
                        path,
                        err,
                        steam_workshop_url(mid)
                        if mid > 0 and mid < NON_STEAM_MOD_ID_BASE
                        else "",
                        str(mid) if mid > 0 and mid < NON_STEAM_MOD_ID_BASE else "",
                        now,
                    ),
                )
            else:
                if app_id is not None:
                    self._conn.execute(
                        """
                        UPDATE mods SET
                            app_id = ?,
                            deploy_status = ?,
                            deploy_time = ?,
                            deploy_path = ?,
                            deploy_error = ?,
                            updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (int(app_id), status, when, path, err, now, mid),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE mods SET
                            deploy_status = ?,
                            deploy_time = ?,
                            deploy_path = ?,
                            deploy_error = ?,
                            updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (status, when, path, err, now, mid),
                    )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT mod_id, app_id, deploy_status, deploy_time, deploy_path,
                       deploy_error
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        assert row is not None
        return _mod_deploy_from_row(row)

    # ------------------------------------------------------------------
    # Mods — user tags (mod_tags)
    # ------------------------------------------------------------------

    def add_mod_tag(
        self,
        mod_id: int | str,
        tag_type: str,
        tag_value: str = "",
    ) -> ModTag:
        """
        Insert or refresh a tag for ``mod_id``.

        For ``invalid`` / ``conflict``, at most one row per type is kept
        (``tag_value`` is updated in place).
        """
        mid = int(str(mod_id).strip())
        ttype = str(tag_type or "").strip()
        if not ttype:
            raise ValueError("tag_type is required")
        value = str(tag_value or "")
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            if ttype in (TAG_TYPE_INVALID, TAG_TYPE_CONFLICT):
                existing = self._conn.execute(
                    """
                    SELECT id FROM mod_tags
                    WHERE mod_id = ? AND tag_type = ?
                    ORDER BY id LIMIT 1
                    """,
                    (mid, ttype),
                ).fetchone()
                if existing is not None:
                    self._conn.execute(
                        """
                        UPDATE mod_tags
                        SET tag_value = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (value, now, int(existing["id"])),
                    )
                    row_id = int(existing["id"])
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO mod_tags
                            (mod_id, tag_type, tag_value, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (mid, ttype, value, now, now),
                    )
                    row_id = int(cur.lastrowid)
            else:
                # Category / freeform tags: skip duplicate (mod_id, type, value)
                dup = self._conn.execute(
                    """
                    SELECT id FROM mod_tags
                    WHERE mod_id = ? AND tag_type = ? AND tag_value = ?
                    ORDER BY id LIMIT 1
                    """,
                    (mid, ttype, value),
                ).fetchone()
                if dup is not None:
                    row_id = int(dup["id"])
                else:
                    cur = self._conn.execute(
                        """
                        INSERT INTO mod_tags
                            (mod_id, tag_type, tag_value, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (mid, ttype, value, now, now),
                    )
                    row_id = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, mod_id, tag_type, tag_value, created_at, updated_at
                FROM mod_tags WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
        assert row is not None
        return _mod_tag_from_row(row)

    def remove_mod_tag(
        self,
        mod_id: int | str,
        tag_type: str,
        tag_value: str | None = None,
    ) -> int:
        """
        Remove tags matching ``mod_id`` + ``tag_type``.

        When ``tag_value`` is given, only that value is removed.
        Returns number of deleted rows.
        """
        mid = int(str(mod_id).strip())
        ttype = str(tag_type or "").strip()
        if not ttype:
            return 0
        with self._lock:
            if tag_value is None:
                cur = self._conn.execute(
                    "DELETE FROM mod_tags WHERE mod_id = ? AND tag_type = ?",
                    (mid, ttype),
                )
            else:
                cur = self._conn.execute(
                    """
                    DELETE FROM mod_tags
                    WHERE mod_id = ? AND tag_type = ? AND tag_value = ?
                    """,
                    (mid, ttype, str(tag_value)),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def get_mod_tags(self, mod_id: int | str) -> list[ModTag]:
        if not str(mod_id).strip().isdigit():
            return []
        mid = int(mod_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, mod_id, tag_type, tag_value, created_at, updated_at
                FROM mod_tags WHERE mod_id = ?
                ORDER BY tag_type, id
                """,
                (mid,),
            ).fetchall()
        return [_mod_tag_from_row(r) for r in rows]

    def get_mods_by_tag(
        self,
        tag_type: str,
        tag_value: str | None = None,
    ) -> list[str]:
        """Return distinct mod_id strings that have the given tag."""
        ttype = str(tag_type or "").strip()
        if not ttype:
            return []
        with self._lock:
            if tag_value is None:
                rows = self._conn.execute(
                    """
                    SELECT DISTINCT mod_id FROM mod_tags
                    WHERE tag_type = ?
                    ORDER BY mod_id
                    """,
                    (ttype,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT DISTINCT mod_id FROM mod_tags
                    WHERE tag_type = ? AND tag_value = ?
                    ORDER BY mod_id
                    """,
                    (ttype, str(tag_value)),
                ).fetchall()
        return [str(r["mod_id"]) for r in rows]

    def add_category_tag(self, mod_id: int | str, tag: str) -> ModTag:
        """User category label (Graphics / Gameplay / …) stored as ``category``."""
        label = str(tag or "").strip()
        if not label:
            raise ValueError("tag is required")
        return self.add_mod_tag(mod_id, TAG_TYPE_CATEGORY, label)

    def remove_category_tag(self, mod_id: int | str, tag: str) -> int:
        return self.remove_mod_tag(mod_id, TAG_TYPE_CATEGORY, str(tag or "").strip())

    def get_category_tags(self, mod_id: int | str) -> list[str]:
        return [
            t.tag_value
            for t in self.get_mod_tags(mod_id)
            if t.tag_type == TAG_TYPE_CATEGORY and (t.tag_value or "").strip()
        ]

    def list_all_category_tags(self) -> list[str]:
        """Distinct category labels across the library (sorted)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT tag_value FROM mod_tags
                WHERE tag_type = ? AND TRIM(tag_value) != ''
                ORDER BY tag_value COLLATE NOCASE
                """,
                (TAG_TYPE_CATEGORY,),
            ).fetchall()
        return [str(r["tag_value"]) for r in rows]

    def delete_mod_record(self, mod_id: int | str) -> bool:
        """
        Remove SQLite rows for ``mod_id`` (mods + tags + relations).

        Does not touch filesystem or game install paths.
        """
        if not str(mod_id).strip().isdigit():
            return False
        mid = int(mod_id)
        mid_s = str(mid)
        with self._lock:
            self._conn.execute("DELETE FROM mod_tags WHERE mod_id = ?", (mid,))
            self._conn.execute(
                """
                DELETE FROM mod_relations
                WHERE source_mod_id = ? OR target_mod_id = ?
                """,
                (mid_s, mid_s),
            )
            cur = self._conn.execute("DELETE FROM mods WHERE mod_id = ?", (mid,))
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def get_mods_tag_flags(
        self,
        mod_ids: Iterable[int | str],
    ) -> dict[str, ModTagFlags]:
        """Batch-read invalid / conflict flags + tag_value text for search."""
        ids = [int(str(i).strip()) for i in mod_ids if str(i).strip().isdigit()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT mod_id, tag_type, tag_value FROM mod_tags
                WHERE mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        buckets: dict[str, dict[str, Any]] = {
            str(i): {
                "invalid": False,
                "conflict": False,
                "invalid_reason": "",
                "values": [],
            }
            for i in ids
        }
        for row in rows:
            mid = str(row["mod_id"])
            bucket = buckets.get(mid)
            if bucket is None:
                continue
            ttype = str(row["tag_type"] or "")
            value = str(row["tag_value"] or "")
            if value:
                bucket["values"].append(value)
            if ttype == TAG_TYPE_INVALID:
                bucket["invalid"] = True
                if value:
                    bucket["invalid_reason"] = value
            elif ttype == TAG_TYPE_CONFLICT:
                bucket["conflict"] = True
        return {
            mid: ModTagFlags(
                invalid=bool(data["invalid"]),
                conflict=bool(data["conflict"]),
                invalid_reason=str(data["invalid_reason"] or ""),
                tag_values=tuple(data["values"]),
            )
            for mid, data in buckets.items()
        }

    # ------------------------------------------------------------------
    # Mods — relations (mod_relations)
    # ------------------------------------------------------------------

    def add_mod_relation(
        self,
        source_mod_id: int | str,
        target_mod_id: int | str,
        relation_type: str = RELATION_TYPE_CONFLICT,
        note: str = "",
    ) -> ModRelation:
        """Add a relation; duplicate (source, target, type) is refreshed."""
        src = int(str(source_mod_id).strip())
        tgt = int(str(target_mod_id).strip())
        rtype = str(relation_type or "").strip() or RELATION_TYPE_CONFLICT
        note_s = str(note or "")
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(src)
            self._ensure_mod_stub(tgt)
            existing = self._conn.execute(
                """
                SELECT id FROM mod_relations
                WHERE source_mod_id = ? AND target_mod_id = ? AND relation_type = ?
                LIMIT 1
                """,
                (src, tgt, rtype),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE mod_relations SET note = ? WHERE id = ?",
                    (note_s, int(existing["id"])),
                )
                row_id = int(existing["id"])
            else:
                cur = self._conn.execute(
                    """
                    INSERT INTO mod_relations
                        (source_mod_id, target_mod_id, relation_type, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (src, tgt, rtype, note_s, now),
                )
                row_id = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, source_mod_id, target_mod_id, relation_type, note, created_at
                FROM mod_relations WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
        assert row is not None
        return _mod_relation_from_row(row)

    def remove_mod_relation(
        self,
        source_mod_id: int | str,
        target_mod_id: int | str | None = None,
        relation_type: str | None = RELATION_TYPE_CONFLICT,
    ) -> int:
        """
        Remove relations from ``source_mod_id``.

        If ``target_mod_id`` is None, remove all matching ``relation_type``
        (or all types when ``relation_type`` is None).
        """
        src = int(str(source_mod_id).strip())
        with self._lock:
            if target_mod_id is None and relation_type is None:
                cur = self._conn.execute(
                    "DELETE FROM mod_relations WHERE source_mod_id = ?",
                    (src,),
                )
            elif target_mod_id is None:
                cur = self._conn.execute(
                    """
                    DELETE FROM mod_relations
                    WHERE source_mod_id = ? AND relation_type = ?
                    """,
                    (src, str(relation_type)),
                )
            elif relation_type is None:
                cur = self._conn.execute(
                    """
                    DELETE FROM mod_relations
                    WHERE source_mod_id = ? AND target_mod_id = ?
                    """,
                    (src, int(str(target_mod_id).strip())),
                )
            else:
                cur = self._conn.execute(
                    """
                    DELETE FROM mod_relations
                    WHERE source_mod_id = ? AND target_mod_id = ?
                          AND relation_type = ?
                    """,
                    (src, int(str(target_mod_id).strip()), str(relation_type)),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def get_mod_relations(
        self,
        mod_id: int | str,
        *,
        relation_type: str | None = RELATION_TYPE_CONFLICT,
        as_source: bool = True,
    ) -> list[ModRelation]:
        if not str(mod_id).strip().isdigit():
            return []
        mid = int(mod_id)
        col = "source_mod_id" if as_source else "target_mod_id"
        with self._lock:
            if relation_type is None:
                rows = self._conn.execute(
                    f"""
                    SELECT id, source_mod_id, target_mod_id, relation_type, note, created_at
                    FROM mod_relations WHERE {col} = ?
                    ORDER BY id
                    """,
                    (mid,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"""
                    SELECT id, source_mod_id, target_mod_id, relation_type, note, created_at
                    FROM mod_relations
                    WHERE {col} = ? AND relation_type = ?
                    ORDER BY id
                    """,
                    (mid, str(relation_type)),
                ).fetchall()
        return [_mod_relation_from_row(r) for r in rows]

    def set_mod_conflict_targets(
        self,
        mod_id: int | str,
        target_mod_ids: Iterable[int | str],
        *,
        note: str = "",
    ) -> list[ModRelation]:
        """
        Replace conflict relations for ``mod_id`` and sync the conflict tag.

        Empty ``target_mod_ids`` clears conflict relations and the conflict tag.
        """
        src = int(str(mod_id).strip())
        targets = sorted(
            {
                int(str(t).strip())
                for t in target_mod_ids
                if str(t).strip().isdigit() and int(str(t).strip()) != src
            }
        )
        with self._lock:
            self._ensure_mod_stub(src)
            self._conn.execute(
                """
                DELETE FROM mod_relations
                WHERE source_mod_id = ? AND relation_type = ?
                """,
                (src, RELATION_TYPE_CONFLICT),
            )
            now = _utc_now()
            for tgt in targets:
                self._ensure_mod_stub(tgt)
                self._conn.execute(
                    """
                    INSERT INTO mod_relations
                        (source_mod_id, target_mod_id, relation_type, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (src, tgt, RELATION_TYPE_CONFLICT, str(note or ""), now),
                )
            self._conn.commit()

        if targets:
            self.add_mod_tag(src, TAG_TYPE_CONFLICT, tag_value="")
        else:
            self.remove_mod_tag(src, TAG_TYPE_CONFLICT)

        return self.get_mod_relations(src)

    def _ensure_mod_stub(self, mod_id: int) -> None:
        """Insert a minimal mods row so FK-less tags can still attach to an ID."""
        now = _utc_now()
        mid = int(mod_id)
        is_steam_range = mid > 0 and mid < NON_STEAM_MOD_ID_BASE
        # Always set a unique provisional external_id (= mod_id text) so
        # UNIQUE(platform, external_id) is not violated by empty stubs.
        self._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                platform, source_url, external_id, mod_files, updated_at
            )
            VALUES (?, 0, '', '', '', '', '', '', 0, 'steam', ?, ?, '{}', ?)
            ON CONFLICT(mod_id) DO NOTHING
            """,
            (
                mid,
                steam_workshop_url(mid) if is_steam_range else "",
                str(mid),
                now,
            ),
        )


def _game_from_row(row: sqlite3.Row) -> GameInfo:
    name = str(row["name"] or "")
    app_id = int(row["app_id"])
    return GameInfo(
        app_id=app_id,
        name=name,
        header_image=str(row["header_url"] or ""),
        short_description=str(row["description"] or ""),
        folder_name=sanitize_folder_name(name, fallback=f"App_{app_id}"),
    )


def _game_deploy_from_row(row: sqlite3.Row) -> GameDeployConfig:
    dtype = str(row["deploy_type"] or "").strip() or DEPLOY_TYPE_FOLDER_COPY
    return GameDeployConfig(
        app_id=int(row["app_id"]),
        install_path=str(row["install_path"] or ""),
        mod_path=str(row["mod_path"] or ""),
        deploy_type=dtype,
        name=str(row["name"] or ""),
    )


def _mod_deploy_from_row(row: sqlite3.Row) -> ModDeployInfo:
    status = str(row["deploy_status"] or "").strip() or DEPLOY_STATUS_NOT_DEPLOYED
    keys = set(row.keys())
    return ModDeployInfo(
        mod_id=str(row["mod_id"]),
        deploy_status=status,
        deploy_time=str(row["deploy_time"] or ""),
        deploy_path=str(row["deploy_path"] or ""),
        app_id=int(row["app_id"] or 0),
        deploy_error=str(row["deploy_error"] or "") if "deploy_error" in keys else "",
    )


def _mod_tag_from_row(row: sqlite3.Row) -> ModTag:
    return ModTag(
        id=int(row["id"]),
        mod_id=str(row["mod_id"]),
        tag_type=str(row["tag_type"] or ""),
        tag_value=str(row["tag_value"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _mod_relation_from_row(row: sqlite3.Row) -> ModRelation:
    return ModRelation(
        id=int(row["id"]),
        source_mod_id=str(row["source_mod_id"]),
        target_mod_id=str(row["target_mod_id"]),
        relation_type=str(row["relation_type"] or ""),
        note=str(row["note"] or ""),
        created_at=str(row["created_at"] or ""),
    )


def _mod_from_row(row: sqlite3.Row) -> ModMetadata:
    keys = row.keys()
    return ModMetadata(
        published_file_id=str(row["mod_id"]),
        title=str(row["title"] or ""),
        description=str(row["description"] or ""),
        preview_url=str(row["preview_url"] or ""),
        app_id=int(row["app_id"] or 0),
        custom_notes=str(row["user_notes"] or "") if "user_notes" in keys else "",
    )


def _display_info_from_row(row: sqlite3.Row) -> ModDisplayInfo:
    keys = set(row.keys())
    steam_name = str(row["title"] or "").strip()
    user_display = str(row["display_name"] or "").strip()
    resolved = user_display or steam_name or f"Unknown_Mod_{row['mod_id']}"
    platform = (
        normalize_platform(str(row["platform"] or PLATFORM_STEAM))
        if "platform" in keys
        else PLATFORM_STEAM
    )
    source_url = str(row["source_url"] or "") if "source_url" in keys else ""
    external_id = str(row["external_id"] or "") if "external_id" in keys else ""
    mod_files_json = (
        str(row["mod_files"] or DEFAULT_MOD_FILES_JSON)
        if "mod_files" in keys
        else DEFAULT_MOD_FILES_JSON
    )
    if not mod_files_json.strip():
        mod_files_json = DEFAULT_MOD_FILES_JSON
    is_invalid = (
        bool(int(row["is_invalid"] or 0)) if "is_invalid" in keys else False
    )
    invalid_reason = (
        str(row["invalid_reason"] or "") if "invalid_reason" in keys else ""
    )
    conflict_status = normalize_conflict_status(
        str(row["conflict_status"] or CONFLICT_STATUS_NONE)
        if "conflict_status" in keys
        else CONFLICT_STATUS_NONE
    )
    conflict_note = (
        str(row["conflict_note"] or "") if "conflict_note" in keys else ""
    )
    last_check_time = (
        str(row["last_check_time"] or "") if "last_check_time" in keys else ""
    )
    mod_version = str(row["mod_version"] or "") if "mod_version" in keys else ""
    installed_version = (
        str(row["installed_version"] or "") if "installed_version" in keys else ""
    )
    version_source = (
        str(row["version_source"] or "") if "version_source" in keys else ""
    )
    version_checked_at = (
        str(row["version_checked_at"] or "") if "version_checked_at" in keys else ""
    )
    enabled = True
    if "enabled" in keys:
        enabled = bool(int(row["enabled"] if row["enabled"] is not None else 1))
    offline_status = (
        normalize_offline_status(str(row["offline_status"] or OFFLINE_STATUS_NONE))
        if "offline_status" in keys
        else OFFLINE_STATUS_NONE
    )
    offline_provider = (
        str(row["offline_provider"] or "") if "offline_provider" in keys else ""
    )
    offline_updated_at = (
        str(row["offline_updated_at"] or "")
        if "offline_updated_at" in keys
        else ""
    )
    return ModDisplayInfo(
        mod_id=str(row["mod_id"]),
        steam_name=steam_name,
        steam_description=str(row["description"] or ""),
        preview_url=str(row["preview_url"] or ""),
        display_name=resolved,
        custom_description=str(row["custom_description"] or ""),
        user_notes=str(row["user_notes"] or ""),
        favorite=bool(int(row["favorite"] or 0)),
        user_display_name=user_display,
        app_id=int(row["app_id"] or 0),
        platform=platform,
        source_url=source_url,
        external_id=external_id,
        mod_files_json=mod_files_json,
        is_invalid=is_invalid,
        invalid_reason=invalid_reason,
        conflict_status=conflict_status,
        conflict_note=conflict_note,
        last_check_time=last_check_time,
        mod_version=mod_version,
        installed_version=installed_version,
        version_source=version_source,
        version_checked_at=version_checked_at,
        enabled=enabled,
        offline_status=offline_status,
        offline_provider=offline_provider,
        offline_updated_at=offline_updated_at,
    )


def get_db() -> DatabaseManager:
    """Convenience accessor for the process-wide database singleton."""
    return DatabaseManager.instance()
