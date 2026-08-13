"""Pure helpers for Mod Detail Files UX (Phase E3) — no Qt, no DB writes."""

from __future__ import annotations

from typing import Any, Sequence

from core.mod_platform import (
    FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_MISC,
    FILE_ROLE_NEXUS_OLD,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_UNKNOWN,
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_MODIO,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_OTHER,
    SOURCE_TYPE_STEAM,
    SUPPORTED_PLATFORMS,
    is_entry_selected_for_deploy,
    normalize_file_role,
    normalize_source_type,
)

# Group keys
GROUP_STEAM_FILES = "steam_files"
GROUP_NEXUS_MAIN = "nexus_main"
GROUP_NEXUS_OPTIONAL = "nexus_optional"
GROUP_NEXUS_MISC = "nexus_misc"
GROUP_NEXUS_OLD = "nexus_old"
GROUP_GITHUB_RELEASE = "github_release"
GROUP_GITHUB_OTHER = "github_other"
GROUP_MODIO_FILES = "modio_files"
GROUP_OTHER_FILES = "other_files"
GROUP_FILES = "files"

_GROUP_ORDER = (
    GROUP_STEAM_FILES,
    GROUP_NEXUS_MAIN,
    GROUP_NEXUS_OPTIONAL,
    GROUP_NEXUS_MISC,
    GROUP_NEXUS_OLD,
    GROUP_GITHUB_RELEASE,
    GROUP_GITHUB_OTHER,
    GROUP_MODIO_FILES,
    GROUP_OTHER_FILES,
    GROUP_FILES,
)

_SOURCE_LABELS = {
    SOURCE_TYPE_STEAM: "Steam",
    SOURCE_TYPE_NEXUS: "Nexus",
    SOURCE_TYPE_GITHUB: "GitHub",
    SOURCE_TYPE_MODIO: "mod.io",
    SOURCE_TYPE_OTHER: "其它",
    "legacy": "Legacy",
}

_ROLE_LABELS = {
    FILE_ROLE_STEAM_CONTENT: "Workshop Content",
    FILE_ROLE_NEXUS_MAIN: "Main File",
    FILE_ROLE_NEXUS_OPTIONAL: "Optional File",
    FILE_ROLE_NEXUS_MISC: "Misc File",
    FILE_ROLE_NEXUS_OLD: "Old File",
    FILE_ROLE_GITHUB_RELEASE_ASSET: "Release Asset",
    FILE_ROLE_GITHUB_DEVELOPER_BUILD: "Development",
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE: "Source",
    FILE_ROLE_UNKNOWN: "Unknown",
}

_GROUP_TITLES = {
    GROUP_STEAM_FILES: "Workshop Files",
    GROUP_NEXUS_MAIN: "Main Files",
    GROUP_NEXUS_OPTIONAL: "Optional Files",
    GROUP_NEXUS_MISC: "Misc",
    GROUP_NEXUS_OLD: "Old",
    GROUP_GITHUB_RELEASE: "Release Assets",
    GROUP_GITHUB_OTHER: "Other",
    GROUP_MODIO_FILES: "mod.io Files",
    GROUP_OTHER_FILES: "其它 Files",
    GROUP_FILES: "Files",
}


def file_primary_label(entry: Any) -> str:
    """Visible primary text: display_name → name → filename."""
    for attr in ("display_name", "name", "filename"):
        value = str(getattr(entry, attr, "") or "").strip()
        if value:
            return value
    return "File"


def file_combo_label(entry: Any) -> str:
    """
    Combo display text — physical archive/file name only (1:1, no soft rename).

    Prefer ``filename``, then the basename of ``path``. Never use
    ``display_name`` / ``name`` (those may collide across distinct archives).
    """
    filename = str(getattr(entry, "filename", "") or "").strip()
    if filename:
        return filename
    path = str(getattr(entry, "path", "") or "").strip().replace("\\", "/")
    if path:
        base = path.rsplit("/", 1)[-1].strip()
        if base:
            return base
    return str(getattr(entry, "id", "") or "File")


def file_source_label(source_type: str | None) -> str:
    key = normalize_source_type(source_type)
    return _SOURCE_LABELS.get(key, key.title() if key else "")


def file_role_label(file_role: str | None) -> str:
    role = normalize_file_role(file_role)
    if not role:
        return ""
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


META_FILE_DESCRIPTION = "description"

# Roles shown with a compact Main / Source badge (no free-form note UI).
_BADGE_MAIN_ROLES = frozenset(
    {
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_GITHUB_RELEASE_ASSET,
        FILE_ROLE_STEAM_CONTENT,
    }
)
_BADGE_SOURCE_ROLES = frozenset({FILE_ROLE_GITHUB_SOURCE_ARCHIVE})


def file_badge_kind(entry: Any) -> str | None:
    """
    Return ``\"Main\"`` / ``\"Source\"`` for badge rows, or ``None`` for Other.

    Other rows show a gray description + edit control instead of a badge.
    """
    role = normalize_file_role(getattr(entry, "file_role", None))
    if role in _BADGE_MAIN_ROLES:
        return "Main"
    if role in _BADGE_SOURCE_ROLES:
        return "Source"
    return None


def file_list_badge(
    entry: Any, platform: str | None = None
) -> tuple[str | None, str | None]:
    """
    Flat-list badge for detail Files rows.

    Returns ``(badge_text, style)`` where *style* is ``\"main\"`` (green) or
    ``\"alt\"`` (non-main Nexus / GitHub Source), or ``(None, None)`` when no
    badge (GitHub Other only).

    Nexus: only the 5-category whitelist appears in the badge.
    """
    plat = str(platform or "").strip().lower()
    source = normalize_source_type(getattr(entry, "source_type", None))
    if plat == PLATFORM_NEXUS or source == SOURCE_TYPE_NEXUS:
        text = normalize_nexus_category(nexus_raw_category(entry))
        if text == "Main":
            return (text, "main")
        return (text, "alt")

    kind = file_badge_kind(entry)
    if kind == "Main":
        return ("Main", "main")
    if kind == "Source":
        return ("Source", "alt")
    return (None, None)


def sort_files_for_detail(files: Sequence[Any]) -> list[Any]:
    """Main first → Other middle → Source last (stable by physical filename)."""

    def _key(entry: Any) -> tuple[int, str]:
        kind = file_badge_kind(entry)
        if kind == "Main":
            tier = 0
        elif kind == "Source":
            tier = 2
        else:
            tier = 1
        return (tier, file_combo_label(entry).casefold())

    return sorted(list(files or []), key=_key)


def sort_files_for_nexus_flat(files: Sequence[Any]) -> list[Any]:
    """Flatten Nexus category groups: Main → Optional → … (stable within group)."""
    ordered: list[Any] = []
    for _cat, entries in group_nexus_file_entries(files):
        ordered.extend(entries)
    return ordered


def file_description(entry: Any) -> str:
    """User note for Other-type files (``metadata.description``)."""
    meta = getattr(entry, "metadata", None)
    if isinstance(meta, dict):
        return str(meta.get(META_FILE_DESCRIPTION) or "").strip()
    return ""


def file_secondary_label(entry: Any) -> str:
    """Secondary row text: ``Nexus · Main File`` (no path/id)."""
    source = file_source_label(getattr(entry, "source_type", None))
    role = file_role_label(getattr(entry, "file_role", None))
    if not role:
        coarse = str(getattr(entry, "type", "") or "").strip()
        if coarse:
            role = coarse.replace("_", " ").title()
    if source and role:
        return f"{source} · {role}"
    return source or role or ""


def file_tooltip(entry: Any) -> str:
    """Technical details for hover only — not shown in the row body."""
    lines: list[str] = []
    filename = str(getattr(entry, "filename", "") or "").strip()
    path = str(getattr(entry, "path", "") or "").strip()
    source = str(getattr(entry, "source_type", "") or "").strip()
    role = str(getattr(entry, "file_role", "") or "").strip()
    fid = str(getattr(entry, "id", "") or "").strip()
    if filename:
        lines.append(f"Filename: {filename}")
    if path:
        lines.append(f"Path: {path}")
    if source:
        lines.append(f"Source: {source}")
    if role:
        lines.append(f"Role: {role}")
    meta = getattr(entry, "metadata", None)
    if isinstance(meta, dict) and meta:
        version = meta.get("version") or meta.get("file_version") or meta.get("Version")
        if version:
            lines.append(f"Version: {version}")
        for key in ("file_id", "nexus_file_id", "asset_id", "id"):
            mid = meta.get(key)
            if mid is not None and str(mid).strip():
                lines.append(f"{key}: {mid}")
                break
    if fid:
        lines.append(f"Id: {fid}")
    return "\n".join(lines) if lines else file_primary_label(entry)


def file_group_key(entry: Any, platform: str | None) -> str:
    """
    Group key for Files workspace.

    Steam → single ``Workshop Files`` bucket (no Nexus-style categories).
    Nexus → Main / Optional / Misc / Old.
    GitHub → Release Assets / Other (dev + source under Other).
    Missing role → ``Files``.
    """
    plat = str(platform or "").strip().lower()
    if plat not in SUPPORTED_PLATFORMS:
        plat = ""
    role = normalize_file_role(getattr(entry, "file_role", None))
    source = normalize_source_type(getattr(entry, "source_type", None))

    # Explicit Steam only — do not invent Steam grouping for unknown platforms.
    if plat == PLATFORM_STEAM or source == SOURCE_TYPE_STEAM or role == FILE_ROLE_STEAM_CONTENT:
        return GROUP_STEAM_FILES

    if plat == PLATFORM_NEXUS or source == SOURCE_TYPE_NEXUS:
        if role == FILE_ROLE_NEXUS_MAIN:
            return GROUP_NEXUS_MAIN
        if role == FILE_ROLE_NEXUS_OPTIONAL:
            return GROUP_NEXUS_OPTIONAL
        if role == FILE_ROLE_NEXUS_MISC:
            return GROUP_NEXUS_MISC
        if role == FILE_ROLE_NEXUS_OLD:
            return GROUP_NEXUS_OLD
        if not role:
            coarse = str(getattr(entry, "type", "") or "").strip().lower()
            if coarse == "main":
                return GROUP_NEXUS_MAIN
            if coarse in ("optional", "patch"):
                return GROUP_NEXUS_OPTIONAL
            return GROUP_FILES
        return GROUP_FILES

    if plat == PLATFORM_GITHUB or source == SOURCE_TYPE_GITHUB:
        if role == FILE_ROLE_GITHUB_RELEASE_ASSET:
            return GROUP_GITHUB_RELEASE
        if role in (
            FILE_ROLE_GITHUB_DEVELOPER_BUILD,
            FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        ):
            return GROUP_GITHUB_OTHER
        if not role:
            return GROUP_FILES
        return GROUP_GITHUB_OTHER

    if plat == PLATFORM_MODIO or source == SOURCE_TYPE_MODIO:
        return GROUP_MODIO_FILES

    if plat == PLATFORM_OTHER or source == SOURCE_TYPE_OTHER:
        return GROUP_OTHER_FILES

    if role == FILE_ROLE_NEXUS_MAIN:
        return GROUP_NEXUS_MAIN
    if role == FILE_ROLE_NEXUS_OPTIONAL:
        return GROUP_NEXUS_OPTIONAL
    if role == FILE_ROLE_NEXUS_MISC:
        return GROUP_NEXUS_MISC
    if role == FILE_ROLE_NEXUS_OLD:
        return GROUP_NEXUS_OLD
    if role == FILE_ROLE_GITHUB_RELEASE_ASSET:
        return GROUP_GITHUB_RELEASE
    if role in (FILE_ROLE_GITHUB_DEVELOPER_BUILD, FILE_ROLE_GITHUB_SOURCE_ARCHIVE):
        return GROUP_GITHUB_OTHER
    return GROUP_FILES


def file_group_title(key: str) -> str:
    return _GROUP_TITLES.get(str(key or "").strip(), "Files")


# Nexus flat-list badges — strict 5-category whitelist only.
NEXUS_CATEGORY_MAIN = "Main"
NEXUS_CATEGORY_OPTIONAL = "Optional"
NEXUS_CATEGORY_MISC = "Miscellaneous"
NEXUS_CATEGORY_I18N = "汉化"
NEXUS_CATEGORY_OTHER = "Other"
NEXUS_CATEGORY_WHITELIST = (
    NEXUS_CATEGORY_MAIN,
    NEXUS_CATEGORY_OPTIONAL,
    NEXUS_CATEGORY_MISC,
    NEXUS_CATEGORY_I18N,
    NEXUS_CATEGORY_OTHER,
)

_NEXUS_ROLE_RAW = {
    FILE_ROLE_NEXUS_MAIN: "Main",
    FILE_ROLE_NEXUS_OPTIONAL: "Optional",
    FILE_ROLE_NEXUS_MISC: "Miscellaneous",
    FILE_ROLE_NEXUS_OLD: "Old",
}

_NEXUS_TREE_CATEGORY = {
    GROUP_NEXUS_MAIN: "Main",
    GROUP_NEXUS_OPTIONAL: "Optional",
    GROUP_NEXUS_MISC: "Miscellaneous",
    GROUP_NEXUS_OLD: "Old",
    GROUP_FILES: "Other",
}


def normalize_nexus_category(file_category: str | None) -> str:
    """
    Force Nexus category into the 5-name whitelist.

    Anything else (示例 / Old / …) → ``Other``.
    """
    raw_cat = str(file_category or "").lower()
    if "main" in raw_cat:
        return NEXUS_CATEGORY_MAIN
    if "optional" in raw_cat:
        return NEXUS_CATEGORY_OPTIONAL
    if "miscellaneous" in raw_cat:
        return NEXUS_CATEGORY_MISC
    if "汉化" in raw_cat:
        return NEXUS_CATEGORY_I18N
    return NEXUS_CATEGORY_OTHER


def nexus_raw_category(entry: Any, *, platform: str | None = PLATFORM_NEXUS) -> str:
    """Raw category hint before whitelist (metadata / folder / role)."""
    meta = getattr(entry, "metadata", None)
    if isinstance(meta, dict):
        for key in ("category", "nexus_category", "category_name"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
    path = str(getattr(entry, "path", "") or "").strip().replace("\\", "/")
    parts = [p for p in path.split("/") if p and p not in {".", ".."}]
    if len(parts) >= 2:
        folder = parts[0].strip()
        if folder and folder not in {".info", "info", "历史版本"}:
            lower = folder.lower()
            if not lower.endswith((".zip", ".7z", ".rar")):
                return folder
    role = normalize_file_role(getattr(entry, "file_role", None))
    if role in _NEXUS_ROLE_RAW:
        return _NEXUS_ROLE_RAW[role]
    coarse = str(getattr(entry, "type", "") or "").strip()
    if coarse:
        return coarse
    key = file_group_key(entry, platform or PLATFORM_NEXUS)
    return _NEXUS_TREE_CATEGORY.get(key, "Other")


def nexus_category_label(entry: Any, *, platform: str | None = PLATFORM_NEXUS) -> str:
    """Whitelisted Nexus badge category (Main / Optional / Miscellaneous / 汉化 / Other)."""
    return normalize_nexus_category(nexus_raw_category(entry, platform=platform))


def group_nexus_file_entries(files: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    """Group Nexus files by whitelisted category (empty groups omitted)."""
    buckets: dict[str, list[Any]] = {k: [] for k in NEXUS_CATEGORY_WHITELIST}
    for entry in files or []:
        cat = nexus_category_label(entry)
        buckets.setdefault(cat, []).append(entry)
    return [(name, buckets[name]) for name in NEXUS_CATEGORY_WHITELIST if buckets.get(name)]


def is_nexus_main_category(category_name: str) -> bool:
    """True only for whitelisted ``Main`` (after normalize)."""
    return normalize_nexus_category(category_name) == NEXUS_CATEGORY_MAIN


def nexus_badge_object_name(category: str) -> str:
    """QSS objectName for Nexus category badges (Main = green)."""
    cat = normalize_nexus_category(category)
    return {
        NEXUS_CATEGORY_MAIN: "detailFileBadgeMain",
        NEXUS_CATEGORY_OPTIONAL: "detailFileBadgeOptional",
        NEXUS_CATEGORY_MISC: "detailFileBadgeMisc",
        NEXUS_CATEGORY_I18N: "detailFileBadgeI18n",
        NEXUS_CATEGORY_OTHER: "detailFileBadgeOther",
    }.get(cat, "detailFileBadgeOther")


def group_file_entries(
    files: Sequence[Any],
    platform: str | None,
) -> list[tuple[str, list[Any]]]:
    """Return ordered ``(group_title, entries)`` pairs (empty groups omitted)."""
    buckets: dict[str, list[Any]] = {k: [] for k in _GROUP_ORDER}
    for entry in files:
        key = file_group_key(entry, platform)
        buckets.setdefault(key, []).append(entry)
    result: list[tuple[str, list[Any]]] = []
    seen: set[str] = set()
    for key in _GROUP_ORDER:
        entries = buckets.get(key) or []
        if not entries:
            continue
        result.append((file_group_title(key), entries))
        seen.add(key)
    for key, entries in buckets.items():
        if key in seen or not entries:
            continue
        result.append((file_group_title(key), entries))
    return result


def files_summary_lines(
    selected_n: int,
    total_n: int,
    *,
    legacy_workshop: bool = False,
) -> tuple[str, str]:
    """
    Return ``(summary, status)`` for the Files header.

    - legacy empty Steam: Workshop Content / Full folder deploy + Ready
    - none selected: No files selected / Deployment Empty
    - otherwise: Ready
    """
    if legacy_workshop:
        return ("Workshop Content · Full folder deploy", "Ready")
    summary = f"Selected {int(selected_n)}/{int(total_n)}"
    if total_n <= 0:
        return (summary, "Deployment Empty")
    if selected_n <= 0:
        return (f"{summary} · No files selected", "Deployment Empty")
    return (summary, "Ready")


def count_selected(files: Sequence[Any]) -> int:
    return sum(1 for f in files if is_entry_selected_for_deploy(f))
