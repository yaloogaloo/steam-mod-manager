"""Library source vs content-status model (Phase 5).

``source_type`` is sticky provenance (how the mod entered the library).
``content_status`` is recomputed from disk / backup / identity each reconcile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.mod_platform import NON_STEAM_MOD_ID_BASE, PLATFORM_STEAM

# --- Sticky source (library provenance) ---
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

# --- Content / lifecycle status ---
CONTENT_HEALTHY = "healthy"
CONTENT_FOLDER_MISSING = "folder_missing"
CONTENT_CONTENT_MISSING = "content_missing"
CONTENT_METADATA_MISSING = "metadata_missing"
CONTENT_BACKUP_INVALID = "backup_invalid"
CONTENT_IDENTITY_CONFLICT = "identity_conflict"

SUPPORTED_CONTENT_STATUSES = (
    CONTENT_HEALTHY,
    CONTENT_FOLDER_MISSING,
    CONTENT_CONTENT_MISSING,
    CONTENT_METADATA_MISSING,
    CONTENT_BACKUP_INVALID,
    CONTENT_IDENTITY_CONFLICT,
)

# --- Game list status ---
GAME_STATUS_HEALTHY = "healthy"
GAME_STATUS_MISSING_FOLDER = "missing_folder"

# Legacy ``library_status`` values (kept for older readers / tests)
LIBRARY_STATUS_NORMAL = "normal"
LIBRARY_STATUS_MISSING = "missing"
LIBRARY_STATUS_IMPORTED = "imported"
LIBRARY_STATUS_CONFLICT = "conflict"
LIBRARY_STATUS_BACKUP_INVALID = "backup_invalid"


def normalize_library_source(value: str | None) -> str:
    """Normalize sticky library source; empty / unknown → ``unknown`` (never Steam)."""
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
    """
    Resolve sticky source for one reconcile pass.

    Existing ``source_type`` always wins. Brand-new disk discovery uses
    ``external`` unless the payload clearly identifies Steam (or an empty
    payload with a workshop-range id). Store platforms in ``.info`` (nexus /
    github / …) do **not** override the sticky ``external`` entry origin.
    """
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
        # Empty / unknown payload: workshop-range ids default to steam
        if is_steam_workshop_id(mod_id):
            return SOURCE_STEAM
        return SOURCE_EXTERNAL

    # Existing DB row without sticky source — migrate once from platform
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
    if payload == SOURCE_STEAM or (
        payload in (SOURCE_NEXUS, SOURCE_MODIO, SOURCE_GITHUB)
    ):
        return payload
    if is_steam_workshop_id(mod_id):
        return SOURCE_STEAM
    if payload != SOURCE_UNKNOWN:
        return payload
    return SOURCE_UNKNOWN


def compute_content_status(
    *,
    folder_present: bool,
    identity_conflict: bool = False,
    backup_status: str = "",
    missing_content: bool = False,
    metadata_missing: bool = False,
) -> str:
    """Recompute content/lifecycle status from current disk + backup signals."""
    if identity_conflict:
        return CONTENT_IDENTITY_CONFLICT
    if not folder_present:
        if str(backup_status or "").strip() == "invalid":
            return CONTENT_BACKUP_INVALID
        return CONTENT_FOLDER_MISSING
    if metadata_missing:
        return CONTENT_METADATA_MISSING
    if missing_content:
        return CONTENT_CONTENT_MISSING
    # Live folder with content wins over a stale backup_status flag
    return CONTENT_HEALTHY


def content_status_to_library_status(content_status: str) -> str:
    """Map Phase-5 content_status → legacy library_status for older readers."""
    key = str(content_status or "").strip()
    return {
        CONTENT_HEALTHY: LIBRARY_STATUS_NORMAL,
        CONTENT_FOLDER_MISSING: LIBRARY_STATUS_MISSING,
        CONTENT_CONTENT_MISSING: LIBRARY_STATUS_MISSING,
        CONTENT_METADATA_MISSING: LIBRARY_STATUS_MISSING,
        CONTENT_BACKUP_INVALID: LIBRARY_STATUS_BACKUP_INVALID,
        CONTENT_IDENTITY_CONFLICT: LIBRARY_STATUS_CONFLICT,
    }.get(key, LIBRARY_STATUS_NORMAL)


def library_status_to_content_status(library_status: str) -> str:
    """Best-effort reverse map for rows that only have legacy library_status."""
    key = str(library_status or "").strip()
    return {
        LIBRARY_STATUS_NORMAL: CONTENT_HEALTHY,
        LIBRARY_STATUS_MISSING: CONTENT_FOLDER_MISSING,
        LIBRARY_STATUS_IMPORTED: CONTENT_HEALTHY,
        LIBRARY_STATUS_CONFLICT: CONTENT_IDENTITY_CONFLICT,
        LIBRARY_STATUS_BACKUP_INVALID: CONTENT_BACKUP_INVALID,
        CONTENT_HEALTHY: CONTENT_HEALTHY,
        CONTENT_FOLDER_MISSING: CONTENT_FOLDER_MISSING,
        CONTENT_CONTENT_MISSING: CONTENT_CONTENT_MISSING,
        CONTENT_METADATA_MISSING: CONTENT_METADATA_MISSING,
        CONTENT_BACKUP_INVALID: CONTENT_BACKUP_INVALID,
        CONTENT_IDENTITY_CONFLICT: CONTENT_IDENTITY_CONFLICT,
    }.get(key, CONTENT_HEALTHY)


def compute_game_status(library_root: str | Path, game_folder: str) -> str:
    folder = str(game_folder or "").strip()
    if not folder:
        return GAME_STATUS_HEALTHY
    path = Path(library_root) / folder
    if path.is_dir():
        return GAME_STATUS_HEALTHY
    return GAME_STATUS_MISSING_FOLDER


def source_badge_label(source_type: str | None) -> str:
    key = normalize_library_source(source_type)
    return {
        SOURCE_STEAM: "Steam",
        SOURCE_NEXUS: "Nexus",
        SOURCE_MODIO: "mod.io",
        SOURCE_GITHUB: "GitHub",
        SOURCE_EXTERNAL: "External",
        SOURCE_LOCAL: "Local",
        SOURCE_UNKNOWN: "Unknown",
    }.get(key, key.title() or "Unknown")


def content_status_badge_label(content_status: str | None) -> str:
    key = str(content_status or "").strip() or CONTENT_HEALTHY
    return {
        CONTENT_HEALTHY: "正常",
        CONTENT_FOLDER_MISSING: "目录缺失",
        CONTENT_CONTENT_MISSING: "文件缺失",
        CONTENT_METADATA_MISSING: "元数据缺失",
        CONTENT_BACKUP_INVALID: "备份损坏",
        CONTENT_IDENTITY_CONFLICT: "冲突",
    }.get(key, key)


def content_status_badge_tip(content_status: str | None) -> str:
    key = str(content_status or "").strip() or CONTENT_HEALTHY
    return {
        CONTENT_HEALTHY: "内容与元数据正常",
        CONTENT_FOLDER_MISSING: "Mod 目录不存在（仍可查看备份元数据）",
        CONTENT_CONTENT_MISSING: "Mod 目录存在但缺少有效内容文件",
        CONTENT_METADATA_MISSING: "目录中缺少 .info / metadata.json",
        CONTENT_BACKUP_INVALID: "Metadata backup 校验失败",
        CONTENT_IDENTITY_CONFLICT: "多个目录匹配同一 Mod 身份",
    }.get(key, "")


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
        return library_status_to_content_status(raw)
    return library_status_to_content_status(str(row.get("library_status") or ""))
