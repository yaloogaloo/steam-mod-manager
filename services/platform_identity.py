"""Canonical Mod store-platform identity for UI display and filters.

``mods.platform`` is the only source of truth for badges / filters / labels.
``mods.source_type`` (import provenance) must not drive these surfaces.
"""

from __future__ import annotations

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    SUPPORTED_PLATFORMS,
    normalize_platform,
    normalize_platform_if_known,
)

__all__ = (
    "PLATFORM_GITHUB",
    "PLATFORM_MODIO",
    "PLATFORM_NEXUS",
    "PLATFORM_OTHER",
    "PLATFORM_STEAM",
    "SUPPORTED_PLATFORMS",
    "format_platform_label",
    "normalize_platform",
    "normalize_platform_if_known",
    "resolve_display_platform",
)


def format_platform_label(value: str | None) -> str:
    """Short UI badge / chip label for a store platform."""
    key = normalize_platform_if_known(value)
    if not key:
        raw = str(value or "").strip().lower()
        if not raw or raw in {"unknown", "external"}:
            return "Unknown"
        key = normalize_platform(value)
    return {
        PLATFORM_STEAM: "Steam",
        PLATFORM_NEXUS: "Nexus",
        PLATFORM_GITHUB: "GitHub",
        PLATFORM_MODIO: "mod.io",
        PLATFORM_OTHER: "其它",
    }.get(key, key.title() or "Unknown")


def resolve_display_platform(
    *,
    db_platform: str | None = None,
    metadata_platform: str | None = None,
    default: str = PLATFORM_STEAM,
) -> str:
    """
    Resolve the platform used for badges and filters.

    Order: ``mods.platform`` → metadata store platform → *default*.
    Never accepts sticky ``mods.source_type`` / provenance ``external``.
    """
    for raw in (db_platform, metadata_platform):
        known = normalize_platform_if_known(raw)
        if known:
            return known
        text = str(raw or "").strip().lower()
        # Reject provenance tokens explicitly
        if text in {"", "external", "unknown"}:
            continue
        if text in SUPPORTED_PLATFORMS or text in {
            "mod.io",
            "mod_io",
            "mod-io",
            "其它",
            "其他",
            "other",
            "local",
            "manual",
        }:
            return normalize_platform(text)
    return normalize_platform(default)
