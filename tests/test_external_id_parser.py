"""Phase 6 — parse external ids from platform URLs / path fragments."""

from __future__ import annotations

from core.db_manager import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from ui.platform_labels import format_external_id, parse_external_id_from_url


def test_nexus_mods_path_fragment() -> None:
    assert parse_external_id_from_url(PLATFORM_NEXUS, "/mods/336") == "336"
    assert (
        parse_external_id_from_url(
            PLATFORM_NEXUS, "https://www.nexusmods.com/palworld/mods/336"
        )
        == "336"
    )
    assert parse_external_id_from_url(PLATFORM_NEXUS, "336") == "336"


def test_github_owner_repo() -> None:
    assert (
        parse_external_id_from_url(
            PLATFORM_GITHUB, "https://github.com/owner/repository"
        )
        == "owner/repository"
    )
    assert parse_external_id_from_url(PLATFORM_GITHUB, "owner/repository") == (
        "owner/repository"
    )


def test_steam_workshop_id() -> None:
    assert (
        parse_external_id_from_url(
            PLATFORM_STEAM,
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546",
        )
        == "3761838546"
    )


def test_format_external_id_never_uses_internal_nexus_pk() -> None:
    assert (
        format_external_id(
            PLATFORM_NEXUS,
            "",
            source_url="https://www.nexusmods.com/x/mods/42",
            published_file_id="9000000000000042",
        )
        == "42"
    )
    assert (
        format_external_id(
            PLATFORM_STEAM,
            "",
            published_file_id="3761838546",
        )
        == "3761838546"
    )
