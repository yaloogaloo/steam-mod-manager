"""Library source + reduced content-status helpers.

ARCHITECTURE RULE
-----------------
``content_status`` ∈ {healthy, content_missing} only.
Identity / backup / offline are not Mod status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.mod_platform import NON_STEAM_MOD_ID_BASE, PLATFORM_STEAM
from services.status_authority import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_OK,
    IDENTITY_STATUS_UNRESOLVED,
    SUPPORTED_CONTENT_STATUSES,
    normalize_content_axis,
    normalize_identity_status,
)

__all__ = (
    "CONTENT_CONTENT_MISSING",
    "CONTENT_HEALTHY",
    "CONTENT_IDENTITY_CONFLICT",
    "GAME_STATUS_HEALTHY",
    "GAME_STATUS_MISSING_FOLDER",
    "IDENTITY_STATUS_CONFLICT",
    "IDENTITY_STATUS_OK",
    "IDENTITY_STATUS_UNRESOLVED",
    "LIBRARY_STATUS_IMPORTED",
    "LIBRARY_STATUS_MISSING",
    "LIBRARY_STATUS_NORMAL",
    "SOURCE_EXTERNAL",
    "SOURCE_GITHUB",
    "SOURCE_LOCAL",
    "SOURCE_MODIO",
    "SOURCE_NEXUS",
    "SOURCE_STEAM",
    "SOURCE_UNKNOWN",
    "SUPPORTED_CONTENT_STATUSES",
    "SUPPORTED_LIBRARY_SOURCES",
    "compute_content_status",
    "compute_game_status",
    "content_status_badge_label",
    "content_status_badge_tip",
    "content_status_to_library_status",
    "identity_status_badge_label",
    "identity_status_badge_tip",
    "infer_initial_source_type",
    "is_steam_workshop_id",
    "library_status_to_content_status",
    "normalize_library_source",
    "row_content_status",
    "row_identity_status",
    "row_source_type",
)

SOURCE_STEAM = "steam"
SOURCE_NEXUS = "nexus"
SOURCE_MODIO = "modio"
SOURCE_GITHUB = "github"
SOURCE_EXTERNAL = "external"
SOURCE_LOCAL = "local"
SOURCE_UNKNOWN = "unknown"

SUPPORTED_LIBRARY_SOURCES = (
    SOURCE_STEAM,
    SOURCE_NEXUS,
    SOURCE_MODIO,
    SOURCE_GITHUB,
    SOURCE_EXTERNAL,
    SOURCE_LOCAL,
    SOURCE_UNKNOWN,
)

# Identity fact token — not content_status.
CONTENT_IDENTITY_CONFLICT = IDENTITY_STATUS_CONFLICT

GAME_STATUS_HEALTHY = "healthy"
GAME_STATUS_MISSING_FOLDER = "missing_folder"

LIBRARY_STATUS_NORMAL = "normal"
LIBRARY_STATUS_MISSING = "missing"
LIBRARY_STATUS_IMPORTED = "imported"


def normalize_library_source(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return SOURCE_UNKNOWN
    if key in {"mod.io", "mod_io", "mod-io"}:
        return SOURCE_MODIO
    if key in {"其它", "其他", "other", "manual"}:
        return SOURCE_LOCAL
    if key in SUPPORTED_LIBRARY_SOURCES:
        return key
    return SOURCE_UNKNOWN


def is_steam_workshop_id(mod_id: int | str) -> bool:
    text = str(mod_id or "").strip()
    if not text.isdigit():
        return False
    mid = int(text)
    return mid > 0 and mid < int(NON_STEAM_MOD_ID_BASE)


def infer_initial_source_type(
    *,
    mod_id: int | str,
    had_row: bool,
    existing_source: str = "",
    existing_platform: str = "",
    payload_source: str = "",
) -> str:
    sticky = normalize_library_source(existing_source)
    if sticky != SOURCE_UNKNOWN:
        return sticky

    payload = normalize_library_source(payload_source)

    if not had_row:
        if payload == SOURCE_STEAM:
            return SOURCE_STEAM
        if payload in (
            SOURCE_NEXUS,
            SOURCE_MODIO,
            SOURCE_GITHUB,
            SOURCE_EXTERNAL,
            SOURCE_LOCAL,
        ):
            return SOURCE_EXTERNAL
        if is_steam_workshop_id(mod_id):
            return SOURCE_STEAM
        return SOURCE_EXTERNAL

    plat = normalize_library_source(existing_platform)
    if plat in (
        SOURCE_STEAM,
        SOURCE_NEXUS,
        SOURCE_MODIO,
        SOURCE_GITHUB,
        SOURCE_EXTERNAL,
        SOURCE_LOCAL,
    ):
        return plat
    if payload != SOURCE_UNKNOWN:
        return payload
    return SOURCE_UNKNOWN


def compute_content_status(
    *,
    folder_present: bool,
    backup_status: str = "",
    missing_content: bool = False,
    metadata_missing: bool = False,
) -> str:
    """
    Reduced Mod content axis: healthy | content_missing.

    ``backup_status`` / ``metadata_missing`` are ignored for Mod status
    (backup stays in backup_status; metadata is diagnostic only).
    """
    del backup_status, metadata_missing  # not Mod status inputs
    if not folder_present or missing_content:
        return CONTENT_CONTENT_MISSING
    return CONTENT_HEALTHY


def content_status_to_library_status(content_status: str) -> str:
    key = normalize_content_axis(content_status)
    if key == CONTENT_CONTENT_MISSING:
        return LIBRARY_STATUS_MISSING
    return LIBRARY_STATUS_NORMAL


def library_status_to_content_status(library_status: str) -> str:
    """Legacy library_status → content axis (missing only; no deleted tokens)."""
    key = str(library_status or "").strip().lower()
    if key in {"missing", "content_missing"}:
        return CONTENT_CONTENT_MISSING
    return CONTENT_HEALTHY


def compute_game_status(library_root: str | Path, game_folder: str) -> str:
    folder = str(game_folder or "").strip()
    if not folder:
        return GAME_STATUS_HEALTHY
    path = Path(library_root) / folder
    if path.is_dir():
        return GAME_STATUS_HEALTHY
    return GAME_STATUS_MISSING_FOLDER


def content_status_badge_label(content_status: str | None) -> str:
    key = normalize_content_axis(content_status)
    return {
        CONTENT_HEALTHY: "正常",
        CONTENT_CONTENT_MISSING: "内容缺失",
    }.get(key, "")


def content_status_badge_tip(content_status: str | None) -> str:
    key = normalize_content_axis(content_status)
    return {
        CONTENT_HEALTHY: "内容正常",
        CONTENT_CONTENT_MISSING: "Mod 内容不存在或无法使用",
    }.get(key, "")


def identity_status_badge_label(identity_status: str | None) -> str:
    """Identity is never a user-facing Mod badge — always empty."""
    del identity_status
    return ""


def identity_status_badge_tip(identity_status: str | None) -> str:
    """Identity is never a user-facing Mod badge — always empty."""
    del identity_status
    return ""


def row_source_type(row: dict[str, Any] | None) -> str:
    if not row:
        return SOURCE_UNKNOWN
    raw = str(row.get("source_type") or "").strip()
    if raw:
        return normalize_library_source(raw)
    return normalize_library_source(str(row.get("platform") or ""))


def row_content_status(row: dict[str, Any] | None) -> str:
    if not row:
        return CONTENT_HEALTHY
    raw = str(row.get("content_status") or "").strip()
    if raw:
        return normalize_content_axis(raw)
    return library_status_to_content_status(str(row.get("library_status") or ""))


def row_identity_status(row: dict[str, Any] | None) -> str:
    if not row:
        return IDENTITY_STATUS_OK
    raw = str(row.get("identity_status") or "").strip()
    if raw:
        return normalize_identity_status(raw)
    return IDENTITY_STATUS_OK
