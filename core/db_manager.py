"""SQLite snapshot layer for Steam game / Mod metadata."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .game_info import GameInfo
from .models import ModMetadata
from .mod_platform import (
    DEFAULT_MOD_FILES_JSON,
    NON_STEAM_MOD_ID_BASE,
    OFFLINE_STATUS_NONE,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    SUPPORTED_PLATFORMS,
    ModFileEntry,
    ModFilesBundle,
    corrected_nexus_workspace_id,
    generate_unique_workspace_id,
    is_internal_mod_id,
    is_modio_external_id_pollution,
    normalize_offline_status,
    normalize_platform,
    normalize_platform_if_known,
    resolve_workspace_id,
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
    workshop_path TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mods (
    -- SQLite PK / FK handle only. Never user-facing. Never Workspace ID.
    -- Frozen Entity Identity is TEXT mods.internal_id (added via migration).
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
    -- Platform-native ID (Steam Workshop ID / Nexus Mod ID). Not a user Mod ID.
    external_id TEXT NOT NULL DEFAULT '',
    -- ONLY user-facing Mod identifier. Steam/Nexus: equals external_id.
    -- Never generated from Internal ID (mod_id).
    workspace_id TEXT NOT NULL DEFAULT '',
    custom_deploy_path TEXT NOT NULL DEFAULT '',
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
    cover_path TEXT NOT NULL DEFAULT '',
    -- Witcher 3 ONLY: original | next_gen | remake. NULL for every other game.
    -- Not mod_version, not Mod.io version, not Steam revision, not identity.
    game_version TEXT,
    -- Optional free-text subcategory. Displayed only when type is「拓展」.
    -- Not a taxonomy / not category_tags / not sidecar.category (type label).
    category TEXT,
    -- Game-scoped Type Definition id. NULL = unbound. Never a type name.
    type_id INTEGER,
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

CREATE TABLE IF NOT EXISTS mod_relationships (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_mod_id       INTEGER NOT NULL,
    target_mod_id       INTEGER NOT NULL,
    relationship_type   TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (source_mod_id) REFERENCES mods(mod_id),
    FOREIGN KEY (target_mod_id) REFERENCES mods(mod_id),
    UNIQUE (source_mod_id, target_mod_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_mod_relationships_source
    ON mod_relationships(source_mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_relationships_target
    ON mod_relationships(target_mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_relationships_type
    ON mod_relationships(relationship_type);

CREATE TABLE IF NOT EXISTS game_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (app_id, name)
);

CREATE INDEX IF NOT EXISTS idx_game_categories_app_id
    ON game_categories(app_id);

CREATE TABLE IF NOT EXISTS deployment_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES games(app_id)
);

CREATE INDEX IF NOT EXISTS idx_deployment_records_app_id
    ON deployment_records(app_id);
-- Per-game display name uniqueness (user-facing identity).
CREATE UNIQUE INDEX IF NOT EXISTS uq_deployment_records_app_name
    ON deployment_records(app_id, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS deployment_record_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   INTEGER NOT NULL,
    mod_id      INTEGER NOT NULL,
    FOREIGN KEY (record_id) REFERENCES deployment_records(id),
    FOREIGN KEY (mod_id) REFERENCES mods(mod_id),
    UNIQUE (record_id, mod_id)
);

CREATE INDEX IF NOT EXISTS idx_deployment_record_items_record_id
    ON deployment_record_items(record_id);
CREATE INDEX IF NOT EXISTS idx_deployment_record_items_mod_id
    ON deployment_record_items(mod_id);

CREATE TABLE IF NOT EXISTS identity_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id      TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    old_value   TEXT NOT NULL DEFAULT '',
    new_value   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_audit_log_mod_id
    ON identity_audit_log(mod_id);

CREATE TABLE IF NOT EXISTS collections (
    collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id        INTEGER NOT NULL,
    name          TEXT NOT NULL,
    cover_path    TEXT NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES games(app_id)
);

CREATE INDEX IF NOT EXISTS idx_collections_app_id
    ON collections(app_id);
-- Per-game display name uniqueness (user-facing identity).
CREATE UNIQUE INDEX IF NOT EXISTS uq_collections_app_name
    ON collections(app_id, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS collection_mods (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    -- FK to mods.mod_id (SQLite PK). Never workspace_id. Never TEXT internal_id.
    mod_id        INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (collection_id) REFERENCES collections(collection_id) ON DELETE CASCADE,
    FOREIGN KEY (mod_id) REFERENCES mods(mod_id),
    UNIQUE (collection_id, mod_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_mods_collection_id
    ON collection_mods(collection_id);
CREATE INDEX IF NOT EXISTS idx_collection_mods_mod_id
    ON collection_mods(mod_id);
"""

# Columns added after the initial schema — applied on every startup.
_GAMES_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("install_path", "TEXT NOT NULL DEFAULT ''"),
    ("mod_path", "TEXT NOT NULL DEFAULT ''"),
    ("deploy_type", "TEXT NOT NULL DEFAULT 'folder_copy'"),
    ("workshop_path", "TEXT NOT NULL DEFAULT ''"),
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
    ("workspace_id", "TEXT NOT NULL DEFAULT ''"),
    ("custom_deploy_path", "TEXT NOT NULL DEFAULT ''"),
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
    ("cover_path", "TEXT NOT NULL DEFAULT ''"),
    ("last_known_path", "TEXT NOT NULL DEFAULT ''"),
    ("folder_present", "INTEGER NOT NULL DEFAULT 1"),
    ("backup_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("backup_metadata_json", "TEXT NOT NULL DEFAULT ''"),
    ("backup_cover_path", "TEXT NOT NULL DEFAULT ''"),
    ("backup_offline_path", "TEXT NOT NULL DEFAULT ''"),
    ("backup_status", "TEXT NOT NULL DEFAULT ''"),
    ("backup_last_validate_at", "TEXT NOT NULL DEFAULT ''"),
    ("internal_id", "TEXT NOT NULL DEFAULT ''"),
    ("library_status", "TEXT NOT NULL DEFAULT ''"),
    ("source_type", "TEXT NOT NULL DEFAULT ''"),
    ("content_status", "TEXT NOT NULL DEFAULT ''"),
    ("identity_status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("official_metadata_synced", "INTEGER NOT NULL DEFAULT 0"),
    ("user_override_fields", "TEXT NOT NULL DEFAULT '{}'"),
    # Witcher 3 ONLY compatibility edition. NULL = not applicable (non-Witcher 3).
    # Never DEFAULT 'next_gen' — that would stamp every game.
    ("game_version", "TEXT"),
    # Optional「分类」for type=拓展. NULL/empty = hidden. Not category_tags.
    ("category", "TEXT"),
    # Game-scoped Type Definition id. NULL = unbound. Not a type name.
    ("type_id", "INTEGER"),
    # Filesystem observation stamps (NOT identity). Cheap L0 probe memory only.
    ("fs_observed_at", "TEXT NOT NULL DEFAULT ''"),
    ("fs_root_mtime", "REAL"),
    # Local managed-directory size observation (NOT Steam archive / file_size).
    # local_size_bytes NULL = unknown (never observed). 0 + status=ok = empty dir.
    ("local_size_bytes", "INTEGER"),
    ("local_size_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("local_size_observed_at", "TEXT NOT NULL DEFAULT ''"),
    ("local_size_root_mtime", "REAL"),
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
TAG_TYPE_ABANDONED = "abandoned"
TAG_TYPE_CATEGORY = "category"
RELATION_TYPE_CONFLICT = "conflict"

# User-declared Mod relationships (mod_relationships) — never auto-guessed
RELATIONSHIP_DEPENDENCY = "dependency"
RELATIONSHIP_CONFLICT = "conflict"
RELATIONSHIP_ADDON = "addon"
RELATIONSHIP_PATCH = "patch"
SUPPORTED_RELATIONSHIP_TYPES = (
    RELATIONSHIP_DEPENDENCY,
    RELATIONSHIP_CONFLICT,
    RELATIONSHIP_ADDON,
    RELATIONSHIP_PATCH,
)

_MOD_SELECT_COLS = (
    "mod_id, app_id, title, preview_url, description, "
    "display_name, custom_description, user_notes, favorite, "
    "platform, source_url, external_id, workspace_id, custom_deploy_path, "
    "mod_files, "
    "is_invalid, invalid_reason, conflict_status, conflict_note, last_check_time, "
    "mod_version, installed_version, version_source, version_checked_at, "
    "enabled, "
    "offline_status, offline_provider, offline_updated_at, "
    "cover_path, "
    "game_version, "
    "category, "
    "type_id, "
    "updated_at"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append_mods_updated_at(
    sets: list[str],
    params: list[Any],
    *,
    reason: str,
) -> None:
    """
    Append ``updated_at = ?`` only after authority validation.

    ARCHITECTURE RULE: every mods.updated_at write must pass a legal reason
    (see ``services.updated_at_authority``). System paths must omit this helper.
    """
    from services.updated_at_authority import validate_updated_at_reason

    validate_updated_at_reason(reason)
    sets.append("updated_at = ?")
    params.append(_utc_now())


def _mods_updated_at_now(*, reason: str) -> str:
    """Return UTC now only when *reason* is a legal ``mods.updated_at`` reason."""
    from services.updated_at_authority import validate_updated_at_reason

    validate_updated_at_reason(reason)
    return _utc_now()


def updated_at_to_mtime(value: str | None) -> float:
    """
    Convert ``mods.updated_at`` ISO text to a Library sort epoch.

    ARCHITECTURE RULE: Library 「最近修改」 uses DB ``updated_at`` only —
    never filesystem mtime, never ``backup_updated_at``.
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return float(parsed.timestamp())
    except ValueError:
        return 0.0


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
    workspace_id: str = ""
    custom_deploy_path: str = ""
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
    cover_path: str = ""
    # Witcher 3 ONLY: original | next_gen | remake. Empty = NULL / not applicable.
    game_version: str = ""
    # Optional「分类」free text. Empty/NULL when unset. Not category_tags.
    category: str = ""
    # Bound Type Definition id for this Mod's game. None = unbound.
    type_id: int | None = None

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
    workspace_id: str = ""
    is_invalid: bool = False
    conflict_status: str = CONFLICT_STATUS_NONE
    enabled: bool = True
    category_tags: str = ""
    type_id: int | None = None
    # Library sort authority (ISO → epoch via updated_at_to_mtime).
    updated_at: str = ""


@dataclass(frozen=True)
class GameDeployConfig:
    """Per-game deploy paths (SQLite ``games`` row)."""

    app_id: int
    install_path: str = ""
    mod_path: str = ""
    deploy_type: str = DEPLOY_TYPE_FOLDER_COPY
    name: str = ""
    workshop_path: str = ""


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
class ModRelationship:
    """One user-declared row from ``mod_relationships``."""

    id: int
    source_mod_id: str
    target_mod_id: str
    relationship_type: str
    created_at: str = ""
    target_title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_mod_id": self.source_mod_id,
            "target_mod_id": self.target_mod_id,
            "mod_id": self.target_mod_id,
            "relationship_type": self.relationship_type,
            "title": self.target_title or "",
            "created_at": self.created_at or "",
        }


@dataclass(frozen=True)
class DeploymentRecord:
    """Named saved Mod set for one game (does not change live deploy state)."""

    id: int
    app_id: int
    name: str
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app_id": self.app_id,
            "name": self.name,
            "created_at": self.created_at or "",
            "updated_at": self.updated_at or "",
        }


@dataclass(frozen=True)
class CollectionRecord:
    """Named Mod Collection for one game (browse/org only — not deploy / WH3)."""

    collection_id: int
    app_id: int
    name: str
    cover_path: str = ""
    sort_order: int = 0
    created_at: str = ""
    updated_at: str = ""
    mod_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "app_id": self.app_id,
            "name": self.name,
            "cover_path": self.cover_path or "",
            "sort_order": self.sort_order,
            "created_at": self.created_at or "",
            "updated_at": self.updated_at or "",
            "mod_count": int(self.mod_count or 0),
        }


@dataclass(frozen=True)
class ModTagFlags:
    """Compact tag summary for library cards / filters."""

    invalid: bool = False
    conflict: bool = False
    invalid_reason: str = ""
    tag_values: tuple[str, ...] = ()
    dependency_count: int = 0
    relationship_conflict_count: int = 0


class IdentityIntegrityError(RuntimeError):
    """Raised when identity UNIQUE constraints cannot be enforced (duplicates)."""


class DatabaseShutdownError(RuntimeError):
    """Raised when ``get_db()`` would otherwise reopen a connection during shutdown."""


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
    _app_shutdown = False

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else database_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._init_schema()
        except Exception:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._closed = True
            raise

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls, db_path: str | Path | None = None) -> DatabaseManager:
        """Return the process-wide singleton.

        When *db_path* is given and differs from the open singleton (typical in
        tests), close and reopen so callers never silently write to the wrong DB.
        Omitting *db_path* keeps the existing instance (production GUI path).
        """
        with cls._instance_lock:
            if cls._app_shutdown:
                if cls._instance is None:
                    raise DatabaseShutdownError(
                        "DatabaseManager is shut down; refusing new connection"
                    )
                return cls._instance
            if cls._instance is None:
                if db_path is None:
                    test_db = os.environ.get("SMM_TEST_DB", "").strip()
                    if test_db:
                        db_path = test_db
                cls._instance = cls(db_path=db_path)
                return cls._instance
            if db_path is not None:
                requested = Path(db_path).expanduser().resolve()
                current = Path(cls._instance.db_path).expanduser().resolve()
                if requested != current:
                    cls._instance.close()
                    cls._instance = cls(db_path=db_path)
            return cls._instance

    @classmethod
    def begin_app_shutdown(cls) -> None:
        """Refuse opening a new singleton connection (process shutdown)."""
        cls._app_shutdown = True

    @classmethod
    def close_singleton(cls) -> None:
        """Close the process singleton once. Does not drop the instance."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.close()

    @classmethod
    def reset_instance(cls) -> None:
        """Close and drop the process-wide singleton (tests / shutdown)."""
        with cls._instance_lock:
            cls._app_shutdown = False
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None

    def is_closed(self) -> bool:
        return bool(self._closed)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_games_table()
            self._migrate_mods_table()
            self._backfill_witcher3_game_version()
            self._backfill_steam_platform_fields()
            self._backfill_workspace_ids()
            self._clear_system_inferred_conflict_pollution()
            self._clear_illegal_content_missing_pollution()
            self._clear_identity_pollution_from_user_status()
            self._run_status_recovery_db_phase()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mods_platform ON mods(platform)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mods_workspace_id ON mods(workspace_id)"
            )
            self._ensure_unique_platform_external_index()
            self._ensure_deployment_record_name_unique()
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

    def _backfill_witcher3_game_version(self) -> None:
        """Witcher 3 ONLY: NULL/empty/invalid → next_gen. Other games stay NULL.

        Idempotent. Never stamps next_gen onto a non-Witcher-3 row.
        Does not recreate Mod entities.
        """
        from core.witcher3_game_version import (
            WITCHER3_APP_IDS,
            WITCHER3_DEFAULT_VERSION,
            WITCHER3_GAME_VERSIONS,
        )

        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "game_version" not in cols:
            return
        ids = ",".join(str(i) for i in sorted(WITCHER3_APP_IDS))
        legal = ",".join(f"'{v}'" for v in sorted(WITCHER3_GAME_VERSIONS))
        self._conn.execute(
            f"""
            UPDATE mods SET game_version = ?
            WHERE app_id IN ({ids})
              AND (
                game_version IS NULL
                OR TRIM(game_version) = ''
                OR game_version NOT IN ({legal})
              )
            """,
            (WITCHER3_DEFAULT_VERSION,),
        )
        self._conn.execute(
            f"""
            UPDATE mods SET game_version = NULL
            WHERE app_id NOT IN ({ids})
              AND game_version IS NOT NULL
            """
        )

    def _ensure_witcher3_game_version_default_locked(
        self, mod_id: int, app_id: int = 0
    ) -> None:
        """Witcher 3 ONLY: fill empty game_version with next_gen. Caller holds lock."""
        from core.witcher3_game_version import (
            WITCHER3_DEFAULT_VERSION,
            is_valid_witcher3_game_version,
            is_witcher3_game,
        )

        mid = int(mod_id)
        row = self._conn.execute(
            "SELECT app_id, game_version FROM mods WHERE mod_id = ?",
            (mid,),
        ).fetchone()
        if row is None:
            return
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        if "game_version" not in keys:
            return
        gid = int(app_id or 0) or int(row["app_id"] or 0)
        if not is_witcher3_game("", gid):
            return
        current = str(row["game_version"] or "").strip()
        if is_valid_witcher3_game_version(current):
            return
        self._conn.execute(
            "UPDATE mods SET game_version = ? WHERE mod_id = ?",
            (WITCHER3_DEFAULT_VERSION, mid),
        )

    def _backfill_steam_platform_fields(self) -> None:
        """
        Normalize empty ``mod_files`` only.

        Historical recovery that copied ``mod_id`` digits into ``external_id`` /
        ``source_url`` is forbidden after contiguous PK remapping —
        ``mods.mod_id`` is never Workshop identity.
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
                mod_files = CASE
                    WHEN mod_files IS NULL OR TRIM(mod_files) = '' THEN '{}'
                    ELSE mod_files
                END
            """
        )

    def _backfill_workspace_ids(self) -> None:
        """Assign persistent ``workspace_id`` from platform identity, never Internal ID."""
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "workspace_id" not in cols:
            return
        rows = self._conn.execute(
            """
            SELECT mod_id, platform, source_url, external_id, workspace_id
            FROM mods
            WHERE workspace_id IS NULL OR TRIM(workspace_id) = ''
            """
        ).fetchall()
        if not rows:
            return
        existing = {
            str(r["workspace_id"] or "").strip()
            for r in self._conn.execute(
                "SELECT workspace_id FROM mods "
                "WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) != ''"
            ).fetchall()
        }
        existing.discard("")
        for row in rows:
            mid = str(row["mod_id"])
            plat = normalize_platform_if_known(str(row["platform"] or ""))
            if is_internal_mod_id(mid) and plat in ("", PLATFORM_STEAM):
                continue
            wid = resolve_workspace_id(
                plat,
                source_url=str(row["source_url"] or ""),
                external_id=str(row["external_id"] or ""),
                existing=str(row["workspace_id"] or ""),
            )
            if wid and is_internal_mod_id(wid) and wid == mid:
                wid = ""
            if not wid:
                if plat in (PLATFORM_GITHUB, PLATFORM_MODIO, PLATFORM_OTHER):
                    wid = generate_unique_workspace_id(existing)
                else:
                    continue
            existing.add(wid)
            self._conn.execute(
                "UPDATE mods SET workspace_id = ? WHERE mod_id = ?",
                (wid, int(mid)),
            )

    def _clear_system_inferred_conflict_pollution(self) -> None:
        """One-shot clear of historically polluted ``conflict_status`` values.

        Previous versions incorrectly generated conflict state from system
        inference (FILE_OVERWRITE scans, relationship→status promotion,
        identity_repair writing ``identity_conflict`` into the user column).

        Conflict is user-owned metadata (equivalent to invalid / abandoned).
        All previous generated values are invalid and are cleared once.
        Users may re-mark via Detail Panel flag chips after this migration.

        Idempotent via ``schema_flags.cleared_system_conflict_v1``.
        """
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_flags (
                flag TEXT PRIMARY KEY NOT NULL,
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        done = self._conn.execute(
            "SELECT 1 FROM schema_flags WHERE flag = ?",
            ("cleared_system_conflict_v1",),
        ).fetchone()
        if done is not None:
            return
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "conflict_status" in cols:
            self._conn.execute(
                """
                UPDATE mods
                SET conflict_status = 'none',
                    conflict_note = ''
                WHERE conflict_status IS NOT NULL
                  AND TRIM(conflict_status) != ''
                  AND LOWER(TRIM(conflict_status)) != 'none'
                """
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_flags (flag, applied_at) VALUES (?, ?)",
            ("cleared_system_conflict_v1", _utc_now()),
        )

    def _clear_illegal_content_missing_pollution(self) -> None:
        """One-shot clear of illegally stamped ``content_status=content_missing``.

        Previous versions let library Reconcile (and Import sticky markers)
        write ``content_missing`` from incomplete / shallow payload probes.
        That polluted DB state, appeared under the 「内容缺失」 filter, cleared
        on Detail Refresh, then reappeared on the next background reconcile.

        Content Missing is system-derived and may only be re-established by
        ``services.content_status_eval`` (Refresh). Rows with
        ``folder_present=1`` and ``content_missing`` are reset to healthy;
        authentic empty payloads are re-evaluated on the next Refresh.

        Idempotent via ``schema_flags.cleared_illegal_content_missing_v1``.
        """
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_flags (
                flag TEXT PRIMARY KEY NOT NULL,
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        done = self._conn.execute(
            "SELECT 1 FROM schema_flags WHERE flag = ?",
            ("cleared_illegal_content_missing_v1",),
        ).fetchone()
        if done is not None:
            return
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "content_status" in cols:
            self._conn.execute(
                """
                UPDATE mods
                SET content_status = 'healthy',
                    library_status = CASE
                        WHEN TRIM(COALESCE(library_status, '')) IN (
                            'missing', 'content_missing'
                        ) THEN 'normal'
                        ELSE library_status
                    END
                WHERE LOWER(TRIM(COALESCE(content_status, ''))) = 'content_missing'
                  AND COALESCE(folder_present, 0) = 1
                """
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_flags (flag, applied_at) VALUES (?, ?)",
            ("cleared_illegal_content_missing_v1", _utc_now()),
        )

    def _clear_identity_pollution_from_user_status(self) -> None:
        """
        One-shot: identity facts must not live on user Mod status columns.

        Clears::
          - content_status ∈ {identity_conflict, …} → healthy
          - library_status ∈ {conflict, identity_*} → normal

        Does **not** mass-reset ``identity_status`` (internal fact column).
        Does **not** touch ``conflict_status`` / deploy / overlays.
        Idempotent via ``schema_flags.cleared_identity_user_status_v1``.
        """
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_flags (
                flag TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        done = self._conn.execute(
            "SELECT 1 FROM schema_flags WHERE flag = ?",
            ("cleared_identity_user_status_v1",),
        ).fetchone()
        if done is not None:
            return
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "content_status" in cols:
            self._conn.execute(
                """
                UPDATE mods
                SET content_status = 'healthy'
                WHERE LOWER(TRIM(COALESCE(content_status, ''))) IN (
                    'identity_conflict',
                    'identity_unresolved',
                    'unresolved',
                    'conflict'
                )
                """
            )
        if "library_status" in cols:
            self._conn.execute(
                """
                UPDATE mods
                SET library_status = 'normal'
                WHERE LOWER(TRIM(COALESCE(library_status, ''))) IN (
                    'conflict',
                    'identity_conflict',
                    'identity_unresolved',
                    'unresolved'
                )
                """
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_flags (flag, applied_at) VALUES (?, ?)",
            ("cleared_identity_user_status_v1", _utc_now()),
        )

    def _run_status_recovery_db_phase(self) -> None:
        """Peel identity pollution off content_status into identity_status.

        Full filesystem re-eval runs from reconcile / startup via
        ``services.status_recovery.run_status_recovery``.
        """
        try:
            from services.status_recovery import migrate_identity_pollution_from_content

            migrate_identity_pollution_from_content(self)
        except Exception:  # noqa: BLE001
            logger.debug("status recovery db phase failed", exc_info=True)

    def _ensure_mod_workspace_id_locked(self, mod_id: int) -> str:
        """
        Ensure ``mods.workspace_id`` is set (caller must hold ``_lock``).

        Returns the final workspace_id. Never overwrites a legal non-empty
        value. Never copies Internal ID into Workspace ID.

        ``workspace_id`` is a registration/display number. The same digits may
        appear on multiple rows when ``app_id`` differs (e.g. Nexus 1333 on
        Stardew and BG3). Uniqueness is ``(platform, app_id, workspace_id)``,
        never ``workspace_id`` alone.
        """
        row = self._conn.execute(
            """
            SELECT mod_id, platform, source_url, external_id, workspace_id, app_id
            FROM mods WHERE mod_id = ?
            """,
            (int(mod_id),),
        ).fetchone()
        if row is None:
            return ""
        existing = str(row["workspace_id"] or "").strip()
        plat = normalize_platform_if_known(str(row["platform"] or ""))
        mid = str(row["mod_id"])
        if existing:
            if is_internal_mod_id(mid) and existing == mid:
                existing = ""
            else:
                return existing
        if is_internal_mod_id(mid) and plat in ("", PLATFORM_STEAM):
            return ""
        wid = resolve_workspace_id(
            plat,
            source_url=str(row["source_url"] or ""),
            external_id=str(row["external_id"] or ""),
        )
        if wid and is_internal_mod_id(mid) and wid == mid:
            wid = ""
        if not wid:
            # Steam Workshop display id is the Workshop ID — never invent one.
            if plat in ("", PLATFORM_STEAM):
                return ""
            # Nexus digits come from URL/external — never invent.
            if plat == PLATFORM_NEXUS:
                return ""
            if plat not in (
                PLATFORM_GITHUB,
                PLATFORM_MODIO,
                PLATFORM_OTHER,
            ):
                return ""
            taken = {
                str(r["workspace_id"] or "").strip()
                for r in self._conn.execute(
                    "SELECT workspace_id FROM mods "
                    "WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) != ''"
                    " AND mod_id != ?",
                    (int(mod_id),),
                ).fetchall()
            }
            taken.discard("")
            wid = generate_unique_workspace_id(taken)
        self._conn.execute(
            "UPDATE mods SET workspace_id = ? WHERE mod_id = ?",
            (wid, int(mod_id)),
        )
        return wid

    def correct_nexus_workspace_id_from_url(
        self, mod_id: int | str
    ) -> str | None:
        """
        Silently overwrite ``workspace_id`` from Nexus ``source_url``
        ``/mods/<id>`` when mismatched. Never raises — returns ``None`` on
        any miss / error (safe inside batch refresh loops).
        """
        try:
            mid = int(str(mod_id).strip())
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT platform, source_url, workspace_id
                    FROM mods WHERE mod_id = ?
                    """,
                    (mid,),
                ).fetchone()
                if row is None:
                    return None
                new_wid = corrected_nexus_workspace_id(
                    platform=str(row["platform"] or ""),
                    source_url=str(row["source_url"] or ""),
                    workspace_id=str(row["workspace_id"] or ""),
                )
                if not new_wid:
                    return None
                clash = self._conn.execute(
                    """
                    SELECT mod_id FROM mods
                    WHERE TRIM(COALESCE(workspace_id, '')) = ?
                      AND mod_id != ?
                    LIMIT 1
                    """,
                    (new_wid, mid),
                ).fetchone()
                if clash is not None:
                    # Display id already owned — leave unique assignment alone.
                    return None
                self._conn.execute(
                    """
                    UPDATE mods SET workspace_id = ?
                    WHERE mod_id = ?
                    """,
                    (new_wid, mid),
                )
                self._conn.commit()
                return new_wid
        except Exception:  # noqa: BLE001
            return None

    def _ensure_unique_platform_external_index(self) -> None:
        """
        Enforce UNIQUE(platform, app_id, external_id).

        Duplicate identity rows fail-fast unless ``SMM_IDENTITY_RECOVERY=1``
        (recovery / repair mode). Silent skip is not allowed in normal runs.
        """
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        if "platform" not in cols or "external_id" not in cols or "app_id" not in cols:
            return

        self._conn.execute("DROP INDEX IF EXISTS uq_mods_platform_external")

        dup_rows = self._conn.execute(
            """
            SELECT platform, app_id, external_id, COUNT(*) AS cnt,
                   GROUP_CONCAT(mod_id) AS mod_ids
            FROM mods
            WHERE TRIM(COALESCE(external_id, '')) != ''
            GROUP BY platform, app_id, external_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        recovery = os.environ.get("SMM_IDENTITY_RECOVERY", "").strip() in {
            "1",
            "true",
            "TRUE",
            "yes",
            "YES",
        }
        if dup_rows:
            details = [
                {
                    "platform": row["platform"],
                    "app_id": row["app_id"],
                    "external_id": row["external_id"],
                    "count": row["cnt"],
                    "mod_ids": row["mod_ids"],
                }
                for row in dup_rows
            ]
            for item in details:
                logger.error(
                    "Duplicate mod identity platform=%r app_id=%s external_id=%r "
                    "count=%s mod_ids=%s",
                    item["platform"],
                    item["app_id"],
                    item["external_id"],
                    item["count"],
                    item["mod_ids"],
                )
            if not recovery:
                raise IdentityIntegrityError(
                    "Identity UNIQUE conflict detected; refuse to start. "
                    "Set SMM_IDENTITY_RECOVERY=1 and run identity repair, "
                    f"or resolve manually. conflicts={details!r}"
                )
            logger.warning(
                "SMM_IDENTITY_RECOVERY=1: continuing without UNIQUE index "
                "(%s conflict group(s))",
                len(details),
            )
            return

        self._conn.execute("DROP INDEX IF EXISTS idx_mods_external")
        self._conn.execute("DROP INDEX IF EXISTS uq_mods_platform_app_external")
        try:
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_mods_platform_app_external
                ON mods(platform, app_id, external_id)
                WHERE TRIM(COALESCE(external_id, '')) != ''
                """
            )
        except sqlite3.IntegrityError as exc:
            if recovery:
                logger.warning(
                    "SMM_IDENTITY_RECOVERY=1: UNIQUE index create failed: %s",
                    exc,
                )
                return
            raise IdentityIntegrityError(
                f"Failed to create uq_mods_platform_app_external: {exc}"
            ) from exc

    def _ensure_deployment_record_name_unique(self) -> None:
        """Ensure UNIQUE(app_id, name) for deployment records (idempotent)."""
        tables = {
            str(r[0])
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "deployment_records" not in tables:
            return
        dup_rows = self._conn.execute(
            """
            SELECT app_id, LOWER(name) AS nkey, COUNT(*) AS cnt
            FROM deployment_records
            WHERE TRIM(COALESCE(name, '')) != ''
            GROUP BY app_id, LOWER(name)
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        if dup_rows:
            for row in dup_rows:
                logger.warning(
                    "Duplicate deployment record name app_id=%s name_key=%r "
                    "count=%s (UNIQUE index not applied; resolve manually)",
                    row["app_id"],
                    row["nkey"],
                    row["cnt"],
                )
            return
        try:
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_deployment_records_app_name
                ON deployment_records(app_id, name COLLATE NOCASE)
                """
            )
        except sqlite3.IntegrityError as exc:
            logger.warning(
                "Failed to create uq_deployment_records_app_name: %s",
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

    def list_game_sidebar_aggregates(self) -> list[dict[str, Any]]:
        """
        Library sidebar source: ``games`` + ``mods`` counts (SQL only).

        ARCHITECTURE RULE: Library Read Projection. Callers must not merge
        filesystem ``list_games`` / ``iterdir`` into this result.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    g.app_id AS app_id,
                    g.name AS name,
                    COUNT(m.mod_id) AS mod_count,
                    COALESCE(SUM(CASE WHEN COALESCE(m.folder_present, 0) = 0
                                      THEN 1 ELSE 0 END), 0) AS absent_count
                FROM games AS g
                LEFT JOIN mods AS m ON m.app_id = g.app_id
                WHERE g.app_id > 0
                GROUP BY g.app_id, g.name
                ORDER BY g.name COLLATE NOCASE
                """
            ).fetchall()
            # Mods whose app_id is missing from games still need a sidebar key.
            orphan_rows = self._conn.execute(
                """
                SELECT
                    m.app_id AS app_id,
                    '' AS name,
                    COUNT(m.mod_id) AS mod_count,
                    COALESCE(SUM(CASE WHEN COALESCE(m.folder_present, 0) = 0
                                      THEN 1 ELSE 0 END), 0) AS absent_count,
                    MAX(m.last_known_path) AS sample_path
                FROM mods AS m
                LEFT JOIN games AS g ON g.app_id = m.app_id
                WHERE m.app_id > 0 AND g.app_id IS NULL
                GROUP BY m.app_id
                """
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            app_id = int(row["app_id"] or 0)
            name = str(row["name"] or "").strip()
            folder = sanitize_folder_name(name, fallback=f"App_{app_id}")
            out.append(
                {
                    "app_id": app_id,
                    "name": name,
                    "folder": folder,
                    "mod_count": int(row["mod_count"] or 0),
                    "absent_count": int(row["absent_count"] or 0),
                }
            )
        from pathlib import Path as _Path

        for row in orphan_rows:
            app_id = int(row["app_id"] or 0)
            sample = str(row["sample_path"] or "").strip()
            folder = ""
            if sample:
                try:
                    folder = _Path(sample).parent.name
                except Exception:  # noqa: BLE001
                    folder = ""
            if not folder:
                folder = f"App_{app_id}"
            out.append(
                {
                    "app_id": app_id,
                    "name": folder,
                    "folder": folder,
                    "mod_count": int(row["mod_count"] or 0),
                    "absent_count": int(row["absent_count"] or 0),
                }
            )
        return out

    def resolve_game_id_for_folder(self, game_folder: str | None) -> int | None:
        """Map library folder name → ``games.app_id`` via SQL (no FS scan)."""
        key = str(game_folder or "").strip()
        if not key:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT app_id, name FROM games WHERE app_id > 0"
            ).fetchall()
        for row in rows:
            app_id = int(row["app_id"] or 0)
            name = str(row["name"] or "").strip()
            folder = sanitize_folder_name(name, fallback=f"App_{app_id}")
            if key == folder or key == name:
                return app_id
        return None

    # ------------------------------------------------------------------
    # Games — deploy configuration (never overwritten by Steam upsert)
    # ------------------------------------------------------------------

    def get_game_deploy_config(self, game_id: int | str) -> GameDeployConfig | None:
        """Return deploy paths for ``app_id`` (``game_id``), or None if missing."""
        app_id = int(game_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT app_id, name, install_path, mod_path, deploy_type,
                       workshop_path
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
        workshop_path: str | None = None,
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
                SELECT app_id, name, install_path, mod_path, deploy_type,
                       workshop_path
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
                new_workshop = (
                    "" if workshop_path is None else str(workshop_path).strip()
                )
                self._conn.execute(
                    """
                    INSERT INTO games (
                        app_id, name, header_url, description,
                        install_path, mod_path, deploy_type, workshop_path,
                        updated_at
                    )
                    VALUES (?, ?, '', '', ?, ?, ?, ?, ?)
                    """,
                    (
                        app_id,
                        new_name,
                        new_install,
                        new_mod,
                        new_type,
                        new_workshop,
                        _utc_now(),
                    ),
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
                keys = set(row.keys())
                prev_workshop = (
                    str(row["workshop_path"] or "") if "workshop_path" in keys else ""
                )
                new_workshop = (
                    prev_workshop
                    if workshop_path is None
                    else str(workshop_path).strip()
                )
                self._conn.execute(
                    """
                    UPDATE games SET
                        name = ?,
                        install_path = ?,
                        mod_path = ?,
                        deploy_type = ?,
                        workshop_path = ?,
                        updated_at = ?
                    WHERE app_id = ?
                    """,
                    (
                        new_name,
                        new_install,
                        new_mod,
                        new_type,
                        new_workshop,
                        _utc_now(),
                        app_id,
                    ),
                )
            self._conn.commit()
            out = self._conn.execute(
                """
                SELECT app_id, name, install_path, mod_path, deploy_type,
                       workshop_path
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

    def resolve_steam_entity_mod_id(
        self,
        workshop_id: str | int = "",
        *,
        app_id: int = 0,
        internal_id: str | int = "",
    ) -> str | None:
        """Map Steam Workshop ID → existing ``mods.mod_id``. Never creates rows.

        Resolution order:
        1. Explicit ``internal_id`` when that PK exists
        2. ``(platform=steam, app_id, external_id|workspace_id)`` when app_id > 0
        3. Unambiguous steam ``external_id`` / ``workspace_id`` match
        4. Legacy coincidence: ``mod_id == workshop_id`` steam-range row
        """
        iid = str(internal_id or "").strip()
        if iid.isdigit():
            with self._lock:
                hit = self._conn.execute(
                    "SELECT 1 FROM mods WHERE mod_id = ?",
                    (int(iid),),
                ).fetchone()
            if hit is not None:
                return iid

        wid = str(workshop_id or "").strip()
        if not wid.isdigit():
            return None
        if int(wid) >= NON_STEAM_MOD_ID_BASE:
            return None
        aid = int(app_id or 0)

        def _one(sql: str, params: tuple[Any, ...]) -> str | None:
            row = self._conn.execute(sql, params).fetchone()
            if row is None:
                return None
            mid = str(row["mod_id"] or "").strip()
            return mid if mid.isdigit() else None

        with self._lock:
            if aid > 0:
                for col in ("external_id", "workspace_id"):
                    found = _one(
                        f"""
                        SELECT mod_id FROM mods
                        WHERE platform = ?
                          AND app_id = ?
                          AND TRIM(COALESCE({col}, '')) = ?
                        LIMIT 1
                        """,
                        (PLATFORM_STEAM, aid, wid),
                    )
                    if found:
                        return found

            for col in ("external_id", "workspace_id"):
                rows = self._conn.execute(
                    f"""
                    SELECT mod_id, app_id FROM mods
                    WHERE platform = ?
                      AND TRIM(COALESCE({col}, '')) = ?
                    """,
                    (PLATFORM_STEAM, wid),
                ).fetchall()
                if len(rows) == 1:
                    mid = str(rows[0]["mod_id"] or "").strip()
                    if mid.isdigit():
                        return mid
                if aid > 0 and rows:
                    matched = [
                        str(r["mod_id"])
                        for r in rows
                        if int(r["app_id"] or 0) == aid
                        and str(r["mod_id"] or "").strip().isdigit()
                    ]
                    if len(matched) == 1:
                        return matched[0]

            # Legacy Steam PK == Workshop digits — only when platform identity
            # on that row also agrees (post-continuity PK is never Workshop).
            legacy = self._conn.execute(
                """
                SELECT mod_id, platform, external_id, workspace_id FROM mods
                WHERE mod_id = ?
                """,
                (int(wid),),
            ).fetchone()
            if legacy is not None:
                plat = normalize_platform_if_known(str(legacy["platform"] or ""))
                ext = str(legacy["external_id"] or "").strip()
                ws = str(legacy["workspace_id"] or "").strip()
                if plat in ("", PLATFORM_STEAM) and (ext == wid or ws == wid):
                    return str(legacy["mod_id"])
        return None

    def upsert_mod(self, meta: ModMetadata, *, allow_insert: bool | None = None) -> None:
        """
        Update Steam catalog fields on an **existing** entity.

        Write boundary (Identity Contract)::

            published_file_id  — Steam Workshop ID only (source association)
            mods.mod_id        — resolved Internal PK; never overwritten

        Flow: resolve existing entity → UPDATE by ``mods.mod_id``.
        Catalog / Steam API paths must not INSERT. INSERT is allowed only when
        ``allow_insert=True`` or an IdentityService create scope is active
        (Sync/Import ``create_mod_identity``). New rows receive a contiguous
        local ``mods.mod_id``; Workshop ID is stored only on
        ``external_id`` / ``workspace_id``.
        """
        workshop = str(meta.published_file_id or "").strip()
        if not workshop.isdigit():
            return
        if not meta.title:
            return
        if int(workshop) >= NON_STEAM_MOD_ID_BASE:
            return

        entity = self.resolve_steam_entity_mod_id(
            workshop,
            app_id=int(meta.app_id or 0),
            internal_id=str(meta.internal_id or "").strip(),
        )
        if entity is not None:
            mid = int(entity)
            source = steam_workshop_url(workshop)
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        app_id = CASE
                            WHEN ? > 0 THEN ?
                            ELSE app_id
                        END,
                        title = ?,
                        preview_url = ?,
                        description = ?,
                        platform = CASE
                            WHEN platform IS NULL OR TRIM(platform) = ''
                            THEN ?
                            ELSE platform
                        END,
                        source_url = CASE
                            WHEN source_url IS NULL OR TRIM(source_url) = ''
                            THEN ?
                            ELSE source_url
                        END,
                        external_id = CASE
                            WHEN external_id IS NULL OR TRIM(external_id) = ''
                            THEN ?
                            ELSE external_id
                        END,
                        workspace_id = CASE
                            WHEN workspace_id IS NULL OR TRIM(workspace_id) = ''
                            THEN ?
                            ELSE workspace_id
                        END
                    WHERE mod_id = ?
                    """,
                    (
                        int(meta.app_id or 0),
                        int(meta.app_id or 0),
                        meta.title,
                        meta.preview_url or "",
                        meta.description or "",
                        PLATFORM_STEAM,
                        source,
                        workshop,
                        workshop,
                        mid,
                    ),
                )
                self._ensure_witcher3_game_version_default_locked(
                    mid, int(meta.app_id or 0)
                )
                self._conn.commit()
            return

        if allow_insert is None:
            from services.identity_service import (
                LIFECYCLE_IMPORT,
                LIFECYCLE_RECONCILE,
                LIFECYCLE_SYNC,
                current_lifecycle,
                is_internal_create_allowed,
            )

            life = current_lifecycle()
            if life == LIFECYCLE_RECONCILE:
                allow_insert = False
            else:
                # Empty lifecycle must NOT auto-create (closes Steam API PK mint).
                allow_insert = is_internal_create_allowed() or life in (
                    LIFECYCLE_IMPORT,
                    LIFECYCLE_SYNC,
                )
        if not allow_insert:
            logger.warning(
                "upsert_mod refused INSERT workshop=%s (no existing entity; "
                "Steam published_file_id is not mods.mod_id)",
                workshop,
            )
            return

        # IdentityService create only — allocate contiguous local PK.
        # Workshop ID binds to external_id / workspace_id only.
        mid = int(self.allocate_mod_id())
        source = steam_workshop_url(workshop)
        with self._lock:
            self._conn.execute(
                """
                UPDATE mods SET
                    app_id = ?,
                    title = ?,
                    preview_url = ?,
                    description = ?,
                    platform = ?,
                    source_url = ?,
                    external_id = ?,
                    workspace_id = ?,
                    updated_at = ?
                WHERE mod_id = ?
                """,
                (
                    int(meta.app_id or 0),
                    meta.title,
                    meta.preview_url or "",
                    meta.description or "",
                    PLATFORM_STEAM,
                    source,
                    workshop,
                    workshop,
                    _utc_now(),
                    mid,
                ),
            )
            self._ensure_witcher3_game_version_default_locked(
                mid, int(meta.app_id or 0)
            )
            self._conn.commit()

    def upsert_mods(self, metas: Iterable[ModMetadata]) -> int:
        """Batch Steam **update** for existing rows only — never INSERT.

        Resolves each Workshop ``published_file_id`` to ``mods.mod_id`` first.
        Entity create is Sync/Import via IdentityService only.
        """
        rows: list[tuple[Any, ...]] = []
        for meta in metas:
            workshop = str(meta.published_file_id or "").strip()
            if not workshop.isdigit() or not meta.title:
                continue
            if int(workshop) >= NON_STEAM_MOD_ID_BASE:
                continue
            entity = self.resolve_steam_entity_mod_id(
                workshop,
                app_id=int(meta.app_id or 0),
                internal_id=str(meta.internal_id or "").strip(),
            )
            if entity is None:
                logger.warning(
                    "upsert_mods skipped workshop=%s (no existing entity)",
                    workshop,
                )
                continue
            mid = int(entity)
            rows.append(
                (
                    int(meta.app_id or 0),
                    meta.title,
                    meta.preview_url or "",
                    meta.description or "",
                    PLATFORM_STEAM,
                    steam_workshop_url(workshop),
                    workshop,
                    workshop,
                    mid,
                )
            )
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """
                UPDATE mods SET
                    app_id = CASE WHEN ? > 0 THEN ? ELSE app_id END,
                    title = ?,
                    preview_url = ?,
                    description = ?,
                    platform = CASE
                        WHEN platform IS NULL OR TRIM(platform) = ''
                        THEN ?
                        ELSE platform
                    END,
                    source_url = CASE
                        WHEN source_url IS NULL OR TRIM(source_url) = ''
                        THEN ?
                        ELSE source_url
                    END,
                    external_id = CASE
                        WHEN external_id IS NULL OR TRIM(external_id) = ''
                        THEN ?
                        ELSE external_id
                    END,
                    workspace_id = CASE
                        WHEN workspace_id IS NULL OR TRIM(workspace_id) = ''
                        THEN ?
                        ELSE workspace_id
                    END
                WHERE mod_id = ?
                """,
                [
                    (
                        app_id,
                        app_id,
                        title,
                        preview,
                        desc,
                        plat,
                        source,
                        ext,
                        ws,
                        mid,
                    )
                    for (
                        app_id,
                        title,
                        preview,
                        desc,
                        plat,
                        source,
                        ext,
                        ws,
                        mid,
                    ) in rows
                ],
            )
            updated = int(self._conn.total_changes - before)
            self._conn.commit()
            return updated

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
        Allocate the next local SQLite ``mods.mod_id`` PK.

        Contiguous handle only: ``MAX(mod_id) + 1``. Never derived from
        Workshop ID, Nexus ID, workspace_id, or correlation_id.

        Inserts a provisional stub row immediately so consecutive allocations
        never return the same id before the caller persists identity fields.
        """
        from services.identity_service import (
            assert_lifecycle_may_allocate,
            identity_create_scope,
        )

        assert_lifecycle_may_allocate()
        with identity_create_scope(), self._lock:
            row = self._conn.execute(
                "SELECT MAX(mod_id) AS mx FROM mods"
            ).fetchone()
            mx = int(row["mx"] or 0) if row is not None else 0
            next_id = mx + 1 if mx > 0 else 1
            self._ensure_mod_stub(next_id)
            self._conn.commit()
            return next_id

    def find_mod_by_internal_id(self, internal_id: str) -> str | None:
        key = str(internal_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE TRIM(internal_id) = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["mod_id"])

    def find_mod_by_workspace_id(
        self,
        workspace_id: str,
        *,
        platform: str | None = None,
        app_id: int = 0,
    ) -> str | None:
        """
        REMOVED as an identity API — always returns ``None``.

        ``workspace_id`` is never a general entity key. Sync/Import registration
        must use :meth:`find_mod_for_registration` with ``(platform, app_id,
        workspace_id)`` and ``app_id > 0``.
        """
        _ = (workspace_id, platform, app_id)
        return None

    def find_mod_for_registration(
        self,
        platform: str,
        app_id: int,
        workspace_id: str,
    ) -> ModDisplayInfo | None:
        """
        Sync / Import registration lookup only.

        Match ``(platform, app_id, workspace_id)`` with ``app_id > 0``.
        Never call from Reconcile / Backup / Deploy / Library / UI.
        """
        plat = normalize_platform(platform)
        wid = str(workspace_id or "").strip()
        aid = int(app_id or 0)
        if not plat or not wid or aid <= 0:
            return None
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {_MOD_SELECT_COLS} FROM mods
                WHERE platform = ?
                  AND app_id = ?
                  AND TRIM(COALESCE(workspace_id, '')) = ?
                LIMIT 1
                """,
                (plat, aid, wid),
            ).fetchone()
        if row is None:
            return None
        return _display_info_from_row(row)

    def resolve_mod_id_by_scoped_workspace(
        self,
        *,
        platform: str,
        app_id: int,
        workspace_id: str,
    ) -> str | None:
        """
        Map a platform workspace_id to ``mods.mod_id`` within one game.

        Match ``(platform, app_id, workspace_id)`` with ``app_id > 0``.
        Returns the Internal Database ID (PK) string, or ``None``.

        Used to translate metadata dependency tokens (stored as workspace_id)
        into runtime PKs. Never treats ``workspace_id`` as a cross-game PK.
        """
        plat = normalize_platform(platform)
        wid = str(workspace_id or "").strip()
        aid = int(app_id or 0)
        if not plat or not wid or aid <= 0:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE platform = ?
                  AND app_id = ?
                  AND TRIM(COALESCE(workspace_id, '')) = ?
                LIMIT 1
                """,
                (plat, aid, wid),
            ).fetchone()
        if row is None:
            return None
        mid = str(row["mod_id"] or "").strip()
        return mid if mid.isdigit() else None

    def update_mod_content_status(
        self,
        mod_id: int | str,
        *,
        content_status: str,
        library_status: str | None = None,
        folder_present: bool | None = None,
        last_known_path: str | None = None,
        touch_updated_at: bool = False,
    ) -> bool:
        """
        Persist system content-axis status.

        ARCHITECTURE RULE: only ``services.content_status_eval`` may call this.
        Identity / Sync / Deploy / Repair must not write ``content_status``.

        ``touch_updated_at`` must stay False — content eval is a forbidden
        ``updated_at`` reason (see ``services.updated_at_authority``).

        Returns True when any written column actually changed.
        """
        from services.library_status import content_status_to_library_status
        from services.status_authority import SUPPORTED_CONTENT_STATUSES
        from services.updated_at_authority import UpdatedAtAuthorityError

        if touch_updated_at:
            raise UpdatedAtAuthorityError(
                "update_mod_content_status must not touch mods.updated_at "
                "(reason would be content_eval — forbidden)"
            )

        mid = int(str(mod_id).strip())
        cs = str(content_status or "").strip().lower()
        if cs not in SUPPORTED_CONTENT_STATUSES:
            raise ValueError(
                f"illegal content_status={content_status!r}; "
                f"allowed={SUPPORTED_CONTENT_STATUSES}"
            )
        ls = (
            str(library_status).strip()
            if library_status is not None
            else content_status_to_library_status(cs)
        )
        sets: list[str] = [
            "content_status = ?",
            "library_status = ?",
        ]
        params: list[Any] = [cs, ls]
        if folder_present is not None:
            sets.append("folder_present = ?")
            params.append(1 if folder_present else 0)
        if last_known_path is not None:
            sets.append("last_known_path = ?")
            params.append(str(last_known_path or "").strip())
        params.append(mid)
        with self._lock:
            prev = self._conn.execute(
                """
                SELECT content_status, library_status, folder_present, last_known_path
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
            if prev is None:
                logger.warning(
                    "update_mod_content_status refused: no mods row for %s", mid
                )
                return False
            changed = False
            if str(prev["content_status"] or "").strip().lower() != cs:
                changed = True
            if str(prev["library_status"] or "").strip() != ls:
                changed = True
            if folder_present is not None:
                if int(prev["folder_present"] or 0) != (1 if folder_present else 0):
                    changed = True
            if last_known_path is not None:
                if str(prev["last_known_path"] or "").strip() != str(
                    last_known_path or ""
                ).strip():
                    changed = True
            if not changed:
                return False
            self._conn.execute(
                f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                tuple(params),
            )
            self._conn.commit()
            return True

    def update_mod_identity_fields(
        self,
        mod_id: int | str,
        *,
        internal_id: str | None = None,
        source_type: str | None = None,
        identity_status: str | None = None,
        last_known_path: str | None = None,
        folder_present: bool | None = None,
        game_name: str | None = None,
        title: str | None = None,
        platform: str | None = None,
        source_url: str | None = None,
        external_id: str | None = None,
        workspace_id: str | None = None,
        app_id: int | None = None,
        sticky_source: bool = True,
        content_status: str | None = None,
        library_status: str | None = None,
    ) -> None:
        """Patch identity / path fields (never content_status)."""
        if content_status is not None or library_status is not None:
            raise TypeError(
                "content_status/library_status must be written via "
                "update_mod_content_status (content_status_eval only)"
            )
        mid = int(str(mod_id).strip())
        # Identity / path / platform patches must not bump Library sort time.
        sets: list[str] = []
        params: list[Any] = []
        if internal_id is not None:
            sets.append("internal_id = ?")
            params.append(str(internal_id or "").strip())
        if identity_status is not None:
            from services.status_authority import normalize_identity_status

            sets.append("identity_status = ?")
            params.append(normalize_identity_status(identity_status))
        if source_type is not None and str(source_type).strip():
            src = str(source_type).strip().lower()
            if sticky_source:
                sets.append(
                    "source_type = CASE "
                    "WHEN TRIM(COALESCE(source_type, '')) = '' THEN ? "
                    "ELSE source_type END"
                )
            else:
                sets.append("source_type = ?")
            params.append(src)
        if last_known_path is not None:
            sets.append("last_known_path = ?")
            params.append(str(last_known_path or "").strip())
        if folder_present is not None:
            sets.append("folder_present = ?")
            params.append(1 if folder_present else 0)
        if game_name is not None:
            # game_name lives on games join; store via title/app only when needed
            pass
        if title is not None and str(title).strip():
            sets.append("title = COALESCE(NULLIF(TRIM(title), ''), ?)")
            params.append(str(title).strip())
        if platform is not None and str(platform).strip():
            sets.append("platform = ?")
            params.append(normalize_platform(platform))
        if source_url is not None:
            from services.mod_identity import source_url_embeds_internal

            url_text = str(source_url or "").strip()
            if source_url_embeds_internal(url_text, internal_pk=str(mid)):
                source_url = None
            else:
                sets.append("source_url = ?")
                params.append(url_text)
        ext_param_set = False
        if external_id is not None:
            ext_val = str(external_id or "").strip()
            if is_modio_external_id_pollution(ext_val, mod_id=mid) or is_internal_mod_id(
                ext_val
            ):
                ext_val = ""
            if ext_val:
                sets.append("external_id = ?")
                params.append(ext_val)
                ext_param_set = True
        if (
            platform is not None
            and normalize_platform(platform) == PLATFORM_MODIO
            and not ext_param_set
        ):
            sets.append(
                "external_id = CASE "
                "WHEN mod_id >= ? AND TRIM(COALESCE(external_id, '')) = CAST(mod_id AS TEXT) "
                "THEN '' ELSE external_id END"
            )
            params.append(NON_STEAM_MOD_ID_BASE)
        if workspace_id is not None:
            ws_val = str(workspace_id or "").strip()
            if ws_val and is_internal_mod_id(str(mid)) and ws_val == str(mid):
                ws_val = ""
            sets.append("workspace_id = ?")
            params.append(ws_val)
        if app_id is not None and int(app_id) > 0:
            sets.append("app_id = ?")
            params.append(int(app_id))
        if not sets:
            return
        params.append(mid)
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if present is None:
                logger.warning(
                    "update_mod_identity_fields refused: no mods row for %s",
                    mid,
                )
                return
            self._conn.execute(
                f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                tuple(params),
            )
            self._conn.commit()

    def find_mod_by_external(
        self,
        platform: str,
        external_id: str,
        *,
        app_id: int = 0,
    ) -> ModDisplayInfo | None:
        """
        DEPRECATED identity API.

        Retained as a thin alias of :meth:`find_mod_for_registration` where
        *external_id* digits are treated as ``workspace_id``. New code must
        call ``find_mod_for_registration`` directly. Do not use from Reconcile /
        Backup / Deploy / Library / UI.
        """
        return self.find_mod_for_registration(platform, int(app_id or 0), str(external_id or ""))

    def find_mod_by_last_known_path(self, path: str) -> str | None:
        """Return ``mod_id`` when *path* matches ``last_known_path`` exactly."""
        row = self.get_mod_backup_row_by_path(str(path or "").strip())
        if row is None:
            return None
        mid = str(row.get("mod_id") or "").strip()
        return mid if mid.isdigit() else None

    def touch_mod_updated_at(self, mod_id: int | str, *, reason: str) -> None:
        """
        Sole intentional bump of ``mods.updated_at`` for an existing row.

        Requires an explicit legal *reason* (``user_edit`` / ``user_import`` /
        ``user_create``). Forbidden system reasons raise
        :class:`UpdatedAtAuthorityError`.
        """
        mid = int(str(mod_id).strip())
        sets: list[str] = []
        params: list[Any] = []
        _append_mods_updated_at(sets, params, reason=reason)
        params.append(mid)
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if present is None:
                return
            self._conn.execute(
                f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                tuple(params),
            )
            self._conn.commit()

    def update_mod_platform_info(
        self,
        mod_id: int | str,
        *,
        platform: str | None = None,
        source_url: str | None = None,
        external_id: str | None = None,
        title: str | None = None,
        app_id: int | None = None,
        description: str | None = None,
        preview_url: str | None = None,
        touch_updated_at: bool = False,
        updated_at_reason: str | None = None,
    ) -> ModDisplayInfo:
        """
        Create or update platform identity fields (never writes ``.info``).

        Changing ``platform`` / ``external_id`` is allowed only when the new
        ``(platform, app_id, external_id)`` triple is free (or is this same row).

        Optional ``description`` / ``preview_url`` update remote catalog text
        (Steam / Mod.io refresh write official fields via this method —
        never ``upsert_mod(published_file_id=workshop)`` after Identity split).

        Default: does **not** bump ``mods.updated_at`` (refresh / identity
        authority). Pass ``touch_updated_at=True`` with a legal reason for
        explicit user edits only.
        """
        mid = int(str(mod_id).strip())
        with self._lock:
            self._ensure_mod_stub(mid)
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            assert row is not None
            old_plat = normalize_platform_if_known(str(row["platform"] or ""))
            old_ext = str(row["external_id"] or "").strip()
            plat = (
                normalize_platform_if_known(platform)
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
            if plat == PLATFORM_MODIO and is_modio_external_id_pollution(ext, mod_id=mid):
                ext = ""
            if ext and is_internal_mod_id(ext):
                ext = ""
            new_title = (
                str(title).strip()
                if title is not None
                else str(row["title"] or "")
            )
            new_app = int(app_id) if app_id is not None else int(row["app_id"] or 0)
            new_description = (
                str(description)
                if description is not None
                else str(row["description"] or "")
            )
            new_preview = (
                str(preview_url).strip()
                if preview_url is not None
                else str(row["preview_url"] or "")
            )

            if plat != old_plat or ext != old_ext or new_app != int(row["app_id"] or 0):
                if not ext:
                    if not (
                        plat == PLATFORM_MODIO
                        and is_modio_external_id_pollution(old_ext, mod_id=mid)
                    ):
                        raise ValueError(
                            "external_id is required when changing platform identity"
                        )
                conflict = self._conn.execute(
                    """
                    SELECT mod_id FROM mods
                    WHERE platform = ? AND app_id = ? AND external_id = ?
                      AND mod_id != ?
                    LIMIT 1
                    """,
                    (plat, new_app, ext, mid),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        f"Mod identity already exists: platform={plat} "
                        f"app_id={new_app} external_id={ext} "
                        f"(mod_id={conflict['mod_id']})"
                    )

            try:
                sets: list[str] = [
                    "platform = ?",
                    "source_url = ?",
                    "external_id = ?",
                    "title = CASE WHEN ? != '' THEN ? ELSE title END",
                    "app_id = ?",
                ]
                params: list[Any] = [
                    plat,
                    url,
                    ext,
                    new_title,
                    new_title,
                    new_app,
                ]
                if description is not None or preview_url is not None:
                    sets.extend(["description = ?", "preview_url = ?"])
                    params.extend([new_description, new_preview])
                if touch_updated_at:
                    _append_mods_updated_at(
                        sets,
                        params,
                        reason=str(updated_at_reason or "user_edit"),
                    )
                params.append(mid)
                self._conn.execute(
                    f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                    tuple(params),
                )
                self._ensure_mod_workspace_id_locked(mid)
                from core.witcher3_game_version import is_witcher3_game

                if is_witcher3_game("", new_app):
                    self._ensure_witcher3_game_version_default_locked(mid, new_app)
                else:
                    self._conn.execute(
                        "UPDATE mods SET game_version = NULL WHERE mod_id = ?",
                        (mid,),
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

    def batch_update_platform(
        self,
        mod_ids: Sequence[int | str],
        platform: str,
        *,
        touch_updated_at: bool = True,
        updated_at_reason: str = "user_edit",
    ) -> int:
        """
        Update only ``mods.platform`` for many Mods.

        Never touches display_name / custom_description / source_url / external_id.
        Returns the number of rows updated.

        Default bumps ``updated_at`` with ``user_edit`` (Detail Panel batch).
        """
        plat = normalize_platform(platform)
        ids: list[int] = []
        for raw in mod_ids:
            text = str(raw or "").strip()
            if text.isdigit():
                mid = int(text)
                if mid not in ids:
                    ids.append(mid)
        if not ids:
            return 0
        updated = 0
        with self._lock:
            for mid in ids:
                sets: list[str] = ["platform = ?"]
                params: list[Any] = [plat]
                if touch_updated_at:
                    _append_mods_updated_at(
                        sets, params, reason=updated_at_reason
                    )
                params.append(mid)
                cur = self._conn.execute(
                    f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                    tuple(params),
                )
                updated += int(cur.rowcount or 0)
            self._conn.commit()
        return updated

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
        bundle = ModFilesBundle.from_json(str(row["mod_files"] or ""))
        # 清剿旧缓存：剔除「历史版本」漏网条目并写回
        from services.importers.local_scanner import (
            filter_out_history_version_entries,
        )

        cleaned = filter_out_history_version_entries(bundle.files)
        if len(cleaned) != len(bundle.files):
            bundle.files = list(cleaned)
            try:
                self.set_mod_files(mid, bundle)
            except Exception:  # noqa: BLE001
                pass
        return bundle

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
        from services.importers.local_scanner import filter_out_history_version_entries

        parsed.files = list(filter_out_history_version_entries(parsed.files))
        payload = parsed.to_json()
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET mod_files = ?
                WHERE mod_id = ?
                """,
                (payload, mid),
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

        Omitted kwargs leave existing values. ``offline_updated_at`` defaults
        to now when any field is written. Does **not** bump ``mods.updated_at``
        (offline is a forbidden reason).
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
                    offline_updated_at = ?
                WHERE mod_id = ?
                """,
                (new_status, new_provider, new_updated, mid),
            )
            self._conn.commit()

    def update_mod_cover_path(self, mod_id: int | str, cover_path: str = "") -> None:
        """Set ``mods.cover_path`` (relative ``.info/cover.ext`` or empty).

        Does not bump ``mods.updated_at`` — cover sync / offline merge is not
        a user Library-sort event (call ``touch_mod_updated_at`` from user save).
        """
        mid = int(str(mod_id).strip())
        value = str(cover_path or "").strip()
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET cover_path = ?
                WHERE mod_id = ?
                """,
                (value, mid),
            )
            self._conn.commit()

    def update_mod_backup_snapshot(
        self,
        mod_id: int | str,
        *,
        last_known_path: str,
        folder_present: bool,
        backup_metadata_json: str,
        backup_cover_path: str = "",
        backup_offline_path: str = "",
    ) -> None:
        """Persist metadata backup fields after ``sync_metadata_backup``."""
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            # Backup completion must not bump ``updated_at`` — Library sort
            # authority is user/import/metadata lifecycle, not async backup.
            self._conn.execute(
                """
                UPDATE mods SET
                    last_known_path = ?,
                    folder_present = ?,
                    backup_updated_at = ?,
                    backup_metadata_json = ?,
                    backup_cover_path = ?,
                    backup_offline_path = ?
                WHERE mod_id = ?
                """,
                (
                    str(last_known_path or "").strip(),
                    1 if folder_present else 0,
                    now,
                    str(backup_metadata_json or ""),
                    str(backup_cover_path or "").strip(),
                    str(backup_offline_path or "").strip(),
                    mid,
                ),
            )
            self._conn.commit()

    def update_mod_backup_offline_path(
        self,
        mod_id: int | str,
        backup_offline_path: str = "",
    ) -> None:
        """Update Backup offline path only. Never rewrites identity or metadata."""
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET
                    backup_updated_at = ?,
                    backup_offline_path = ?
                WHERE mod_id = ?
                """,
                (now, str(backup_offline_path or "").strip(), mid),
            )
            self._conn.commit()

    def update_mod_backup_status(
        self,
        mod_id: int | str,
        *,
        status: str,
        touch_validate_at: bool = True,
    ) -> None:
        """Set ``backup_status`` / optional ``backup_last_validate_at``."""
        mid = int(str(mod_id).strip())
        value = str(status or "").strip()
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(mid)
            if touch_validate_at:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        backup_status = ?,
                        backup_last_validate_at = ?
                    WHERE mod_id = ?
                    """,
                    (value, now, mid),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        backup_status = ?
                    WHERE mod_id = ?
                    """,
                    (value, mid),
                )
            self._conn.commit()

    def set_mods_folder_present(
        self, mod_ids: Iterable[int | str], *, present: bool
    ) -> int:
        """Batch ``folder_present`` write. Never allocates stubs."""
        ids: list[int] = []
        for raw in mod_ids:
            text = str(raw or "").strip()
            if text.isdigit():
                ids.append(int(text))
        if not ids:
            return 0
        flag = 1 if present else 0
        with self._lock:
            self._conn.executemany(
                "UPDATE mods SET folder_present = ? WHERE mod_id = ?",
                [(flag, mid) for mid in ids],
            )
            self._conn.commit()
        return len(ids)

    def set_mod_folder_present(self, mod_id: int | str, *, present: bool) -> None:
        mid = int(str(mod_id).strip())
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET folder_present = ?
                WHERE mod_id = ?
                """,
                (1 if present else 0, mid),
            )
            self._conn.commit()

    def update_mod_size_observation(
        self,
        mod_id: int | str,
        *,
        status: str,
        size_bytes: int | None,
        observed_at: str,
        root_mtime: float | None,
    ) -> None:
        """Persist local directory size observation (not identity, not updated_at)."""
        mid = int(str(mod_id).strip())
        stamp = str(observed_at or "").strip()
        state = str(status or "unknown").strip() or "unknown"
        bytes_val: int | None
        if size_bytes is None:
            bytes_val = None
        else:
            bytes_val = int(size_bytes)
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if present is None:
                return
            self._conn.execute(
                """
                UPDATE mods SET
                    local_size_bytes = ?,
                    local_size_status = ?,
                    local_size_observed_at = ?,
                    local_size_root_mtime = ?
                WHERE mod_id = ?
                """,
                (bytes_val, state, stamp, root_mtime, mid),
            )
            self._conn.commit()

    def get_mod_size_observation(self, mod_id: int | str) -> dict[str, Any] | None:
        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id, last_known_path, folder_present,
                       local_size_bytes, local_size_status,
                       local_size_observed_at, local_size_root_mtime,
                       fs_root_mtime
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return None
        return {str(k): row[k] for k in row.keys()}

    def list_mod_size_observation_rows(
        self,
        *,
        game_id: int | None = None,
        mod_ids: list[str] | list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """SQL-only size observation rows (no filesystem walk)."""
        clauses: list[str] = ["TRIM(COALESCE(last_known_path, '')) != ''"]
        params: list[Any] = []
        gid = int(game_id or 0)
        if gid > 0:
            clauses.append("app_id = ?")
            params.append(gid)
        ids = [
            int(str(x).strip())
            for x in (mod_ids or ())
            if str(x).strip().isdigit()
        ]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"mod_id IN ({placeholders})")
            params.extend(ids)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT mod_id, last_known_path, folder_present,
                       local_size_bytes, local_size_status,
                       local_size_observed_at, local_size_root_mtime,
                       fs_root_mtime
                FROM mods
                WHERE {where}
                ORDER BY mod_id
                """,
                params,
            ).fetchall()
        return [{str(k): row[k] for k in row.keys()} for row in rows]

    def update_mod_fs_observation(
        self,
        mod_id: int | str,
        *,
        observed_at: str,
        root_mtime: float | None,
        root_exists: bool | None = None,
    ) -> None:
        """Persist filesystem observation stamp (not identity, not content_status)."""
        del root_exists  # stamp records mtime/time only; presence lives on folder_present
        mid = int(str(mod_id).strip())
        stamp = str(observed_at or "").strip()
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if present is None:
                return
            self._conn.execute(
                """
                UPDATE mods SET
                    fs_observed_at = ?,
                    fs_root_mtime = ?
                WHERE mod_id = ?
                """,
                (stamp, root_mtime, mid),
            )
            self._conn.commit()

    def get_mod_fs_observation(self, mod_id: int | str) -> dict[str, Any] | None:
        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id, last_known_path, folder_present,
                       content_status, fs_observed_at, fs_root_mtime,
                       local_size_bytes, local_size_status,
                       local_size_observed_at, local_size_root_mtime
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return None
        return {str(k): row[k] for k in row.keys()}

    def list_mod_ids_for_fs_observe(self) -> list[str]:
        """Internal IDs with a bound path — batch L0 candidates (no FS scan)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE TRIM(COALESCE(last_known_path, '')) != ''
                ORDER BY mod_id
                """
            ).fetchall()
        out: list[str] = []
        for row in rows:
            mid = str(row["mod_id"] or "").strip()
            if mid.isdigit():
                out.append(mid)
        return out

    def get_mods_backup_rows(
        self, mod_ids: list[str] | list[int]
    ) -> dict[str, dict[str, Any]]:
        """Batch ``get_mod_backup_row`` for snapshot builds (avoids N+1)."""
        ids: list[int] = []
        for raw in mod_ids:
            text = str(raw or "").strip()
            if text.isdigit():
                ids.append(int(text))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT mod_id, app_id, last_known_path, folder_present,
                       backup_updated_at, backup_metadata_json,
                       backup_cover_path, backup_offline_path,
                       backup_status, backup_last_validate_at,
                       offline_status, internal_id, library_status,
                       source_type, content_status, platform
                FROM mods WHERE mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            mid = str(row["mod_id"])
            out[mid] = {str(k): row[k] for k in row.keys()}
        return out

    def list_mod_list_items(
        self,
        *,
        game_id: int | None = None,
        app_id: int | None = None,
        game_folder: str | None = None,
        mod_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        """
        DB-first Library Layer-1 rows (no filesystem scan).

        Returns dicts compatible with ``services.mod_list_item.ModListItem``.

        ARCHITECTURE RULE: game filtering is SQL-pushed via ``m.app_id = ?``.
        ``game_id`` is the preferred parameter (maps to ``mods.app_id``).
        ``game_folder`` is resolved to ``game_id`` first — never load the full
        mods table then filter in Python.
        ``mod_id`` fetches a single Mod row for projection patch (not a full reload).
        """
        from pathlib import Path as _Path

        # Prefer explicit game_id; app_id is a legacy alias for the same column.
        gid: int | None = None
        if game_id is not None and int(game_id) > 0:
            gid = int(game_id)
        elif app_id is not None and int(app_id) > 0:
            gid = int(app_id)
        elif str(game_folder or "").strip():
            resolved = self.resolve_game_id_for_folder(game_folder)
            if resolved is None or int(resolved) <= 0:
                return []
            gid = int(resolved)

        clauses: list[str] = []
        params: list[Any] = []
        mid_filter = str(mod_id or "").strip()
        if mid_filter.isdigit():
            clauses.append("m.mod_id = ?")
            params.append(int(mid_filter))
        if gid is not None and gid > 0:
            # SQL: WHERE game_id (= mods.app_id) = ?
            clauses.append("m.app_id = ?")
            params.append(gid)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    m.mod_id,
                    m.workspace_id,
                    m.app_id,
                    m.title,
                    m.display_name,
                    m.favorite,
                    m.cover_path,
                    m.backup_cover_path,
                    m.last_known_path,
                    m.folder_present,
                    m.deploy_status,
                    m.platform,
                    m.content_status,
                    m.library_status,
                    m.conflict_status,
                    m.identity_status,
                    m.is_invalid,
                    EXISTS (
                        SELECT 1 FROM mod_tags AS t
                        WHERE t.mod_id = m.mod_id
                          AND t.tag_type = '{TAG_TYPE_ABANDONED}'
                    ) AS abandoned,
                    m.enabled,
                    m.offline_status,
                    m.external_id,
                    m.source_url,
                    m.source_type,
                    m.user_notes,
                    m.backup_offline_path,
                    m.updated_at,
                    m.local_size_bytes,
                    m.local_size_status,
                    m.type_id,
                    COALESCE(g.name, '') AS game_name
                FROM mods AS m
                LEFT JOIN games AS g ON g.app_id = m.app_id
                {where}
                ORDER BY m.updated_at DESC, m.mod_id DESC
                """,
                params,
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            mid = str(row["mod_id"])
            path = str(row["last_known_path"] or "").strip()
            game_name = str(row["game_name"] or "").strip()
            derived_folder = ""
            if path:
                try:
                    derived_folder = _Path(path).parent.name
                except Exception:  # noqa: BLE001
                    derived_folder = ""
            if not derived_folder and game_name:
                derived_folder = sanitize_folder_name(
                    game_name, fallback=game_name
                )

            display = str(row["display_name"] or "").strip()
            steam = str(row["title"] or "").strip()
            name = display or steam or ( _Path(path).name if path else mid)
            deploy_status = str(row["deploy_status"] or "not_deployed")
            conflict_status = str(row["conflict_status"] or "none")
            offline_status = str(row["offline_status"] or "none")
            backup_offline = str(row["backup_offline_path"] or "").strip()
            folder_present = bool(int(row["folder_present"] or 0))
            # LIVE → local cover; MISS → Backup cover. Never emit a dead
            # relative live path when folder_present=0 (Card would miss Detail).
            from services.cover_projection import projection_cover_ref

            cover = projection_cover_ref(
                folder_present=folder_present,
                cover_path=str(row["cover_path"] or ""),
                backup_cover_path=str(row["backup_cover_path"] or ""),
            )
            notes = str(row["user_notes"] or "")
            # Keep search light — truncate notes preview.
            notes_preview = notes[:120] if notes else ""
            content_status = str(row["content_status"] or "")
            try:
                from services.status_authority import normalize_content_axis
                from services.status_authority import normalize_identity_status as _norm_id

                content_status = normalize_content_axis(content_status)
                identity_status = _norm_id(
                    str(row["identity_status"] or "")
                    if "identity_status" in row.keys()
                    else ""
                )
            except Exception:  # noqa: BLE001
                identity_status = "ok"
            # User Mod status badge token: content axis only.
            # Identity / folder_absent / library_status never become status_badge.
            status_badge = (
                content_status if content_status == "content_missing" else ""
            )
            has_offline = offline_status in ("archived", "generated") or bool(
                backup_offline
            )
            # Layer-1 session row key = SQLite PK. Frozen TEXT identity is
            # mods.internal_id; business APIs must resolve via resolve_mod_pk.
            out.append(
                {
                    "internal_id": mid,
                    "workspace_id": str(row["workspace_id"] or "").strip(),
                    "game_id": int(row["app_id"] or 0),
                    "game_folder": derived_folder,
                    "name": name,
                    "favorite": bool(int(row["favorite"] or 0)),
                    "cover_path": cover,
                    "status_badge": status_badge,
                    "managed_path": path,
                    "platform": str(row["platform"] or "").strip(),
                    "deployed": deploy_status == DEPLOY_STATUS_DEPLOYED,
                    "folder_absent": not folder_present,
                    "content_status": content_status,
                    "identity_status": identity_status,
                    "conflict": conflict_status == CONFLICT_STATUS_CONFLICT,
                    "conflict_status": conflict_status,
                    "invalid": bool(int(row["is_invalid"] or 0)),
                    "abandoned": bool(
                        int(row["abandoned"] or 0)
                    ) if "abandoned" in row.keys() else False,
                    "enabled": bool(int(row["enabled"] if row["enabled"] is not None else 1)),
                    "has_offline": has_offline,
                    "mtime": updated_at_to_mtime(
                        str(row["updated_at"] or "")
                        if "updated_at" in row.keys()
                        else ""
                    ),
                    "local_size_bytes": (
                        None
                        if "local_size_bytes" not in row.keys()
                        or row["local_size_bytes"] is None
                        else int(row["local_size_bytes"])
                    ),
                    "local_size_status": (
                        str(row["local_size_status"] or "unknown").strip()
                        or "unknown"
                        if "local_size_status" in row.keys()
                        else "unknown"
                    ),
                    "category_tags": "",
                    "type_id": _row_type_id(row),
                    "external_id": str(row["external_id"] or "").strip(),
                    "source_url": str(row["source_url"] or "").strip(),
                    "source_type": str(row["source_type"] or "").strip(),
                    "deploy_status": deploy_status,
                    "offline_status": offline_status,
                    "library_status": str(row["library_status"] or "").strip(),
                    "steam_name": steam,
                    "relation_deps": 0,
                    "relation_conflicts": 0,
                    "notes_preview": notes_preview,
                }
            )
        return out

    def get_mod_backup_row(self, mod_id: int | str) -> dict[str, Any] | None:
        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id, app_id, last_known_path, folder_present,
                       backup_updated_at, backup_metadata_json,
                       backup_cover_path, backup_offline_path,
                       backup_status, backup_last_validate_at,
                       offline_status, internal_id, library_status,
                       source_type, content_status, identity_status,
                       platform, workspace_id,
                       fs_observed_at, fs_root_mtime
                FROM mods WHERE mod_id = ?
                """,
                (mid,),
            ).fetchone()
        if row is None:
            return None
        return {str(k): row[k] for k in row.keys()}

    def get_mod_backup_row_by_path(self, path: str) -> dict[str, Any] | None:
        key = str(path or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mod_id, app_id, last_known_path, folder_present,
                       backup_updated_at, backup_metadata_json,
                       backup_cover_path, backup_offline_path
                FROM mods
                WHERE last_known_path = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {str(k): row[k] for k in row.keys()}

    def iter_mod_backup_rows(self) -> list[dict[str, Any]]:
        """Rows that have ever been backed up (``last_known_path`` or backup JSON)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id, app_id, last_known_path, folder_present,
                       backup_updated_at, backup_metadata_json,
                       backup_cover_path, backup_offline_path,
                       backup_status, library_status, source_type,
                       content_status, platform
                FROM mods
                WHERE TRIM(last_known_path) != ''
                   OR TRIM(backup_metadata_json) != ''
                """
            ).fetchall()
        return [{str(k): row[k] for k in row.keys()} for row in rows]

    def iter_mod_backup_key_rows(self) -> list[dict[str, str]]:
        """All entity storage keys for backup census (includes MISS rows).

        Returns ``mod_id``, Frozen ``internal_id``, ``workspace_id``, and
        ``last_known_path``. Folder name of ``data/mod_backup/<mod_id>/`` is
        the current storage key — never ``workspace_id``.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id, internal_id, workspace_id, last_known_path,
                       folder_present, app_id, content_status
                FROM mods
                """
            ).fetchall()
        out: list[dict[str, str]] = []
        for row in rows:
            mid = str(row["mod_id"] or "").strip()
            if not mid.isdigit():
                continue
            out.append(
                {
                    "mod_id": mid,
                    "internal_id": str(row["internal_id"] or "").strip(),
                    "workspace_id": str(row["workspace_id"] or "").strip(),
                    "last_known_path": str(row["last_known_path"] or "").strip(),
                    "folder_present": str(int(row["folder_present"] or 0)),
                    "app_id": str(int(row["app_id"] or 0)),
                    "content_status": str(row["content_status"] or "").strip(),
                }
            )
        return out

    def list_folder_missing_mods(
        self,
        *,
        game_folder: str | None = None,
        library_root: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """
        Mods with ``folder_present = 0`` and a backup snapshot.

        Optional *game_folder* filters by parent folder name in ``last_known_path``.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id, app_id, last_known_path, folder_present,
                       backup_updated_at, backup_metadata_json,
                       backup_cover_path, backup_offline_path
                FROM mods
                WHERE folder_present = 0
                  AND TRIM(backup_metadata_json) != ''
                ORDER BY mod_id
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        game_key = str(game_folder or "").strip()
        root = Path(library_root) if library_root is not None else None
        for row in rows:
            item = {str(k): row[k] for k in row.keys()}
            if not game_key:
                out.append(item)
                continue
            lkp = str(item.get("last_known_path") or "").strip()
            if not lkp:
                continue
            try:
                rel = Path(lkp).resolve().relative_to(root.resolve()) if root else Path(lkp)
                parts = rel.parts
                if parts and parts[0] == game_key:
                    out.append(item)
            except (ValueError, OSError):
                if Path(lkp).parent.name == game_key:
                    out.append(item)
        return out

    def count_folder_missing_mods(
        self,
        *,
        game_folder: str | None = None,
        library_root: str | Path | None = None,
    ) -> int:
        return len(
            self.list_folder_missing_mods(
                game_folder=game_folder,
                library_root=library_root,
            )
        )

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

        Identity is ``(platform, app_id, external_id)``. Steam callers should keep using
        ``upsert_mod`` with Workshop ID as ``mod_id``.

        Non-Steam platforms require a real ``app_id`` (game context). Platform
        names such as ``GitHub`` / ``Nexus Mods`` are rejected as *game_name*.
        Uniqueness is enforced by ``uq_mods_platform_app_external`` when present.
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
                if requested <= 0:
                    raise ValueError(f"invalid mod_id: {mod_id!r}")
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
                ),
                allow_insert=True,
            )
            resolved = self.resolve_steam_entity_mod_id(
                ext, app_id=resolved_app
            )
            if resolved is None:
                raise RuntimeError(
                    f"Steam register failed to resolve workshop={ext}"
                )
            info = self.get_mod_display_info(resolved)
            assert info is not None
            from services.identity_service import ensure_durable_internal_id

            ensure_durable_internal_id(self, resolved)
            if source_url:
                return self.update_mod_platform_info(
                    resolved,
                    platform=PLATFORM_STEAM,
                    source_url=source_url,
                    external_id=ext,
                )
            refreshed = self.get_mod_display_info(resolved)
            return refreshed if refreshed is not None else info

        existing = self.find_mod_by_external(plat, ext, app_id=resolved_app)
        if existing is not None:
            # Refuse to rewrite a row whose Nexus game slug disagrees with the
            # incoming source URL (historical hybrid / cross-game pollution).
            if plat == PLATFORM_NEXUS and source_url:
                from services.importers.duplicate_check import (
                    nexus_source_urls_compatible,
                )

                row_url = str(getattr(existing, "source_url", "") or "")
                if row_url and not nexus_source_urls_compatible(row_url, source_url):
                    raise ValueError(
                        "nexus identity conflict: existing row source_url game "
                        "slug does not match import URL "
                        f"(mod_id={existing.mod_id}, row={row_url!r}, "
                        f"import={source_url!r})"
                    )
            mid = int(existing.mod_id)
        else:
            mid = int(mod_id) if mod_id is not None else None
            if mid is None:
                from services.identity_service import allocate_internal_id

                mid = int(allocate_internal_id(self))

        from services.identity_service import identity_create_scope

        try:
            with identity_create_scope():
                info = self.update_mod_platform_info(
                    mid,
                    platform=plat,
                    source_url=source_url,
                    external_id=ext,
                    title=title or None,
                    app_id=resolved_app,
                )
        except (sqlite3.IntegrityError, ValueError):
            raced = self.find_mod_by_external(plat, ext, app_id=resolved_app)
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
                from core.witcher3_game_version import ensure_witcher3_game_version_default
                from services.identity_service import ensure_durable_internal_id

                ensure_witcher3_game_version_default(
                    self, mid, app_id=resolved_app, game_name=resolved_game
                )
                ensure_durable_internal_id(self, mid)
                again = self.get_mod_display_info(mid)
                return again if again is not None else refreshed
        from core.witcher3_game_version import ensure_witcher3_game_version_default

        ensure_witcher3_game_version_default(
            self, mid, app_id=resolved_app, game_name=resolved_game
        )
        from services.identity_service import ensure_durable_internal_id

        ensure_durable_internal_id(self, mid)
        refreshed = self.get_mod_display_info(mid)
        return refreshed if refreshed is not None else info

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

    def update_mod_conflict_annotation(
        self,
        mod_id: int | str,
        *,
        conflict: bool,
        note: str = "",
    ) -> ModStatus:
        """
        Sole DB writer for ``mods.conflict_status`` / ``conflict_note``.

        ARCHITECTURE RULE: only ``services.user_annotation`` (Detail Panel
        flag chips) may call this. Sync / Refresh / Deploy / Reconcile /
        Identity / Repair / ConflictDetector must never touch this column.
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        new_cstatus = (
            CONFLICT_STATUS_CONFLICT if conflict else CONFLICT_STATUS_NONE
        )
        new_cnote = str(note or "") if conflict else ""
        stamp = _mods_updated_at_now(reason="user_edit")
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?", (mid,)
            ).fetchone()
            if present is None:
                logger.warning(
                    "update_mod_conflict_annotation skipped missing mod_id=%s "
                    "(must not mint identity)",
                    mid,
                )
                return ModStatus()
            self._conn.execute(
                """
                UPDATE mods SET
                    conflict_status = ?,
                    conflict_note = ?,
                    last_check_time = ?,
                    updated_at = ?
                WHERE mod_id = ?
                """,
                (new_cstatus, new_cnote, now, stamp, mid),
            )
            self._conn.commit()
        return self.get_mod_status(mid)

    def update_mod_status(
        self,
        mod_id: int | str,
        *,
        invalid: bool | None = None,
        invalid_reason: str | None = None,
        last_check_time: str | None = None,
        touch_check_time: bool = False,
    ) -> ModStatus:
        """
        Patch invalid / last_check_time columns. Omitted kwargs leave values.

        ARCHITECTURE RULE: this API **cannot** write ``conflict_status``.
        User conflict marks go through ``update_mod_conflict_annotation``
        via ``services.user_annotation`` only. ``touch_check_time`` alone is
        allowed for diagnostic scans (ConflictDetector).

        When ``touch_check_time`` is True and ``last_check_time`` is None,
        sets ``last_check_time`` to now (UTC).

        Never bumps ``mods.updated_at`` (status scan / recovery is forbidden).
        """
        mid = int(str(mod_id).strip())
        now = _utc_now()
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?", (mid,)
            ).fetchone()
            if present is None:
                logger.warning(
                    "update_mod_status skipped missing mod_id=%s "
                    "(status persist must not mint identity)",
                    mid,
                )
                return ModStatus()
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
            if last_check_time is not None:
                new_check = str(last_check_time)
            elif touch_check_time:
                new_check = now
            else:
                new_check = current.last_check_time
            # Never SET conflict_status / conflict_note here — those columns
            # belong exclusively to update_mod_conflict_annotation.
            self._conn.execute(
                """
                UPDATE mods SET
                    is_invalid = ?,
                    invalid_reason = ?,
                    last_check_time = ?
                WHERE mod_id = ?
                """,
                (
                    1 if new_invalid else 0,
                    new_reason,
                    new_check,
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
            # Version probes / platform sync must not bump Library sort time.
            self._conn.execute(
                """
                UPDATE mods SET
                    mod_version = ?,
                    installed_version = ?,
                    version_source = ?,
                    version_checked_at = ?
                WHERE mod_id = ?
                """,
                (new_mod_v, new_inst, new_src, new_checked, mid),
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
        stamp = _mods_updated_at_now(reason="user_edit")
        with self._lock:
            self._ensure_mod_stub(mid)
            self._conn.execute(
                """
                UPDATE mods SET enabled = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                (1 if enabled else 0, stamp, mid),
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
                    m.workspace_id,
                    m.is_invalid,
                    m.conflict_status,
                    m.enabled,
                    m.updated_at,
                    m.type_id,
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
            try:
                from core.models import is_unknown_mod_title

                if is_unknown_mod_title(user_display, published_file_id=mid):
                    user_display = ""
            except Exception:  # noqa: BLE001
                pass
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
                display_name=user_display or steam or (
                    f"Unknown_Mod_{mid}" if not is_internal_mod_id(mid) else steam
                ),
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
                workspace_id=(
                    str(row["workspace_id"] or "") if "workspace_id" in keys else ""
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
                type_id=_row_type_id(row),
                updated_at=(
                    str(row["updated_at"] or "") if "updated_at" in keys else ""
                ),
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
        # Optional — only update when the key is present (edit dialog).
        touch_source = "source_url" in data
        source_url = str(data.get("source_url", "") or "").strip() if touch_source else None
        touch_platform = "platform" in data
        platform = (
            normalize_platform(str(data.get("platform") or ""))
            if touch_platform
            else None
        )
        touch_custom_deploy = "custom_deploy_path" in data
        custom_deploy_path = (
            str(data.get("custom_deploy_path", "") or "").strip()
            if touch_custom_deploy
            else None
        )
        now = _mods_updated_at_now(reason="user_edit")

        with self._lock:
            existing = self._conn.execute(
                "SELECT mod_id FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if existing is None:
                from services.identity_service import refuse_unauthorized_mod_insert

                logger.warning(
                    "update_mod_user_metadata refused create for missing mod_id=%s",
                    mid,
                )
                refuse_unauthorized_mod_insert(mid)
                raise RuntimeError(f"metadata edit refused: no mods row for {mid}")
            else:
                if touch_source or touch_platform:
                    if touch_source and touch_platform:
                        self._conn.execute(
                            """
                            UPDATE mods SET
                                display_name = ?,
                                custom_description = ?,
                                user_notes = ?,
                                favorite = ?,
                                platform = ?,
                                source_url = ?,
                                updated_at = ?
                            WHERE mod_id = ?
                            """,
                            (
                                display_name,
                                custom_description,
                                user_notes,
                                favorite,
                                platform or PLATFORM_STEAM,
                                source_url or "",
                                now,
                                mid,
                            ),
                        )
                    elif touch_source:
                        self._conn.execute(
                            """
                            UPDATE mods SET
                                display_name = ?,
                                custom_description = ?,
                                user_notes = ?,
                                favorite = ?,
                                source_url = ?,
                                updated_at = ?
                            WHERE mod_id = ?
                            """,
                            (
                                display_name,
                                custom_description,
                                user_notes,
                                favorite,
                                source_url or "",
                                now,
                                mid,
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
                                platform = ?,
                                updated_at = ?
                            WHERE mod_id = ?
                            """,
                            (
                                display_name,
                                custom_description,
                                user_notes,
                                favorite,
                                platform or PLATFORM_STEAM,
                                now,
                                mid,
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
                if touch_custom_deploy:
                    self._conn.execute(
                        """
                        UPDATE mods SET custom_deploy_path = ?, updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (custom_deploy_path or "", now, mid),
                    )
                if "game_version" in data:
                    self._apply_mod_game_version_locked(
                        mid, data.get("game_version")
                    )
                if "category" in data:
                    text = str(data.get("category") or "").strip()
                    self._conn.execute(
                        """
                        UPDATE mods SET category = ?, updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (text or None, now, mid),
                    )
                self._ensure_mod_workspace_id_locked(mid)
            self._apply_user_override_flags_from_save(
                mid,
                display_name=display_name,
                custom_description=custom_description,
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        assert row is not None
        return _display_info_from_row(row)

    def set_mod_game_version(
        self, mod_id: int | str, value: str | None
    ) -> ModDisplayInfo:
        """Persist Witcher 3 game_version. Non-Witcher-3 rows are forced to NULL.

        Does not change mod_id / external_id / workspace_id / source_url.
        Invalid tokens raise ValueError (no silent fallback).
        """
        mid = int(str(mod_id).strip())
        with self._lock:
            existing = self._conn.execute(
                "SELECT mod_id FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"set_mod_game_version: no mods row for {mid}")
            self._apply_mod_game_version_locked(mid, value)
            stamp = _mods_updated_at_now(reason="user_edit")
            self._conn.execute(
                "UPDATE mods SET updated_at = ? WHERE mod_id = ?",
                (stamp, mid),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_MOD_SELECT_COLS} FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        assert row is not None
        return _display_info_from_row(row)

    def _apply_mod_game_version_locked(self, mid: int, value: Any) -> None:
        """Witcher 3 ONLY writer. Caller holds lock. Invalid values raise."""
        from core.witcher3_game_version import (
            WITCHER3_DEFAULT_VERSION,
            is_witcher3_game,
            validate_witcher3_game_version,
        )

        row = self._conn.execute(
            "SELECT app_id FROM mods WHERE mod_id = ?",
            (mid,),
        ).fetchone()
        if row is None:
            return
        app_id = int(row["app_id"] or 0)
        if not is_witcher3_game("", app_id):
            self._conn.execute(
                "UPDATE mods SET game_version = NULL WHERE mod_id = ?",
                (mid,),
            )
            return
        text = "" if value is None else str(value).strip()
        stored = WITCHER3_DEFAULT_VERSION if not text else validate_witcher3_game_version(text)
        self._conn.execute(
            "UPDATE mods SET game_version = ? WHERE mod_id = ?",
            (stored, mid),
        )

    def get_user_override_fields(self, mod_id: int | str) -> dict[str, bool]:
        from services.metadata_ownership import parse_user_override_fields

        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                "SELECT user_override_fields FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        if row is None:
            return {}
        keys = set(row.keys()) if hasattr(row, "keys") else {"user_override_fields"}
        raw = str(row["user_override_fields"] or "") if "user_override_fields" in keys else ""
        return parse_user_override_fields(raw)

    def set_user_override_field(
        self, mod_id: int | str, field: str, *, overridden: bool = True
    ) -> None:
        from services.metadata_ownership import (
            FIELD_COVER,
            FIELD_DESCRIPTION,
            FIELD_DISPLAY_NAME,
            parse_user_override_fields,
            serialize_user_override_fields,
        )

        key = str(field or "").strip()
        if key not in {FIELD_DISPLAY_NAME, FIELD_DESCRIPTION, FIELD_COVER}:
            return
        mid = int(str(mod_id).strip())
        with self._lock:
            self._ensure_mod_stub(mid)
            row = self._conn.execute(
                "SELECT user_override_fields FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            current = parse_user_override_fields(
                str(row["user_override_fields"] or "") if row else ""
            )
            if overridden:
                current[key] = True
            else:
                current.pop(key, None)
            stamp = _mods_updated_at_now(reason="user_edit")
            self._conn.execute(
                """
                UPDATE mods SET user_override_fields = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                (serialize_user_override_fields(current), stamp, mid),
            )
            self._conn.commit()

    def _apply_user_override_flags_from_save(
        self,
        mod_id: int,
        *,
        display_name: str,
        custom_description: str,
    ) -> None:
        from services.metadata_ownership import (
            FIELD_DESCRIPTION,
            FIELD_DISPLAY_NAME,
            is_placeholder_display_name,
            parse_user_override_fields,
            serialize_user_override_fields,
        )

        row = self._conn.execute(
            "SELECT user_override_fields FROM mods WHERE mod_id = ?",
            (mod_id,),
        ).fetchone()
        current = parse_user_override_fields(
            str(row["user_override_fields"] or "") if row else ""
        )
        if display_name.strip() and not is_placeholder_display_name(
            display_name, mod_id=str(mod_id)
        ):
            current[FIELD_DISPLAY_NAME] = True
        elif not display_name.strip():
            current.pop(FIELD_DISPLAY_NAME, None)
        if custom_description.strip():
            current[FIELD_DESCRIPTION] = True
        # Parent ``update_mod_user_metadata`` already stamped updated_at.
        self._conn.execute(
            """
            UPDATE mods SET user_override_fields = ?
            WHERE mod_id = ?
            """,
            (serialize_user_override_fields(current), mod_id),
        )

    def is_official_metadata_synced(self, mod_id: int | str) -> bool:
        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                "SELECT official_metadata_synced FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        if row is None:
            return False
        return bool(int(row["official_metadata_synced"] or 0))

    def set_official_metadata_synced(
        self, mod_id: int | str, synced: bool
    ) -> None:
        mid = int(str(mod_id).strip())
        with self._lock:
            present = self._conn.execute(
                "SELECT 1 FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
            if present is None:
                logger.warning(
                    "set_official_metadata_synced refused: no mods row for %s",
                    mid,
                )
                return
            self._conn.execute(
                """
                UPDATE mods SET official_metadata_synced = ?
                WHERE mod_id = ?
                """,
                (1 if synced else 0, mid),
            )
            self._conn.commit()

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
                from services.identity_service import refuse_unauthorized_mod_insert

                refuse_unauthorized_mod_insert(mid)
                raise RuntimeError(f"deploy status refused: no mods row for {mid}")
            if app_id is not None:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        app_id = ?,
                        deploy_status = ?,
                        deploy_time = ?,
                        deploy_path = ?,
                        deploy_error = ?
                    WHERE mod_id = ?
                    """,
                    (int(app_id), status, when, path, err, mid),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE mods SET
                        deploy_status = ?,
                        deploy_time = ?,
                        deploy_path = ?,
                        deploy_error = ?
                    WHERE mod_id = ?
                    """,
                    (status, when, path, err, mid),
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
        stamp = _mods_updated_at_now(reason="user_edit")
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
            self._conn.execute(
                "UPDATE mods SET updated_at = ? WHERE mod_id = ?",
                (stamp, mid),
            )
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
        stamp = _mods_updated_at_now(reason="user_edit")
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
            deleted = int(cur.rowcount or 0)
            if deleted:
                self._conn.execute(
                    "UPDATE mods SET updated_at = ? WHERE mod_id = ?",
                    (stamp, mid),
                )
            self._conn.commit()
            return deleted

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

    def add_game_category(self, app_id: int | str, name: str) -> bool:
        """Persist a user-defined Mod type label for one game."""
        label = str(name or "").strip()
        aid = int(app_id or 0)
        if not label or aid <= 0:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO game_categories (app_id, name, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (aid, label, _utc_now()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_game_categories(self, app_id: int | str) -> list[str]:
        """Category labels defined for one game (sorted)."""
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name FROM game_categories
                WHERE app_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (aid,),
            ).fetchall()
        return [str(r["name"]) for r in rows]

    def list_legacy_type_names_by_game(self) -> dict[int, list[str]]:
        """Exact ``(app_id, name)`` union of game_categories + category tags.

        Legacy migration source only — not Type Definition authority.
        """
        names: dict[int, list[str]] = {}
        seen: dict[int, set[str]] = {}
        with self._lock:
            cat_rows = self._conn.execute(
                "SELECT app_id, name FROM game_categories ORDER BY app_id, id"
            ).fetchall()
            tag_rows = self._conn.execute(
                """
                SELECT m.app_id, t.tag_value AS name
                FROM mod_tags AS t
                JOIN mods AS m ON m.mod_id = t.mod_id
                WHERE t.tag_type = ? AND t.tag_value != ''
                ORDER BY m.app_id, t.id
                """,
                (TAG_TYPE_CATEGORY,),
            ).fetchall()
        for row in (*cat_rows, *tag_rows):
            try:
                aid = int(row["app_id"] or 0)
            except (TypeError, ValueError):
                continue
            label = str(row["name"] or "")
            if aid <= 0 or not label:
                continue
            bucket = seen.setdefault(aid, set())
            if label in bucket:
                continue
            bucket.add(label)
            names.setdefault(aid, []).append(label)
        return names

    def list_legacy_mod_type_names(self) -> list[tuple[str, int, str]]:
        """Primary category tag per Mod: ``(mod_id, app_id, exact_name)``.

        Uses the earliest ``mod_tags.id`` when a Mod has several category tags.
        Legacy migration source only.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.mod_id, m.app_id, t.tag_value AS name
                FROM mod_tags AS t
                JOIN mods AS m ON m.mod_id = t.mod_id
                WHERE t.tag_type = ?
                  AND t.tag_value != ''
                  AND t.id = (
                    SELECT MIN(t2.id) FROM mod_tags AS t2
                    WHERE t2.mod_id = t.mod_id
                      AND t2.tag_type = ?
                      AND t2.tag_value != ''
                  )
                """,
                (TAG_TYPE_CATEGORY, TAG_TYPE_CATEGORY),
            ).fetchall()
        out: list[tuple[str, int, str]] = []
        for row in rows:
            try:
                aid = int(row["app_id"] or 0)
            except (TypeError, ValueError):
                continue
            label = str(row["name"] or "")
            if aid <= 0 or not label:
                continue
            out.append((str(row["mod_id"]), aid, label))
        return out

    def bind_null_mod_type_ids(self, assignments: Mapping[str, int]) -> int:
        """Set ``mods.type_id`` only where it is currently NULL. No updated_at."""
        pairs: list[tuple[int, int]] = []
        for raw_mid, raw_tid in (assignments or {}).items():
            try:
                mid = int(str(raw_mid).strip())
                tid = int(raw_tid)
            except (TypeError, ValueError):
                continue
            if mid <= 0 or tid <= 0:
                continue
            pairs.append((tid, mid))
        if not pairs:
            return 0
        changed = 0
        with self._lock:
            for tid, mid in pairs:
                cur = self._conn.execute(
                    """
                    UPDATE mods SET type_id = ?
                    WHERE mod_id = ? AND type_id IS NULL
                    """,
                    (tid, mid),
                )
                changed += int(cur.rowcount or 0)
            self._conn.commit()
        return changed

    def count_mods_and_type_bindings(self) -> tuple[int, int]:
        """``(total mods, mods with type_id)``. No Identity reads."""
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
            bound = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM mods
                    WHERE type_id IS NOT NULL AND type_id > 0
                    """
                ).fetchone()[0]
            )
        return total, bound

    def delete_game_category(self, app_id: int | str, name: str) -> bool:
        """Remove a game-defined type label. Does not strip Mod tags."""
        label = str(name or "").strip()
        aid = int(app_id or 0)
        if not label or aid <= 0:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM game_categories WHERE app_id = ? AND name = ?",
                (aid, label),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Deployment records (named Mod sets — no deploy side effects)
    # ------------------------------------------------------------------

    def create_deployment_record(
        self,
        app_id: int | str,
        name: str,
        mod_ids: Iterable[int | str],
    ) -> DeploymentRecord:
        """
        Insert a named Mod set for ``app_id``.

        Caller must validate that every ``mod_id`` belongs to ``app_id``.
        Does not read or write ``mods.deploy_status``.
        """
        aid = int(app_id)
        label = str(name or "").strip()
        if aid <= 0:
            raise ValueError("app_id must be a positive game id")
        if not label:
            raise ValueError("deployment record name must be non-empty")
        ids = _normalize_mod_id_list(mod_ids)
        now = _utc_now()
        with self._lock:
            game = self._conn.execute(
                "SELECT app_id FROM games WHERE app_id = ?",
                (aid,),
            ).fetchone()
            if game is None:
                raise ValueError(f"unknown game app_id={aid}")
            cur = self._conn.execute(
                """
                INSERT INTO deployment_records (app_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (aid, label, now, now),
            )
            record_id = int(cur.lastrowid)
            if ids:
                self._conn.executemany(
                    """
                    INSERT INTO deployment_record_items (record_id, mod_id)
                    VALUES (?, ?)
                    """,
                    [(record_id, mid) for mid in ids],
                )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records WHERE id = ?
                """,
                (record_id,),
            ).fetchone()
        assert row is not None
        return _deployment_record_from_row(row)

    def list_deployment_records(self, app_id: int | str) -> list[DeploymentRecord]:
        """All deployment records for one game (newest updated first)."""
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records
                WHERE app_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (aid,),
            ).fetchall()
        return [_deployment_record_from_row(r) for r in rows]

    def get_deployment_record(self, record_id: int | str) -> DeploymentRecord | None:
        """Return one record by primary key, or None."""
        rid = int(str(record_id).strip())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records WHERE id = ?
                """,
                (rid,),
            ).fetchone()
        if row is None:
            return None
        return _deployment_record_from_row(row)

    def find_deployment_record_by_name(
        self, app_id: int | str, name: str
    ) -> DeploymentRecord | None:
        """Find a record by per-game display name (case-insensitive)."""
        aid = int(app_id or 0)
        label = str(name or "").strip()
        if aid <= 0 or not label:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records
                WHERE app_id = ? AND name = ? COLLATE NOCASE
                LIMIT 1
                """,
                (aid, label),
            ).fetchone()
        if row is None:
            return None
        return _deployment_record_from_row(row)

    def get_deployment_record_mod_ids(self, record_id: int | str) -> set[str]:
        """Mod ID set stored in a deployment record (for Filter Context)."""
        rid = int(str(record_id).strip())
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM deployment_record_items
                WHERE record_id = ?
                ORDER BY mod_id
                """,
                (rid,),
            ).fetchall()
        # Normalize numeric ids so \"001\" and 1 compare equal in Filter Context.
        out: set[str] = set()
        for r in rows:
            raw = str(r["mod_id"] or "").strip()
            if raw.isdigit():
                out.add(str(int(raw)))
            elif raw:
                out.add(raw)
        return out

    def update_deployment_record(
        self,
        record_id: int | str,
        *,
        name: str | None = None,
        mod_ids: Iterable[int | str] | None = None,
    ) -> DeploymentRecord:
        """
        Rename and/or replace the Mod set for an existing record.

        Passing ``mod_ids`` replaces all items. Does not touch live deploy state.
        Renames must not collide with another record for the same ``app_id``.
        """
        rid = int(str(record_id).strip())
        now = _utc_now()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records WHERE id = ?
                """,
                (rid,),
            ).fetchone()
            if existing is None:
                raise LookupError(f"deployment record not found: {rid}")
            new_name = (
                str(existing["name"] or "")
                if name is None
                else str(name).strip()
            )
            if not new_name:
                raise ValueError("deployment record name must be non-empty")
            if name is not None:
                clash = self._conn.execute(
                    """
                    SELECT id FROM deployment_records
                    WHERE app_id = ? AND name = ? COLLATE NOCASE AND id != ?
                    LIMIT 1
                    """,
                    (int(existing["app_id"]), new_name, rid),
                ).fetchone()
                if clash is not None:
                    raise ValueError(
                        f"deployment record name already exists for this game: {new_name!r}"
                    )
            self._conn.execute(
                """
                UPDATE deployment_records
                SET name = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_name, now, rid),
            )
            if mod_ids is not None:
                ids = _normalize_mod_id_list(mod_ids)
                self._conn.execute(
                    "DELETE FROM deployment_record_items WHERE record_id = ?",
                    (rid,),
                )
                if ids:
                    self._conn.executemany(
                        """
                        INSERT INTO deployment_record_items (record_id, mod_id)
                        VALUES (?, ?)
                        """,
                        [(rid, mid) for mid in ids],
                    )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT id, app_id, name, created_at, updated_at
                FROM deployment_records WHERE id = ?
                """,
                (rid,),
            ).fetchone()
        assert row is not None
        return _deployment_record_from_row(row)

    def delete_deployment_record(self, record_id: int | str) -> bool:
        """
        Delete a record and its items.

        Does not delete ``mods`` rows or change ``deploy_status``.
        """
        rid = int(str(record_id).strip())
        with self._lock:
            self._conn.execute(
                "DELETE FROM deployment_record_items WHERE record_id = ?",
                (rid,),
            )
            cur = self._conn.execute(
                "DELETE FROM deployment_records WHERE id = ?",
                (rid,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Collections (named Mod sets — browse/org only, no deploy / WH3)
    # ------------------------------------------------------------------

    def create_collection(self, app_id: int | str, name: str) -> CollectionRecord:
        """
        Insert a Collection for ``app_id``.

        Name is stripped; empty names are rejected. Duplicate names per game
        raise ``ValueError`` (UNIQUE COLLATE NOCASE), matching deployment records.
        Does not write mods / deploy / WH3 load order.
        """
        aid = int(app_id)
        label = str(name or "").strip()
        if aid <= 0:
            raise ValueError("app_id must be a positive game id")
        if not label:
            raise ValueError("collection name must be non-empty")
        now = _utc_now()
        with self._lock:
            game = self._conn.execute(
                "SELECT app_id FROM games WHERE app_id = ?",
                (aid,),
            ).fetchone()
            if game is None:
                raise ValueError(f"unknown game app_id={aid}")
            row_max = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) AS mx FROM collections WHERE app_id = ?",
                (aid,),
            ).fetchone()
            next_order = int(row_max["mx"] or -1) + 1
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO collections (
                        app_id, name, cover_path, sort_order, created_at, updated_at
                    ) VALUES (?, ?, '', ?, ?, ?)
                    """,
                    (aid, label, next_order, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"collection name already exists for this game: {label!r}"
                ) from exc
            cid = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT collection_id, app_id, name, cover_path, sort_order,
                       created_at, updated_at, 0 AS mod_count
                FROM collections WHERE collection_id = ?
                """,
                (cid,),
            ).fetchone()
        assert row is not None
        return _collection_from_row(row)

    def list_collections(self, app_id: int | str) -> list[CollectionRecord]:
        """Collections for one game, ordered by ``sort_order`` (then id)."""
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    c.collection_id,
                    c.app_id,
                    c.name,
                    c.cover_path,
                    c.sort_order,
                    c.created_at,
                    c.updated_at,
                    (
                        SELECT COUNT(*) FROM collection_mods AS m
                        WHERE m.collection_id = c.collection_id
                    ) AS mod_count
                FROM collections AS c
                WHERE c.app_id = ?
                ORDER BY c.sort_order ASC, c.collection_id ASC
                """,
                (aid,),
            ).fetchall()
        return [_collection_from_row(r) for r in rows]

    def get_collection(self, collection_id: int | str) -> CollectionRecord | None:
        try:
            cid = int(str(collection_id).strip())
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    c.collection_id,
                    c.app_id,
                    c.name,
                    c.cover_path,
                    c.sort_order,
                    c.created_at,
                    c.updated_at,
                    (
                        SELECT COUNT(*) FROM collection_mods AS m
                        WHERE m.collection_id = c.collection_id
                    ) AS mod_count
                FROM collections AS c
                WHERE c.collection_id = ?
                """,
                (cid,),
            ).fetchone()
        if row is None:
            return None
        return _collection_from_row(row)

    def rename_collection(
        self, collection_id: int | str, name: str
    ) -> CollectionRecord:
        cid = int(str(collection_id).strip())
        label = str(name or "").strip()
        if not label:
            raise ValueError("collection name must be non-empty")
        now = _utc_now()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT collection_id, app_id, name FROM collections
                WHERE collection_id = ?
                """,
                (cid,),
            ).fetchone()
            if existing is None:
                raise LookupError(f"collection not found: {cid}")
            clash = self._conn.execute(
                """
                SELECT collection_id FROM collections
                WHERE app_id = ? AND name = ? COLLATE NOCASE
                  AND collection_id != ?
                LIMIT 1
                """,
                (int(existing["app_id"]), label, cid),
            ).fetchone()
            if clash is not None:
                raise ValueError(
                    f"collection name already exists for this game: {label!r}"
                )
            self._conn.execute(
                """
                UPDATE collections SET name = ?, updated_at = ?
                WHERE collection_id = ?
                """,
                (label, now, cid),
            )
            self._conn.commit()
        found = self.get_collection(cid)
        assert found is not None
        return found

    def update_collection_cover_path(
        self, collection_id: int | str, cover_path: str
    ) -> CollectionRecord:
        """Set ``collections.cover_path`` (relative to ``data_dir()``). No schema change."""
        cid = int(str(collection_id).strip())
        value = str(cover_path or "").strip()
        now = _utc_now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT collection_id FROM collections WHERE collection_id = ?",
                (cid,),
            ).fetchone()
            if existing is None:
                raise LookupError(f"collection not found: {cid}")
            self._conn.execute(
                """
                UPDATE collections SET cover_path = ?, updated_at = ?
                WHERE collection_id = ?
                """,
                (value, now, cid),
            )
            self._conn.commit()
        found = self.get_collection(cid)
        assert found is not None
        return found

    def delete_collection(self, collection_id: int | str) -> bool:
        """
        Delete a Collection and its memberships.

        Does not delete ``mods`` rows, metadata, files, or deploy / WH3 state.
        """
        cid = int(str(collection_id).strip())
        with self._lock:
            self._conn.execute(
                "DELETE FROM collection_mods WHERE collection_id = ?",
                (cid,),
            )
            cur = self._conn.execute(
                "DELETE FROM collections WHERE collection_id = ?",
                (cid,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def reorder_collections(
        self,
        app_id: int | str,
        ordered_ids: Sequence[int | str],
    ) -> list[CollectionRecord]:
        """Persist Collection list order for ``app_id``. Does not touch WH3."""
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        wanted: list[int] = []
        seen: set[int] = set()
        for raw in ordered_ids or ():
            try:
                cid = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if cid <= 0 or cid in seen:
                continue
            seen.add(cid)
            wanted.append(cid)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT collection_id FROM collections
                WHERE app_id = ?
                ORDER BY sort_order ASC, collection_id ASC
                """,
                (aid,),
            ).fetchall()
            existing = [int(r["collection_id"]) for r in rows]
            existing_set = set(existing)
            final = [cid for cid in wanted if cid in existing_set]
            for cid in existing:
                if cid not in seen:
                    final.append(cid)
            now = _utc_now()
            for index, cid in enumerate(final):
                self._conn.execute(
                    """
                    UPDATE collections
                    SET sort_order = ?, updated_at = ?
                    WHERE collection_id = ? AND app_id = ?
                    """,
                    (index, now, cid, aid),
                )
            self._conn.commit()
        return self.list_collections(aid)

    def list_collection_member_ids(self, collection_id: int | str) -> list[str]:
        """Membership FK values (``mods.mod_id`` as text) in one Collection."""
        cid = int(str(collection_id).strip())
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM collection_mods
                WHERE collection_id = ?
                ORDER BY id ASC
                """,
                (cid,),
            ).fetchall()
        out: list[str] = []
        for row in rows:
            raw = str(row["mod_id"] or "").strip()
            if raw.isdigit():
                out.append(str(int(raw)))
            elif raw:
                out.append(raw)
        return out

    def add_mod_to_collection(
        self,
        collection_id: int | str,
        internal_id: int | str,
    ) -> bool:
        """
        Add a Mod to a Collection by SQLite PK (``collection_mods.mod_id``).

        Business callers must resolve Frozen TEXT ``internal_id`` first
        (``services.collection``). INSERT OR IGNORE on UNIQUE(collection_id, mod_id).
        Never writes mods.
        """
        return bool(self.add_mods_to_collection(collection_id, [internal_id]))

    def add_mods_to_collection(
        self,
        collection_id: int | str,
        internal_ids: Iterable[int | str],
    ) -> int:
        """Batch membership insert in one transaction. Returns rows inserted."""
        cid = int(str(collection_id).strip())
        ids = _normalize_mod_id_list(internal_ids)
        if not ids:
            return 0
        now = _utc_now()
        inserted = 0
        with self._lock:
            coll = self._conn.execute(
                "SELECT collection_id, app_id FROM collections WHERE collection_id = ?",
                (cid,),
            ).fetchone()
            if coll is None:
                raise LookupError(f"collection not found: {cid}")
            app_id = int(coll["app_id"])
            placeholders = ",".join("?" for _ in ids)
            owned = self._conn.execute(
                f"""
                SELECT mod_id FROM mods
                WHERE mod_id IN ({placeholders}) AND app_id = ?
                """,
                [*ids, app_id],
            ).fetchall()
            owned_ids = {int(r["mod_id"]) for r in owned}
            missing = [mid for mid in ids if mid not in owned_ids]
            if missing:
                raise ValueError(
                    f"mod Internal ID not in this game: {missing[0]}"
                )
            for mid in ids:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO collection_mods (
                        collection_id, mod_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (cid, mid, now),
                )
                inserted += int(cur.rowcount or 0)
            if inserted:
                self._conn.execute(
                    "UPDATE collections SET updated_at = ? WHERE collection_id = ?",
                    (now, cid),
                )
            self._conn.commit()
        return inserted

    def remove_mod_from_collection(
        self,
        collection_id: int | str,
        internal_id: int | str,
    ) -> bool:
        """Drop membership only — never deletes the Mod row."""
        return bool(
            self.remove_mods_from_collection(collection_id, [internal_id])
        )

    def remove_mods_from_collection(
        self,
        collection_id: int | str,
        internal_ids: Iterable[int | str],
    ) -> int:
        cid = int(str(collection_id).strip())
        ids = _normalize_mod_id_list(internal_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            cur = self._conn.execute(
                f"""
                DELETE FROM collection_mods
                WHERE collection_id = ? AND mod_id IN ({placeholders})
                """,
                [cid, *ids],
            )
            removed = int(cur.rowcount or 0)
            if removed:
                self._conn.execute(
                    "UPDATE collections SET updated_at = ? WHERE collection_id = ?",
                    (_utc_now(), cid),
                )
            self._conn.commit()
        return removed

    def list_collection_ids_for_mods(
        self, internal_ids: Iterable[int | str]
    ) -> dict[str, set[int]]:
        """Map Internal ID ``str(mods.mod_id)`` → Collection ids. Never workspace_id."""
        ids = _normalize_mod_id_list(internal_ids)
        out: dict[str, set[int]] = {str(mid): set() for mid in ids}
        if not ids:
            return out
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT collection_id, mod_id FROM collection_mods
                WHERE mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        for row in rows:
            mid = str(int(row["mod_id"]))
            if mid in out:
                out[mid].add(int(row["collection_id"]))
        return out

    def apply_collection_memberships(
        self,
        app_id: int | str,
        internal_ids: Iterable[int | str],
        add_collection_ids: Iterable[int | str],
        remove_collection_ids: Iterable[int | str],
    ) -> tuple[int, int]:
        """
        One-transaction membership apply for selected Mods.

        Adds ``internal_ids`` to ``add_collection_ids`` and removes them from
        ``remove_collection_ids``. Does not write mods / deploy / WH3.
        Returns ``(inserted, removed)`` row counts.
        """
        aid = int(app_id)
        mods = _normalize_mod_id_list(internal_ids)
        add_ids = _normalize_positive_id_list(add_collection_ids)
        remove_ids = _normalize_positive_id_list(remove_collection_ids)
        if aid <= 0 or not mods or (not add_ids and not remove_ids):
            return (0, 0)
        now = _utc_now()
        inserted = 0
        removed = 0
        with self._lock:
            owned = self._conn.execute(
                f"""
                SELECT mod_id FROM mods
                WHERE mod_id IN ({",".join("?" for _ in mods)}) AND app_id = ?
                """,
                [*mods, aid],
            ).fetchall()
            owned_ids = {int(r["mod_id"]) for r in owned}
            missing = [mid for mid in mods if mid not in owned_ids]
            if missing:
                raise ValueError(
                    f"mod Internal ID not in this game: {missing[0]}"
                )
            wanted = list(dict.fromkeys([*add_ids, *remove_ids]))
            coll_rows = self._conn.execute(
                f"""
                SELECT collection_id FROM collections
                WHERE app_id = ? AND collection_id IN ({",".join("?" for _ in wanted)})
                """,
                [aid, *wanted],
            ).fetchall()
            legal = {int(r["collection_id"]) for r in coll_rows}
            add_ids = [cid for cid in add_ids if cid in legal]
            remove_ids = [cid for cid in remove_ids if cid in legal]
            if not add_ids and not remove_ids:
                return (0, 0)
            touched: set[int] = set()
            for cid in add_ids:
                for mid in mods:
                    cur = self._conn.execute(
                        """
                        INSERT OR IGNORE INTO collection_mods (
                            collection_id, mod_id, created_at
                        ) VALUES (?, ?, ?)
                        """,
                        (cid, mid, now),
                    )
                    n = int(cur.rowcount or 0)
                    inserted += n
                    if n:
                        touched.add(cid)
            for cid in remove_ids:
                cur = self._conn.execute(
                    f"""
                    DELETE FROM collection_mods
                    WHERE collection_id = ?
                      AND mod_id IN ({",".join("?" for _ in mods)})
                    """,
                    [cid, *mods],
                )
                n = int(cur.rowcount or 0)
                removed += n
                if n:
                    touched.add(cid)
            for cid in touched:
                self._conn.execute(
                    "UPDATE collections SET updated_at = ? WHERE collection_id = ?",
                    (now, cid),
                )
            self._conn.commit()
        return (inserted, removed)

    def list_deployed_mod_ids_for_app(self, app_id: int | str) -> list[str]:
        """Mod IDs with ``deploy_status=deployed`` for one game (read-only)."""
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE app_id = ? AND deploy_status = ?
                ORDER BY mod_id
                """,
                (aid, DEPLOY_STATUS_DEPLOYED),
            ).fetchall()
        return [str(r["mod_id"]) for r in rows]

    def list_deployed_mod_ids_for_library_game(
        self,
        app_id: int | str,
        *,
        game_folder: str | None = None,
        library_root: str | Path | None = None,
    ) -> list[str]:
        """
        Deployed Mod IDs as the Library shows them for one game.

        Matches Library card membership (folder under the game directory), not
        ``mods.app_id``. Mods with ``app_id=0`` under the game folder are included.

        Fallback: when ``last_known_path`` is empty, still include rows whose
        ``app_id`` equals the game (legacy / test rows without a path).
        """
        aid = int(app_id or 0)
        if aid <= 0:
            return []
        folder = str(game_folder or "").strip()
        if not folder:
            game = self.get_game(aid)
            if game is not None:
                folder = str(game.folder_name or "").strip() or sanitize_folder_name(
                    game.name, fallback=f"App_{aid}"
                )
        root = Path(library_root) if library_root is not None else None
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id, app_id, last_known_path FROM mods
                WHERE deploy_status = ?
                ORDER BY mod_id
                """,
                (DEPLOY_STATUS_DEPLOYED,),
            ).fetchall()
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            mid = str(row["mod_id"] or "").strip()
            if not mid:
                continue
            if mid.isdigit():
                mid = str(int(mid))
            lkp = str(row["last_known_path"] or "").strip()
            include = False
            if folder and lkp and _last_known_path_in_game_folder(
                lkp, folder, library_root=root
            ):
                include = True
            elif not lkp and int(row["app_id"] or 0) == aid:
                # Path-less rows: keep Steam-bound / unit-test mods.
                include = True
            if include and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    def get_mods_app_ids(
        self, mod_ids: Iterable[int | str]
    ) -> dict[str, int]:
        """Map ``mod_id`` → ``app_id`` for the given ids (missing ids omitted)."""
        ids = _normalize_mod_id_list(mod_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT mod_id, app_id FROM mods
                WHERE mod_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {str(r["mod_id"]): int(r["app_id"] or 0) for r in rows}

    def set_mod_category(self, mod_id: int | str, category: str) -> None:
        """Replace all category tags with a single label (empty clears)."""
        label = str(category or "").strip()
        for tag in self.get_category_tags(mod_id):
            self.remove_category_tag(mod_id, tag)
        if label:
            self.add_category_tag(mod_id, label)

    def get_mod_type_id(self, mod_id: int | str) -> int | None:
        mid = int(str(mod_id).strip())
        with self._lock:
            row = self._conn.execute(
                "SELECT type_id FROM mods WHERE mod_id = ?",
                (mid,),
            ).fetchone()
        if row is None:
            return None
        return _row_type_id(row)

    def set_mod_type_id(
        self,
        mod_id: int | str,
        type_id: int | str | None,
        *,
        touch_updated_at: bool = True,
    ) -> None:
        """Bind this Mod to a game-scoped Type ID (None clears). Never stores a name."""
        mid = int(str(mod_id).strip())
        tid = None
        if type_id is not None and str(type_id).strip() != "":
            try:
                parsed = int(str(type_id).strip())
            except (TypeError, ValueError):
                parsed = 0
            tid = parsed if parsed > 0 else None
        with self._lock:
            if touch_updated_at:
                now = _mods_updated_at_now(reason="user_edit")
                self._conn.execute(
                    "UPDATE mods SET type_id = ?, updated_at = ? WHERE mod_id = ?",
                    (tid, now, mid),
                )
            else:
                self._conn.execute(
                    "UPDATE mods SET type_id = ? WHERE mod_id = ?",
                    (tid, mid),
                )
            self._conn.commit()

    def list_mod_ids_with_type(self, app_id: int | str, type_id: int | str) -> list[str]:
        aid = int(app_id or 0)
        try:
            tid = int(str(type_id).strip())
        except (TypeError, ValueError):
            return []
        if aid <= 0 or tid <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mod_id FROM mods
                WHERE app_id = ? AND type_id = ?
                """,
                (aid, tid),
            ).fetchall()
        return [str(r["mod_id"]) for r in rows]

    def clear_mods_type_id(self, app_id: int | str, type_id: int | str) -> int:
        """Unbind every Mod in *app_id* from *type_id*. Does not bump updated_at."""
        aid = int(app_id or 0)
        try:
            tid = int(str(type_id).strip())
        except (TypeError, ValueError):
            return 0
        if aid <= 0 or tid <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE mods SET type_id = NULL
                WHERE app_id = ? AND type_id = ?
                """,
                (aid, tid),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def list_app_ids_with_type_bindings(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT app_id FROM mods
                WHERE type_id IS NOT NULL AND type_id > 0
                """
            ).fetchall()
        out: list[int] = []
        for row in rows:
            try:
                aid = int(row["app_id"] or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0:
                out.append(aid)
        return out

    def clear_orphan_mod_type_ids(
        self, app_id: int | str, valid_type_ids: Iterable[int]
    ) -> int:
        """NULL type_id values in *app_id* that are not in *valid_type_ids*."""
        aid = int(app_id or 0)
        if aid <= 0:
            return 0
        valid = sorted(
            {
                int(x)
                for x in (valid_type_ids or ())
                if str(x).strip().lstrip("-").isdigit() and int(x) > 0
            }
        )
        with self._lock:
            if not valid:
                cur = self._conn.execute(
                    """
                    UPDATE mods SET type_id = NULL
                    WHERE app_id = ? AND type_id IS NOT NULL
                    """,
                    (aid,),
                )
            else:
                placeholders = ",".join("?" for _ in valid)
                cur = self._conn.execute(
                    f"""
                    UPDATE mods SET type_id = NULL
                    WHERE app_id = ? AND type_id IS NOT NULL
                      AND type_id NOT IN ({placeholders})
                    """,
                    (aid, *valid),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def append_identity_audit_log(
        self,
        *,
        mod_id: int | str,
        field_name: str,
        old_value: str = "",
        new_value: str = "",
        source: str = "",
        reason: str = "",
        commit: bool = True,
    ) -> None:
        """Persist one identity mutation provenance row."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO identity_audit_log (
                    mod_id, field_name, old_value, new_value,
                    source, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(mod_id or "").strip(),
                    str(field_name or "").strip(),
                    str(old_value or ""),
                    str(new_value or ""),
                    str(source or ""),
                    str(reason or ""),
                    _utc_now(),
                ),
            )
            if commit:
                self._conn.commit()

    def detach_mods_from_game(self, app_id: int | str) -> int:
        """Clear ``mods.app_id`` mapping for a game without deleting Mod rows."""
        try:
            aid = int(str(app_id).strip())
        except (TypeError, ValueError):
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE mods SET app_id = 0, updated_at = ? WHERE app_id = ?",
                (_utc_now(), aid),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def delete_game_record(self, app_id: int | str) -> bool:
        """
        Remove ``games`` + ``game_categories`` for ``app_id``.

        Does not delete Mod rows or touch the filesystem.
        """
        try:
            aid = int(str(app_id).strip())
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM game_categories WHERE app_id = ?",
                (aid,),
            )
            cur = self._conn.execute(
                "DELETE FROM games WHERE app_id = ?",
                (aid,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def delete_mod_record(self, mod_id: int | str) -> bool:
        """
        Remove SQLite rows for ``mod_id`` (mods + tags + relations).

        Does not touch filesystem or game install paths.
        """
        if not str(mod_id).strip().isdigit():
            return False
        mid = int(mod_id)
        with self._lock:
            self._conn.execute("DELETE FROM mod_tags WHERE mod_id = ?", (mid,))
            self._conn.execute(
                """
                DELETE FROM mod_relations
                WHERE source_mod_id = ? OR target_mod_id = ?
                """,
                (mid, mid),
            )
            self._conn.execute(
                """
                DELETE FROM mod_relationships
                WHERE source_mod_id = ? OR target_mod_id = ?
                """,
                (mid, mid),
            )
            self._conn.execute(
                "DELETE FROM deployment_record_items WHERE mod_id = ?",
                (mid,),
            )
            try:
                self._conn.execute(
                    "DELETE FROM identity_audit_log WHERE mod_id = ?",
                    (mid,),
                )
            except sqlite3.OperationalError:
                pass
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

    # ------------------------------------------------------------------
    # Mods — relationships (mod_relationships)
    # ------------------------------------------------------------------

    def find_mod_id_by_workspace_id(
        self, workspace_id: int | str, *, app_id: int = 0
    ) -> str | None:
        """
        REMOVED as an identity API — always returns ``None``.

        ``workspace_id`` cannot locate a Mod entity. Callers must pass
        Frozen ``internal_id`` (TEXT) resolved to ``mods.mod_id``, or a PK
        handle. Never ``workspace_id``. Registration rematch uses
        ``(platform, app_id, workspace_id)`` via ``find_mod_for_registration``.
        """
        _ = (workspace_id, app_id)
        return None

    def add_mod_relationship(
        self,
        source_mod_id: int | str,
        target_mod_id: int | str,
        relationship_type: str,
    ) -> ModRelationship:
        """
        Declare a user-confirmed relationship. Never auto-guessed.

        Duplicate ``(source, target, type)`` returns the existing row.
        """
        src = int(str(source_mod_id).strip())
        tgt = int(str(target_mod_id).strip())
        rtype = str(relationship_type or "").strip().lower()
        if rtype not in SUPPORTED_RELATIONSHIP_TYPES:
            raise ValueError(
                f"unsupported relationship_type: {relationship_type!r}"
            )
        if src == tgt:
            raise ValueError("source_mod_id and target_mod_id must differ")
        now = _utc_now()
        with self._lock:
            self._ensure_mod_stub(src)
            self._ensure_mod_stub(tgt)
            existing = self._conn.execute(
                """
                SELECT id FROM mod_relationships
                WHERE source_mod_id = ? AND target_mod_id = ?
                      AND relationship_type = ?
                LIMIT 1
                """,
                (src, tgt, rtype),
            ).fetchone()
            if existing is not None:
                row_id = int(existing["id"])
            else:
                cur = self._conn.execute(
                    """
                    INSERT INTO mod_relationships
                        (source_mod_id, target_mod_id, relationship_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (src, tgt, rtype, now),
                )
                row_id = int(cur.lastrowid)
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT r.id, r.source_mod_id, r.target_mod_id, r.relationship_type,
                       r.created_at,
                       COALESCE(NULLIF(TRIM(m.display_name), ''), m.title, '')
                           AS target_title
                FROM mod_relationships AS r
                LEFT JOIN mods AS m ON m.mod_id = r.target_mod_id
                WHERE r.id = ?
                """,
                (row_id,),
            ).fetchone()
        assert row is not None
        return _mod_relationship_from_row(row)

    def remove_mod_relationship(self, relationship_id: int | str) -> bool:
        """Delete one relationship by primary key. Returns True if a row was removed."""
        rid = int(str(relationship_id).strip())
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM mod_relationships WHERE id = ?",
                (rid,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

    def get_mod_relationships(self, mod_id: int | str) -> dict[str, list[dict[str, Any]]]:
        """
        Group outbound relationships for ``mod_id``::

            {
              "dependencies": [...],
              "conflicts": [...],
              "addons": [...],
              "patches": [...],
            }
        """
        empty: dict[str, list[dict[str, Any]]] = {
            "dependencies": [],
            "conflicts": [],
            "addons": [],
            "patches": [],
        }
        if not str(mod_id).strip().isdigit():
            return empty
        mid = int(mod_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.id, r.source_mod_id, r.target_mod_id, r.relationship_type,
                       r.created_at,
                       COALESCE(NULLIF(TRIM(m.display_name), ''), m.title, '')
                           AS target_title
                FROM mod_relationships AS r
                LEFT JOIN mods AS m ON m.mod_id = r.target_mod_id
                WHERE r.source_mod_id = ?
                ORDER BY r.relationship_type, r.id
                """,
                (mid,),
            ).fetchall()
        bucket_key = {
            RELATIONSHIP_DEPENDENCY: "dependencies",
            RELATIONSHIP_CONFLICT: "conflicts",
            RELATIONSHIP_ADDON: "addons",
            RELATIONSHIP_PATCH: "patches",
        }
        for row in rows:
            rel = _mod_relationship_from_row(row)
            key = bucket_key.get(rel.relationship_type)
            if key:
                empty[key].append(rel.as_dict())
        return empty

    def get_relationship_counts(
        self,
        mod_ids: Iterable[int | str],
    ) -> dict[str, tuple[int, int]]:
        """
        Batch counts for card badges: ``mod_id → (dependency_count, conflict_count)``.
        """
        ids = [int(str(i).strip()) for i in mod_ids if str(i).strip().isdigit()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        out: dict[str, tuple[int, int]] = {str(i): (0, 0) for i in ids}
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT source_mod_id, relationship_type, COUNT(*) AS n
                FROM mod_relationships
                WHERE source_mod_id IN ({placeholders})
                  AND relationship_type IN (?, ?)
                GROUP BY source_mod_id, relationship_type
                """,
                [*ids, RELATIONSHIP_DEPENDENCY, RELATIONSHIP_CONFLICT],
            ).fetchall()
        deps: dict[str, int] = {str(i): 0 for i in ids}
        confs: dict[str, int] = {str(i): 0 for i in ids}
        for row in rows:
            mid = str(row["source_mod_id"])
            n = int(row["n"] or 0)
            if str(row["relationship_type"]) == RELATIONSHIP_DEPENDENCY:
                deps[mid] = n
            elif str(row["relationship_type"]) == RELATIONSHIP_CONFLICT:
                confs[mid] = n
        for mid in out:
            out[mid] = (deps.get(mid, 0), confs.get(mid, 0))
        return out

    def check_relationship_deploy_warnings(
        self,
        mod_id: int | str,
    ) -> list[dict[str, Any]]:
        """
        Warn-only deploy checks for declared relationships.

        - dependency target disabled
        - known conflict with another Mod
        Never auto-enables Mods.
        """
        warnings: list[dict[str, Any]] = []
        if not str(mod_id).strip().isdigit():
            return warnings
        mid = str(mod_id).strip()
        grouped = self.get_mod_relationships(mid)
        for item in grouped["dependencies"]:
            tid = str(item.get("mod_id") or item.get("target_mod_id") or "")
            if not tid.isdigit():
                continue
            if not self.is_mod_enabled(tid):
                title = str(item.get("title") or tid)
                warnings.append(
                    {
                        "type": "dependency_disabled",
                        "target_mod_id": tid,
                        "title": title,
                        "message": f"Required Mod disabled:\n{title}",
                    }
                )
        for item in grouped["conflicts"]:
            tid = str(item.get("mod_id") or item.get("target_mod_id") or "")
            title = str(item.get("title") or tid)
            src_info = self.get_mod_display_info(mid)
            src_name = (
                src_info.display_name if src_info else mid
            )
            warnings.append(
                {
                    "type": "known_conflict",
                    "target_mod_id": tid,
                    "title": title,
                    "message": f"Known conflict:\n{src_name} conflicts with {title}",
                }
            )
        return warnings

    def _ensure_mod_stub(self, mod_id: int) -> None:
        """Insert a minimal mods row so FK-less tags can still attach to an ID."""
        now = _utc_now()
        mid = int(mod_id)
        present = self._conn.execute(
            "SELECT 1 FROM mods WHERE mod_id = ?", (mid,)
        ).fetchone()
        if present is not None:
            return
        from services.identity_service import refuse_unauthorized_mod_insert

        refuse_unauthorized_mod_insert(mid)
        # PK is never Workshop / platform identity. Stub until create binds.
        stub_external_id = f"stub:{mid}"
        self._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                platform, source_url, external_id, workspace_id, mod_files, updated_at
            )
            VALUES (?, 0, '', '', '', '', '', '', 0, '', '', ?, '', '{}', ?)
            ON CONFLICT(mod_id) DO NOTHING
            """,
            (
                mid,
                stub_external_id,
                now,
            ),
        )


def _normalize_mod_id_list(mod_ids: Iterable[int | str]) -> list[int]:
    """Deduped positive integer mod ids preserving first-seen order."""
    return _normalize_positive_id_list(mod_ids, label="mod_id")


def _normalize_positive_id_list(
    values: Iterable[int | str], *, label: str = "id"
) -> list[int]:
    """Deduped positive integer ids preserving first-seen order."""
    seen: set[int] = set()
    out: list[int] = []
    for raw in values or ():
        text = str(raw).strip()
        if not text.isdigit():
            raise ValueError(f"invalid {label}: {raw!r}")
        mid = int(text)
        if mid <= 0:
            raise ValueError(f"invalid {label}: {raw!r}")
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _last_known_path_in_game_folder(
    last_known_path: str,
    game_folder: str,
    *,
    library_root: Path | None = None,
) -> bool:
    """True when ``…/<game_folder>/<mod>`` matches Library game membership."""
    folder = str(game_folder or "").strip()
    lkp = str(last_known_path or "").strip()
    if not folder or not lkp:
        return False
    path = Path(lkp)
    try:
        if library_root is not None:
            rel = path.resolve().relative_to(Path(library_root).resolve())
            parts = rel.parts
            return bool(parts) and parts[0] == folder
    except (ValueError, OSError):
        pass
    try:
        parts = path.parts
    except Exception:  # noqa: BLE001
        return path.parent.name == folder
    return folder in parts


def _deployment_record_from_row(row: sqlite3.Row) -> DeploymentRecord:
    return DeploymentRecord(
        id=int(row["id"]),
        app_id=int(row["app_id"]),
        name=str(row["name"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _collection_from_row(row: sqlite3.Row) -> CollectionRecord:
    keys = set(row.keys())
    return CollectionRecord(
        collection_id=int(row["collection_id"]),
        app_id=int(row["app_id"]),
        name=str(row["name"] or ""),
        cover_path=str(row["cover_path"] or "") if "cover_path" in keys else "",
        sort_order=int(row["sort_order"] or 0) if "sort_order" in keys else 0,
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        mod_count=int(row["mod_count"] or 0) if "mod_count" in keys else 0,
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
    keys = set(row.keys())
    workshop = (
        str(row["workshop_path"] or "") if "workshop_path" in keys else ""
    )
    return GameDeployConfig(
        app_id=int(row["app_id"]),
        install_path=str(row["install_path"] or ""),
        mod_path=str(row["mod_path"] or ""),
        deploy_type=dtype,
        name=str(row["name"] or ""),
        workshop_path=workshop,
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


def _mod_relationship_from_row(row: sqlite3.Row) -> ModRelationship:
    keys = set(row.keys())
    return ModRelationship(
        id=int(row["id"]),
        source_mod_id=str(row["source_mod_id"]),
        target_mod_id=str(row["target_mod_id"]),
        relationship_type=str(row["relationship_type"] or ""),
        created_at=str(row["created_at"] or ""),
        target_title=(
            str(row["target_title"] or "") if "target_title" in keys else ""
        ),
    )


def _row_type_id(row: sqlite3.Row | Mapping[str, Any]) -> int | None:
    try:
        keys = row.keys()
    except Exception:  # noqa: BLE001
        keys = ()
    if "type_id" not in keys:
        return None
    raw = row["type_id"]
    if raw is None or raw == "":
        return None
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def _mod_from_row(row: sqlite3.Row) -> ModMetadata:
    keys = row.keys()
    mid = str(row["mod_id"])
    # Workshop axis only — never invent published_file_id from Internal PK.
    # Steam historical rows may still have PK == Workshop; callers that need
    # the entity key must use ``internal_id`` / ``entity_internal_id()``.
    ext = str(row["external_id"] or "") if "external_id" in keys else ""
    plat = str(row["platform"] or "") if "platform" in keys else ""
    pub = ""
    try:
        from services.identity_service import sidecar_published_file_id

        pub = sidecar_published_file_id(
            mod_id=mid, platform=plat, external_id=ext
        )
    except Exception:  # noqa: BLE001
        pub = ""
    if not pub and not is_internal_mod_id(mid):
        # Legacy Steam PK==Workshop coincidence — keep Workshop-shaped token.
        pub = mid
    return ModMetadata(
        published_file_id=pub,
        internal_id=mid,
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
    # Placeholder Unknown_Mod_* overrides must not hide a real Steam title.
    try:
        from core.models import is_unknown_mod_title

        if is_unknown_mod_title(
            user_display, published_file_id=str(row["mod_id"] or "")
        ):
            user_display = ""
    except Exception:  # noqa: BLE001
        pass
    resolved = user_display or steam_name or (
        f"Unknown_Mod_{row['mod_id']}"
        if not is_internal_mod_id(row["mod_id"])
        else steam_name or str(row["mod_id"])
    )
    raw_platform = (
        str(row["platform"] or "") if "platform" in keys else ""
    )
    platform = normalize_platform_if_known(raw_platform)
    if not platform:
        if is_internal_mod_id(row["mod_id"]):
            platform = ""
        else:
            platform = PLATFORM_STEAM
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
    cover_path = str(row["cover_path"] or "") if "cover_path" in keys else ""
    # Witcher 3 ONLY. Empty string in the dataclass == SQL NULL / not applicable.
    game_version = ""
    if "game_version" in keys and row["game_version"] is not None:
        game_version = str(row["game_version"] or "").strip()
    category = ""
    if "category" in keys and row["category"] is not None:
        category = str(row["category"] or "").strip()
    type_id = _row_type_id(row) if "type_id" in keys else None
    workspace_id = (
        str(row["workspace_id"] or "") if "workspace_id" in keys else ""
    )
    custom_deploy_path = (
        str(row["custom_deploy_path"] or "")
        if "custom_deploy_path" in keys
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
        workspace_id=workspace_id,
        custom_deploy_path=custom_deploy_path,
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
        cover_path=cover_path,
        game_version=game_version,
        category=category,
        type_id=type_id,
    )


def get_db() -> DatabaseManager:
    """Convenience accessor for the process-wide database singleton."""
    return DatabaseManager.instance()
