"""Pure Mod Library search / filter / sort helpers (no network, no archive)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    normalize_platform,
)
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_WARNING,
    normalize_conflict_status,
)

FILTER_ALL = "all"
FILTER_FAVORITE = "favorite"
FILTER_DEPLOYED = "deployed"
FILTER_OFFLINE_PRESENT = "offline_present"
FILTER_OFFLINE_MISSING = "offline_missing"
FILTER_INVALID = "invalid"
FILTER_CONFLICT = "conflict"
FILTER_DISABLED = "disabled"
FILTER_PLATFORM_ALL = "platform_all"
FILTER_PLATFORM_STEAM = "platform_steam"
FILTER_PLATFORM_NEXUS = "platform_nexus"
FILTER_PLATFORM_GITHUB = "platform_github"
FILTER_PLATFORM_OTHER = "platform_other"
FILTER_CATEGORY_ALL = "category_all"

SORT_MTIME = "mtime"
SORT_NAME = "name"


def resolve_mod_library_title(
    *,
    metadata_display_name: str = "",
    metadata_title: str = "",
    db_display_name: str = "",
    db_steam_name: str = "",
    folder_name: str = "",
) -> str:
    """
    Library card / filter title priority (UI read only)::

        metadata.display_name
            > DB display_name
            > DB steam_name / metadata.title
            > folder name
    """
    for candidate in (
        metadata_display_name,
        db_display_name,
        db_steam_name,
        metadata_title,
        folder_name,
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return "—"


# Status chips (exclusive within group).
STATUS_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    (FILTER_ALL, "全部"),
    (FILTER_FAVORITE, "收藏"),
    (FILTER_DEPLOYED, "已部署"),
    (FILTER_INVALID, "失效"),
    (FILTER_CONFLICT, "冲突"),
    (FILTER_DISABLED, "已禁用"),
    (FILTER_OFFLINE_MISSING, "离线页面缺失"),
)

# Platform chips (exclusive within group) — combine with status.
PLATFORM_FILTER_LABELS: tuple[tuple[str, str], ...] = (
    (FILTER_PLATFORM_ALL, "全部平台"),
    (FILTER_PLATFORM_STEAM, "Steam"),
    (FILTER_PLATFORM_NEXUS, "Nexus"),
    (FILTER_PLATFORM_GITHUB, "GitHub"),
)

# Back-compat flat list (status then platform) for older callers.
FILTER_LABELS: tuple[tuple[str, str], ...] = (
    *STATUS_FILTER_LABELS,
    *PLATFORM_FILTER_LABELS,
)

SORT_LABELS: tuple[tuple[str, str], ...] = (
    (SORT_MTIME, "最近修改"),
    (SORT_NAME, "名称"),
)


@dataclass(frozen=True)
class ModFilterIndex:
    """Cached fields used for library search / filter / sort."""

    mod_id: str
    display_name: str
    steam_name: str
    notes: str
    game_name: str
    favorite: bool
    deployed: bool
    has_offline: bool
    mtime: float
    sort_name: str
    invalid: bool = False
    conflict: bool = False
    tag_values: str = ""
    platform: str = PLATFORM_STEAM
    source_url: str = ""
    external_id: str = ""
    is_invalid: bool = False
    conflict_status: str = "none"
    enabled: bool = True
    category_tags: str = ""


def offline_page_exists(managed_path: Path) -> bool:
    """True when an offline page file exists — ``is_file`` only (no content read)."""
    from services.offline.paths import offline_page_file_exists

    return offline_page_file_exists(managed_path)


def folder_mtime(managed_path: Path) -> float:
    try:
        return float(Path(managed_path).stat().st_mtime)
    except OSError:
        return 0.0


def matches_search(index: ModFilterIndex, query: str) -> bool:
    q = (query or "").strip().casefold()
    if not q:
        return True
    haystacks = (
        index.display_name,
        index.steam_name,
        index.notes,
        index.mod_id,
        index.game_name,
        index.tag_values,
        index.category_tags,
        index.source_url,
        index.external_id,
        index.platform,
    )
    return any(q in (h or "").casefold() for h in haystacks)


def matches_status_filter(index: ModFilterIndex, filter_key: str) -> bool:
    key = filter_key or FILTER_ALL
    if key == FILTER_ALL:
        return True
    if key == FILTER_FAVORITE:
        return bool(index.favorite)
    if key == FILTER_DEPLOYED:
        return bool(index.deployed)
    if key == FILTER_OFFLINE_PRESENT:
        return bool(index.has_offline)
    if key == FILTER_OFFLINE_MISSING:
        return not bool(index.has_offline)
    if key == FILTER_INVALID:
        return bool(index.is_invalid or index.invalid)
    if key == FILTER_CONFLICT:
        status = normalize_conflict_status(index.conflict_status)
        return bool(index.conflict) or status in (
            CONFLICT_STATUS_CONFLICT,
            CONFLICT_STATUS_WARNING,
        )
    if key == FILTER_DISABLED:
        return not bool(index.enabled)
    # Platform keys accidentally passed as status → defer to platform matcher
    if key in (
        FILTER_PLATFORM_ALL,
        FILTER_PLATFORM_STEAM,
        FILTER_PLATFORM_NEXUS,
        FILTER_PLATFORM_GITHUB,
    ):
        return matches_platform_filter(index, key)
    return True


def matches_platform_filter(index: ModFilterIndex, platform_key: str) -> bool:
    key = platform_key or FILTER_PLATFORM_ALL
    if key in (FILTER_ALL, FILTER_PLATFORM_ALL, ""):
        return True
    legacy = {
        FILTER_PLATFORM_STEAM: PLATFORM_STEAM,
        FILTER_PLATFORM_NEXUS: PLATFORM_NEXUS,
        FILTER_PLATFORM_GITHUB: PLATFORM_GITHUB,
    }
    if key in legacy:
        return normalize_platform(index.platform) == legacy[key]
    return normalize_platform(index.platform) == normalize_platform(key)


def matches_category_filter(index: ModFilterIndex, category_key: str) -> bool:
    key = (category_key or FILTER_CATEGORY_ALL).strip()
    if key in ("", FILTER_ALL, FILTER_CATEGORY_ALL, "全部标签"):
        return True
    tags = (index.category_tags or "").casefold().split()
    needle = key.casefold()
    return needle in tags or needle in (index.category_tags or "").casefold()


def sort_key(index: ModFilterIndex, sort_mode: str):
    mode = sort_mode or SORT_MTIME
    if mode == SORT_NAME:
        return (index.sort_name.casefold(), index.mod_id)
    return (-index.mtime, index.sort_name.casefold(), index.mod_id)


def filter_and_sort(
    entries: list[tuple[ModFilterIndex, object]],
    *,
    query: str = "",
    filter_key: str = FILTER_ALL,
    platform_key: str = FILTER_PLATFORM_ALL,
    category_key: str = FILTER_CATEGORY_ALL,
    sort_mode: str = SORT_MTIME,
) -> list[object]:
    """
    Filter ``(index, payload)`` pairs and return payloads in sort order.

    Status / platform / category combine (AND) — e.g. Nexus + Gameplay + 冲突.
    """
    matched: list[tuple[ModFilterIndex, object]] = []
    for index, payload in entries:
        if not matches_search(index, query):
            continue
        if not matches_status_filter(index, filter_key):
            continue
        if not matches_platform_filter(index, platform_key):
            continue
        if not matches_category_filter(index, category_key):
            continue
        matched.append((index, payload))
    matched.sort(key=lambda pair: sort_key(pair[0], sort_mode))
    return [payload for _, payload in matched]
