"""Platform-agnostic UI labels and external-id helpers for Mod identity."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    normalize_platform,
)


@dataclass(frozen=True)
class PlatformMetadataLabels:
    """Detail-panel field captions for one platform (no UI branching needed)."""

    name: str
    external_id: str
    platform: str
    source: str
    badge: str


def platform_badge_label(platform: str | None) -> str:
    """Short card badge text (keeps cards narrow)."""
    from services.platform_identity import format_platform_label

    return format_platform_label(platform)


def format_platform_name(platform: str | None) -> str:
    """Human-readable platform name for detail / clipboard."""
    from services.platform_identity import format_platform_label, normalize_platform
    from core.mod_platform import (
        PLATFORM_GITHUB,
        PLATFORM_MODIO,
        PLATFORM_NEXUS,
        PLATFORM_OTHER,
        PLATFORM_STEAM,
    )

    key = normalize_platform(platform)
    # Detail uses longer Steam label
    return {
        PLATFORM_STEAM: "Steam Workshop",
        PLATFORM_NEXUS: "Nexus Mods",
        PLATFORM_GITHUB: "GitHub",
        PLATFORM_MODIO: "mod.io",
        PLATFORM_OTHER: "其它",
    }.get(key, format_platform_label(key))


def get_platform_metadata_labels(platform: str | None) -> PlatformMetadataLabels:
    key = normalize_platform(platform)
    id_labels = {
        PLATFORM_STEAM: "Workshop ID",
        PLATFORM_NEXUS: "Nexus Mod ID",
        PLATFORM_GITHUB: "GitHub Repository",
        PLATFORM_MODIO: "mod.io ID",
        PLATFORM_OTHER: "本地标识",
    }
    return PlatformMetadataLabels(
        name="名称",
        external_id=id_labels.get(key, "External ID"),
        platform="平台",
        source="来源",
        badge=platform_badge_label(key),
    )


def format_external_id(
    platform: str | None,
    value: str = "",
    *,
    source_url: str = "",
    published_file_id: str = "",
) -> str:
    """
    Canonical external identity for display / clipboard.

    Never returns the internal high-range SQLite ``mod_id`` for Nexus/GitHub.
    When ``value`` is empty, may parse from ``source_url``.
    """
    key = normalize_platform(platform)
    ext = str(value or "").strip()
    if not ext:
        ext = parse_external_id_from_url(key, source_url)
    pub = str(published_file_id or "").strip()
    if key == PLATFORM_STEAM:
        return ext or pub
    return ext


def parse_external_id_from_url(platform: str | None, url: str = "") -> str:
    """Extract platform external id from a source URL (or bare id / owner/repo)."""
    key = normalize_platform(platform)
    text = str(url or "").strip()
    if not text:
        return ""
    if key == PLATFORM_NEXUS:
        return _parse_nexus_external_id(text)
    if key == PLATFORM_GITHUB:
        return _parse_github_external_id(text)
    if key == PLATFORM_STEAM:
        return _parse_steam_external_id(text)
    if key == PLATFORM_MODIO:
        return _parse_modio_external_id(text)
    return ""


def _parse_nexus_external_id(text: str) -> str:
    if text.isdigit():
        return text
    parts = [p for p in urlparse(text).path.split("/") if p]
    if "mods" in parts:
        try:
            idx = parts.index("mods")
            return parts[idx + 1].split("?")[0].strip()
        except (ValueError, IndexError):
            return ""
    # Path fragment like "/mods/336" without host
    if "/mods/" in text.replace("\\", "/"):
        tail = text.replace("\\", "/").split("/mods/", 1)[-1]
        return tail.split("/")[0].split("?")[0].strip()
    return ""


def _parse_github_external_id(text: str) -> str:
    raw = text.strip().rstrip("/")
    if "github.com" in raw.lower():
        parts = [p for p in urlparse(raw).path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return ""
    if raw.count("/") == 1 and not raw.startswith("http"):
        return raw
    return ""


def _parse_steam_external_id(text: str) -> str:
    if text.isdigit():
        return text
    low = text.lower()
    marker = "id="
    if marker in low:
        frag = text[low.index(marker) + len(marker) :]
        return "".join(ch for ch in frag if ch.isdigit()) or ""
    return ""


def _parse_modio_external_id(text: str) -> str:
    if text.isdigit():
        return text
    parts = [p for p in urlparse(text).path.split("/") if p]
    if "m" in parts:
        try:
            idx = parts.index("m")
            return parts[idx + 1].split("?")[0].strip()
        except (ValueError, IndexError):
            return ""
    return ""


# --- Back-compat aliases (previous Phase helpers) ---


def platform_pretty_name(platform: str | None) -> str:
    return format_platform_name(platform)


def platform_title_label(platform: str | None) -> str:
    return get_platform_metadata_labels(platform).name


def platform_id_label(platform: str | None) -> str:
    return get_platform_metadata_labels(platform).external_id


def resolve_external_id_for_display(
    *,
    platform: str | None,
    external_id: str = "",
    published_file_id: str = "",
    source_url: str = "",
) -> str:
    return format_external_id(
        platform,
        external_id,
        source_url=source_url,
        published_file_id=published_file_id,
    )


def format_mod_info_clipboard(
    *,
    name: str,
    platform: str | None,
    source_url: str = "",
    external_id: str = "",
    files: list[str] | None = None,
    deploy_status: str = "",  # kept for callers; omitted from Phase-6 format
) -> str:
    """Plain-text Mod summary for the system clipboard."""
    del deploy_status  # Phase-6 format does not include deploy status
    file_lines = [str(f).strip() for f in (files or []) if str(f).strip()]
    files_text = "\n".join(file_lines) if file_lines else "—"
    return "\n".join(
        [
            f"名称:\n{(name or '').strip() or '—'}",
            "",
            f"平台:\n{format_platform_name(platform)}",
            "",
            f"ID:\n{(external_id or '').strip() or '—'}",
            "",
            f"来源:\n{(source_url or '').strip() or '—'}",
            "",
            f"文件:\n{files_text}",
        ]
    )
