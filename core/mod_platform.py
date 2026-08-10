"""Platform / source-URL / multi-file helpers for generic Mod identity."""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

PLATFORM_STEAM = "steam"
PLATFORM_NEXUS = "nexus"
PLATFORM_GITHUB = "github"
PLATFORM_MODIO = "modio"
PLATFORM_OTHER = "other"
SUPPORTED_PLATFORMS = (
    PLATFORM_STEAM,
    PLATFORM_NEXUS,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_OTHER,
)

# Per-file source (additive; Mod-level ``mods.platform`` remains authoritative until E2+).
SOURCE_TYPE_STEAM = "steam"
SOURCE_TYPE_NEXUS = "nexus"
SOURCE_TYPE_GITHUB = "github"
SOURCE_TYPE_MODIO = "modio"
SOURCE_TYPE_OTHER = "other"
SOURCE_TYPE_LEGACY = "legacy"
SUPPORTED_SOURCE_TYPES = (
    SOURCE_TYPE_STEAM,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_MODIO,
    SOURCE_TYPE_OTHER,
    SOURCE_TYPE_LEGACY,
)

FILE_TYPE_MAIN = "main"
FILE_TYPE_OPTIONAL = "optional"
FILE_TYPE_PATCH = "patch"
SUPPORTED_FILE_TYPES = (FILE_TYPE_MAIN, FILE_TYPE_OPTIONAL, FILE_TYPE_PATCH)

# Platform-specific file roles (additive; coarse ``type`` stays deploy/UI-facing).
FILE_ROLE_STEAM_CONTENT = "steam_content"
FILE_ROLE_NEXUS_MAIN = "nexus_main"
FILE_ROLE_NEXUS_OPTIONAL = "nexus_optional"
FILE_ROLE_NEXUS_MISC = "nexus_misc"
FILE_ROLE_NEXUS_OLD = "nexus_old"
FILE_ROLE_GITHUB_RELEASE_ASSET = "github_release_asset"
FILE_ROLE_GITHUB_DEVELOPER_BUILD = "github_developer_build"
FILE_ROLE_GITHUB_SOURCE_ARCHIVE = "github_source_archive"
# Import default — role not inferred; user assigns later.
FILE_ROLE_UNKNOWN = "unknown"
SUPPORTED_FILE_ROLES = (
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_NEXUS_MISC,
    FILE_ROLE_NEXUS_OLD,
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_UNKNOWN,
)

# Alias map from external / dialog role names → canonical file_role.
FILE_ROLE_ALIASES = {
    "main_file": FILE_ROLE_NEXUS_MAIN,
    "optional_file": FILE_ROLE_NEXUS_OPTIONAL,
    "misc_file": FILE_ROLE_NEXUS_MISC,
    "old_file": FILE_ROLE_NEXUS_OLD,
    "workshop_content": FILE_ROLE_STEAM_CONTENT,
    "release_asset": FILE_ROLE_GITHUB_RELEASE_ASSET,
    "developer_build": FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    "dev_build": FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    "source_archive": FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
}

# Internal SQLite ``mod_id`` values for non-Steam Mods (avoid Workshop ID collisions).
NON_STEAM_MOD_ID_BASE = 9_000_000_000_000_000

DEFAULT_MOD_FILES_JSON = "{}"

# mods.offline_status / offline_provider
OFFLINE_STATUS_NONE = "none"
OFFLINE_STATUS_GENERATED = "generated"
OFFLINE_STATUS_ARCHIVED = "archived"
OFFLINE_STATUS_FAILED = "failed"
SUPPORTED_OFFLINE_STATUSES = (
    OFFLINE_STATUS_NONE,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
)
PROVIDER_STEAM_ARCHIVE = "steam_archive"
PROVIDER_NEXUS_SNAPSHOT = "nexus_snapshot"
PROVIDER_NEXUS_MANUAL_IMPORT = "nexus_manual_import"
PROVIDER_GITHUB_SNAPSHOT = "github_snapshot"
PROVIDER_MODIO_ARCHIVE = "modio_archive"
# Legacy generator ids — kept for rows written before snapshot providers.
PROVIDER_NEXUS_GENERATOR = "nexus_generator"
PROVIDER_GITHUB_GENERATOR = "github_generator"

PROVIDER_DISPLAY_NAMES = {
    PROVIDER_STEAM_ARCHIVE: "Steam Archive",
    PROVIDER_NEXUS_SNAPSHOT: "Nexus Snapshot",
    PROVIDER_NEXUS_MANUAL_IMPORT: "用户浏览器保存",
    PROVIDER_GITHUB_SNAPSHOT: "GitHub Snapshot",
    PROVIDER_MODIO_ARCHIVE: "mod.io Archive",
    PROVIDER_NEXUS_GENERATOR: "Nexus Generator",
    PROVIDER_GITHUB_GENERATOR: "GitHub Generator",
}


def format_offline_provider(value: str | None) -> str:
    """Human-readable offline provider label for UI."""
    key = str(value or "").strip()
    if not key:
        return "—"
    return PROVIDER_DISPLAY_NAMES.get(key, key)


def normalize_offline_status(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_OFFLINE_STATUSES:
        return key
    return OFFLINE_STATUS_NONE


def steam_workshop_url(workshop_id: int | str) -> str:
    mid = str(workshop_id).strip()
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}"


def _nexus_numeric_id_from_url(url: str) -> str:
    """Last numeric segment after ``/mods/`` (or a bare digit string)."""
    text = str(url or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    parts = [p for p in urlparse(text).path.split("/") if p]
    if "mods" in parts:
        try:
            idx = parts.index("mods")
            candidate = parts[idx + 1].split("?")[0].strip()
            return candidate if candidate.isdigit() else ""
        except (ValueError, IndexError):
            return ""
    if "/mods/" in text.replace("\\", "/"):
        tail = text.replace("\\", "/").split("/mods/", 1)[-1]
        candidate = tail.split("/")[0].split("?")[0].strip()
        return candidate if candidate.isdigit() else ""
    return ""


def generate_unique_workspace_id(existing: set[str] | None = None) -> str:
    """
    Random unique digit string for GitHub / mod.io / 其它 (and Nexus fallback).

    Uses millisecond timestamp + 4 random digits; retries on collision.
    """
    taken = existing if existing is not None else set()
    for _ in range(64):
        candidate = f"{int(time.time() * 1000)}{random.randint(1000, 9999)}"
        if candidate not in taken:
            return candidate
    return str(uuid.uuid4().int)[:18]


def resolve_workspace_id(
    platform: str | None,
    *,
    mod_id: int | str = "",
    source_url: str = "",
    external_id: str = "",
    existing: str = "",
) -> str:
    """
    Resolve a persistent Workspace ID for a Mod.

    - Steam → Steam Mod ID (``mod_id``)
    - Nexus → trailing numeric id from source URL (``…/mods/1234`` → ``1234``)
    - GitHub / mod.io / 其它 → empty (caller must generate via
      :func:`generate_unique_workspace_id`)

    Non-empty ``existing`` is always kept (persist once assigned).
    """
    kept = str(existing or "").strip()
    if kept:
        return kept
    key = normalize_platform(platform)
    mid = str(mod_id or "").strip()
    ext = str(external_id or "").strip()
    url = str(source_url or "").strip()
    if key == PLATFORM_STEAM:
        if mid.isdigit():
            return mid
        return ext if ext.isdigit() else ""
    if key == PLATFORM_NEXUS:
        nid = _nexus_numeric_id_from_url(url)
        if nid:
            return nid
        if ext.isdigit():
            return ext
        return ""
    return ""


# Anno 1800 / 纪元1800 — exclusive mod.io game gate.
ANNO_1800_APP_IDS = frozenset({916440})
ANNO_1800_NAME_ALIASES = frozenset(
    {
        "anno 1800",
        "anno1800",
        "纪元1800",
        "纪元 1800",
    }
)
MODIO_ANNO_1800_URL = "https://mod.io/g/anno-1800"
MODIO_DEFAULT_URL = "https://mod.io"


def _normalize_game_key(game_name: str | None) -> str:
    text = str(game_name or "").strip().casefold()
    for ch in (" ", "_", "-", "·", "—"):
        text = text.replace(ch, "")
    return text


def is_anno_1800_game(game_name: str = "", game_id: int = 0) -> bool:
    """True when the library game is Anno 1800 / 纪元1800."""
    if int(game_id or 0) in ANNO_1800_APP_IDS:
        return True
    key = _normalize_game_key(game_name)
    if not key:
        return False
    aliases = {_normalize_game_key(a) for a in ANNO_1800_NAME_ALIASES}
    if key in aliases:
        return True
    return "anno1800" in key or "纪元1800" in key


# (platform_id, UI label) — base sources every game supports.
BASE_SOURCE_OPTIONS: tuple[tuple[str, str], ...] = (
    (PLATFORM_STEAM, "Steam Workshop"),
    (PLATFORM_NEXUS, "Nexus Mods"),
    (PLATFORM_GITHUB, "GitHub"),
    (PLATFORM_OTHER, "其它"),
)

# Platforms that never require a source URL (pure local / free-form identity).
URL_OPTIONAL_PLATFORMS = frozenset({PLATFORM_OTHER})

# Auto-download / archive offline pages (Nexus is import-only — not listed).
OFFLINE_DOWNLOAD_PLATFORMS = frozenset(
    {PLATFORM_STEAM, PLATFORM_GITHUB, PLATFORM_MODIO}
)


def platform_requires_source_url(platform: str | None) -> bool:
    """False when Import / Edit may leave the source link empty."""
    return normalize_platform(platform) not in URL_OPTIONAL_PLATFORMS


def supports_offline_page_download(platform: str | None) -> bool:
    """True when Detail「保存离线页面」can fetch without a manual HTML import."""
    return normalize_platform(platform) in OFFLINE_DOWNLOAD_PLATFORMS


def get_available_sources(
    game_name: str = "",
    game_id: int = 0,
) -> list[tuple[str, str]]:
    """
    Dynamic source platforms for Import / Edit UI.

    Always includes Steam / Nexus / GitHub / 其它. Appends ``mod.io`` only for
    Anno 1800 (纪元1800).
    """
    sources = list(BASE_SOURCE_OPTIONS)
    if is_anno_1800_game(game_name, game_id):
        sources.append((PLATFORM_MODIO, "mod.io"))
    return sources


def default_source_url_for_platform(
    platform: str | None,
    *,
    game_name: str = "",
    game_id: int = 0,
) -> str:
    """Baseline URL when the user leaves the source link empty."""
    key = normalize_platform(platform)
    if key == PLATFORM_MODIO:
        if is_anno_1800_game(game_name, game_id):
            return MODIO_ANNO_1800_URL
        return MODIO_DEFAULT_URL
    # 「其它」and similar free-form sources keep an empty link.
    return ""


def normalize_platform(value: str | None) -> str:
    key = str(value or "").strip().lower()
    # Accept common UI spellings.
    if key in {"mod.io", "mod_io", "mod-io"}:
        key = PLATFORM_MODIO
    if key in {"其它", "其他", "other", "local", "manual"}:
        key = PLATFORM_OTHER
    if key in SUPPORTED_PLATFORMS:
        return key
    return PLATFORM_STEAM


# Canonical JSON key for Mod-level platform in ``.info/metadata.json``.
METADATA_SOURCE_TYPE_KEY = "source_type"
METADATA_PLATFORM_ALIASES = (METADATA_SOURCE_TYPE_KEY, "platform", "source")


def normalize_platform_if_known(value: str | None) -> str:
    """
    Map known platform spellings for metadata I/O.

    Returns ``""`` when *value* is empty or unrecognized — never falls back
    to Steam (callers decide defaults for brand-new imports only).
    """
    key = str(value or "").strip().lower()
    if not key:
        return ""
    if key in {"mod.io", "mod_io", "mod-io"}:
        key = PLATFORM_MODIO
    if key in {"其它", "其他", "other", "local", "manual"}:
        key = PLATFORM_OTHER
    if key in SUPPORTED_PLATFORMS:
        return key
    return ""


def parse_metadata_platform(data: Mapping[str, Any] | None) -> str:
    """
    Read platform from ``metadata.json`` / sidecar dict.

    Checks ``source_type`` (canonical), then legacy ``platform`` / ``source``.
    Never defaults missing values to Steam.
    """
    if not isinstance(data, Mapping):
        return ""
    for key in METADATA_PLATFORM_ALIASES:
        raw = str(data.get(key) or "").strip()
        if not raw:
            continue
        parsed = normalize_platform_if_known(raw)
        if parsed:
            return parsed
    return ""


def normalize_file_type(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_FILE_TYPES:
        return key
    if key in ("update", "addon"):
        return FILE_TYPE_OPTIONAL
    return FILE_TYPE_MAIN


def normalize_source_type(value: str | None) -> str:
    """Normalize per-file source_type; missing/unknown → legacy."""
    key = str(value or "").strip().lower()
    if key in SUPPORTED_SOURCE_TYPES:
        return key
    return SOURCE_TYPE_LEGACY


def normalize_file_role(value: str | None) -> str:
    """Normalize file_role; empty stays empty; unknown values kept (forward-compat)."""
    key = str(value or "").strip().lower()
    if not key:
        return ""
    return FILE_ROLE_ALIASES.get(key, key)


def map_file_type_to_role(
    file_type: str | None,
    source_type: str | None = SOURCE_TYPE_LEGACY,
) -> str:
    """
    Map coarse ``type`` (main/optional/patch) to a platform ``file_role``.

    Returns "" for legacy/unknown sources — soft migration / importers should
    set roles explicitly when richer taxonomy is available.
    """
    st = normalize_source_type(source_type)
    ft = normalize_file_type(file_type)
    if st == SOURCE_TYPE_STEAM:
        return FILE_ROLE_STEAM_CONTENT
    if st == SOURCE_TYPE_NEXUS:
        if ft == FILE_TYPE_MAIN:
            return FILE_ROLE_NEXUS_MAIN
        if ft in (FILE_TYPE_OPTIONAL, FILE_TYPE_PATCH):
            return FILE_ROLE_NEXUS_OPTIONAL
        return FILE_ROLE_NEXUS_MISC
    if st == SOURCE_TYPE_GITHUB:
        return FILE_ROLE_GITHUB_RELEASE_ASSET
    return ""


def resolve_selected_for_deploy(
    *,
    enabled: bool | None = None,
    selected_for_deploy: bool | None = None,
    selected_key_present: bool = False,
    enabled_key_present: bool = False,
) -> bool:
    """
    Resolve deploy-selection boolean with backward-compatible defaults.

    Prefer ``selected_for_deploy`` when that key is present; otherwise use
    ``enabled``; default True when neither is provided.
    """
    if selected_key_present and selected_for_deploy is not None:
        return bool(selected_for_deploy)
    if enabled_key_present and enabled is not None:
        return bool(enabled)
    if selected_for_deploy is not None:
        return bool(selected_for_deploy)
    if enabled is not None:
        return bool(enabled)
    return True


def is_entry_selected_for_deploy(entry: ModFileEntry) -> bool:
    """Prefer ``selected_for_deploy``; fall back to ``enabled`` if missing."""
    sel = getattr(entry, "selected_for_deploy", None)
    if sel is None:
        return bool(getattr(entry, "enabled", True))
    return bool(sel)


def default_selected_for_role(file_role: str | None, *, first_release_asset: bool = False) -> bool:
    """Platform default selection for a file_role (importer / Reset Default)."""
    role = normalize_file_role(file_role)
    if role == FILE_ROLE_STEAM_CONTENT:
        return True
    if role == FILE_ROLE_NEXUS_MAIN:
        return True
    if role in (
        FILE_ROLE_NEXUS_OPTIONAL,
        FILE_ROLE_NEXUS_MISC,
        FILE_ROLE_NEXUS_OLD,
        FILE_ROLE_GITHUB_DEVELOPER_BUILD,
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    ):
        return False
    if role == FILE_ROLE_GITHUB_RELEASE_ASSET:
        return bool(first_release_asset)
    if role == FILE_ROLE_UNKNOWN:
        # Other / unassigned — opt-in deploy only.
        return False
    return False


def new_file_id() -> str:
    return str(uuid.uuid4())


@dataclass
class ModFileEntry:
    """One archive / package inside a multi-file Mod (Nexus-style)."""

    id: str = ""
    name: str = ""
    filename: str = ""
    path: str = ""
    type: str = FILE_TYPE_MAIN  # main | optional | patch (kept for existing consumers)
    enabled: bool = True
    # None = inherit from ``enabled`` in ``__post_init__`` (constructor compat).
    selected_for_deploy: bool | None = None
    source_type: str = SOURCE_TYPE_LEGACY
    file_role: str = ""
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_file_id()
        self.type = normalize_file_type(self.type)
        self.source_type = normalize_source_type(self.source_type)
        self.file_role = normalize_file_role(self.file_role)
        if not self.file_role:
            self.file_role = map_file_type_to_role(self.type, self.source_type)
        if not self.filename and self.path:
            self.filename = Path(self.path).name
        if not self.name and self.filename:
            self.name = self.filename
        if not self.display_name:
            self.display_name = self.name or self.filename
        if not self.name and self.display_name:
            self.name = self.display_name
        if not isinstance(self.metadata, dict):
            if isinstance(self.metadata, Mapping):
                self.metadata = dict(self.metadata)
            else:
                self.metadata = {}
        # Keep enabled ↔ selected_for_deploy in lockstep.
        if self.selected_for_deploy is None:
            self.selected_for_deploy = bool(self.enabled)
        else:
            selected = bool(self.selected_for_deploy)
            self.selected_for_deploy = selected
            self.enabled = selected

    def set_selection(self, selected: bool) -> None:
        """Update both selection aliases together."""
        flag = bool(selected)
        self.selected_for_deploy = flag
        self.enabled = flag

    @property
    def is_selected(self) -> bool:
        """UI/deploy alias — prefer selected_for_deploy, fall back to enabled."""
        return is_entry_selected_for_deploy(self)

    def to_dict(self) -> dict[str, Any]:
        selected = is_entry_selected_for_deploy(self)
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "path": self.path,
            "type": self.type,
            "enabled": selected,
            "selected_for_deploy": selected,
            "source_type": self.source_type,
            "file_role": self.file_role,
            "display_name": self.display_name,
            "metadata": dict(self.metadata) if self.metadata else {},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModFileEntry:
        path = str(data.get("path") or "")
        filename = str(data.get("filename") or "") or (Path(path).name if path else "")
        name = str(data.get("name") or "")
        display_name = str(data.get("display_name") or "")
        raw_meta = data.get("metadata")
        metadata: dict[str, Any]
        if isinstance(raw_meta, Mapping):
            metadata = dict(raw_meta)
        else:
            metadata = {}
        # source_type: caller-provided wins; else legacy (old JSON has no key).
        raw_source = data.get("source_type", None)
        if raw_source is None or str(raw_source).strip() == "":
            source_type = SOURCE_TYPE_LEGACY
        else:
            source_type = normalize_source_type(str(raw_source))
        file_type = normalize_file_type(str(data.get("type") or FILE_TYPE_MAIN))
        raw_role = data.get("file_role", None)
        if raw_role is None or str(raw_role).strip() == "":
            file_role = map_file_type_to_role(file_type, source_type)
        else:
            file_role = normalize_file_role(str(raw_role))
        selected_key_present = "selected_for_deploy" in data
        enabled_key_present = "enabled" in data
        selected = resolve_selected_for_deploy(
            enabled=bool(data["enabled"]) if enabled_key_present else None,
            selected_for_deploy=(
                bool(data["selected_for_deploy"]) if selected_key_present else None
            ),
            selected_key_present=selected_key_present,
            enabled_key_present=enabled_key_present,
        )
        return cls(
            id=str(data.get("id") or "") or new_file_id(),
            name=name or display_name or filename,
            filename=filename,
            path=path,
            type=file_type,
            enabled=selected,
            selected_for_deploy=selected,
            source_type=source_type,
            file_role=file_role,
            display_name=display_name or name or filename,
            metadata=metadata,
        )


@dataclass
class ModFilesBundle:
    """
    Multi-file payload stored in ``mods.mod_files`` JSON.

    One Mod row owns many files — never split into multiple Mods.
    """

    files: list[ModFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"files": [f.to_dict() for f in self.files]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ModFilesBundle:
        if not data:
            return cls()
        raw_files = data.get("files") if isinstance(data, Mapping) else None
        files: list[ModFileEntry] = []
        if isinstance(raw_files, list):
            for item in raw_files:
                if isinstance(item, Mapping):
                    files.append(ModFileEntry.from_dict(item))
        return cls(files=files)

    @classmethod
    def from_json(cls, raw: str | None) -> ModFilesBundle:
        text = (raw or "").strip() or DEFAULT_MOD_FILES_JSON
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return cls()
        if isinstance(data, dict):
            return cls.from_dict(data)
        return cls()

    def enabled_files(self) -> list[ModFileEntry]:
        """Files with ``enabled`` true (kept in sync with selected_for_deploy)."""
        return [f for f in self.files if f.enabled]

    def selected_files(self) -> list[ModFileEntry]:
        """Files selected for deploy (selected_for_deploy, else enabled)."""
        return [f for f in self.files if is_entry_selected_for_deploy(f)]

    def find(self, file_id: str) -> ModFileEntry | None:
        fid = str(file_id or "").strip()
        for entry in self.files:
            if entry.id == fid:
                return entry
        return None
