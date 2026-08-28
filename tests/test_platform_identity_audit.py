"""Audit: UI display platform never comes from sticky mods.source_type."""

from __future__ import annotations

from services.platform_identity import (
    format_platform_label,
    resolve_display_platform,
)
from ui.library_query import (
    FILTER_PLATFORM_GITHUB,
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    ModFilterIndex,
    effective_source_token,
    matches_platform_filter,
)
from ui.platform_labels import format_platform_name, platform_badge_label


def _idx(*, platform: str, source_type: str = "") -> ModFilterIndex:
    return ModFilterIndex(
        mod_id="1",
        display_name="T",
        steam_name="T",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="T",
        platform=platform,
        source_type=source_type,
    )


def _all_ui_display_tokens(platform: str, source_type: str) -> dict[str, str]:
    """Simulate every UI surface that must agree on store platform."""
    resolved = resolve_display_platform(
        db_platform=platform,
        metadata_platform="",  # sticky must not be passed as metadata
    )
    idx = _idx(platform=platform, source_type=source_type)
    return {
        "resolve": resolved,
        "badge": platform_badge_label(resolved),
        "label": format_platform_label(resolved),
        "detail_name": format_platform_name(resolved),
        "filter_token": effective_source_token(idx),
    }


def test_case1_nexus_vs_external_sticky_all_surfaces() -> None:
    tokens = _all_ui_display_tokens("nexus", "external")
    assert tokens["resolve"] == "nexus"
    assert tokens["badge"] == "Nexus"
    assert tokens["label"] == "Nexus"
    assert "Nexus" in tokens["detail_name"]
    assert tokens["filter_token"] == "nexus"
    assert matches_platform_filter(
        _idx(platform="nexus", source_type="external"), FILTER_PLATFORM_NEXUS
    )
    assert not matches_platform_filter(
        _idx(platform="nexus", source_type="external"), FILTER_PLATFORM_STEAM
    )


def test_case2_steam_vs_external_sticky() -> None:
    tokens = _all_ui_display_tokens("steam", "external")
    assert tokens["resolve"] == "steam"
    assert tokens["badge"] == "Steam"
    assert tokens["filter_token"] == "steam"


def test_case3_sticky_github_cannot_override_nexus_platform() -> None:
    # Even if provenance/sticky claims github, display uses mods.platform
    tokens = _all_ui_display_tokens("nexus", "github")
    assert tokens["resolve"] == "nexus"
    assert tokens["badge"] == "Nexus"
    assert tokens["filter_token"] == "nexus"
    assert not matches_platform_filter(
        _idx(platform="nexus", source_type="github"), FILTER_PLATFORM_GITHUB
    )
    # Passing sticky as metadata_platform must still lose to db platform
    assert (
        resolve_display_platform(
            db_platform="nexus",
            metadata_platform="github",
        )
        == "nexus"
    )


def test_case4_empty_platform_metadata_ok_sticky_forbidden() -> None:
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform="nexus",
        )
        == "nexus"
    )
    # provenance-only must not become display platform
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform="external",
        )
        == "steam"
    )
    # source_type on index alone must not drive filter when platform empty-ish
    # (empty platform normalizes via resolve default; filter uses index.platform)
    idx = _idx(platform="", source_type="nexus")
    # empty platform → effective_source_token falls back to steam default, NOT sticky nexus
    assert effective_source_token(idx) == "steam"
