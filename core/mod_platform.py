"""Platform / source-URL / multi-file helpers for generic Mod identity."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

PLATFORM_STEAM = "steam"
PLATFORM_NEXUS = "nexus"
PLATFORM_GITHUB = "github"
SUPPORTED_PLATFORMS = (PLATFORM_STEAM, PLATFORM_NEXUS, PLATFORM_GITHUB)

FILE_TYPE_MAIN = "main"
FILE_TYPE_OPTIONAL = "optional"
FILE_TYPE_PATCH = "patch"
SUPPORTED_FILE_TYPES = (FILE_TYPE_MAIN, FILE_TYPE_OPTIONAL, FILE_TYPE_PATCH)

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
PROVIDER_GITHUB_SNAPSHOT = "github_snapshot"
# Legacy generator ids — kept for rows written before snapshot providers.
PROVIDER_NEXUS_GENERATOR = "nexus_generator"
PROVIDER_GITHUB_GENERATOR = "github_generator"

PROVIDER_DISPLAY_NAMES = {
    PROVIDER_STEAM_ARCHIVE: "Steam Archive",
    PROVIDER_NEXUS_SNAPSHOT: "Nexus Snapshot",
    PROVIDER_GITHUB_SNAPSHOT: "GitHub Snapshot",
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


def normalize_platform(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_PLATFORMS:
        return key
    return PLATFORM_STEAM


def normalize_file_type(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_FILE_TYPES:
        return key
    if key in ("update", "addon"):
        return FILE_TYPE_OPTIONAL
    return FILE_TYPE_MAIN


def new_file_id() -> str:
    return str(uuid.uuid4())


@dataclass
class ModFileEntry:
    """One archive / package inside a multi-file Mod (Nexus-style)."""

    id: str = ""
    name: str = ""
    filename: str = ""
    path: str = ""
    type: str = FILE_TYPE_MAIN  # main | optional | patch
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_file_id()
        self.type = normalize_file_type(self.type)
        if not self.filename and self.path:
            self.filename = Path(self.path).name
        if not self.name and self.filename:
            self.name = self.filename

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "path": self.path,
            "type": self.type,
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModFileEntry:
        path = str(data.get("path") or "")
        filename = str(data.get("filename") or "") or (Path(path).name if path else "")
        return cls(
            id=str(data.get("id") or "") or new_file_id(),
            name=str(data.get("name") or "") or filename,
            filename=filename,
            path=path,
            type=normalize_file_type(str(data.get("type") or FILE_TYPE_MAIN)),
            enabled=bool(data.get("enabled", True)),
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
        return [f for f in self.files if f.enabled]

    def find(self, file_id: str) -> ModFileEntry | None:
        fid = str(file_id or "").strip()
        for entry in self.files:
            if entry.id == fid:
                return entry
        return None
