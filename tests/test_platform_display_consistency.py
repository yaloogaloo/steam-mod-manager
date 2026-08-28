"""Store platform (mods.platform) drives UI badges/filters — not sticky source_type."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from core.models import ModMetadata
from services.mod_library_cache import ModCardData
from services.platform_identity import (
    format_platform_label,
    resolve_display_platform,
)
from ui.library_query import (
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    ModFilterIndex,
    effective_source_token,
    matches_platform_filter,
)
from ui.mod_card import ModCardWidget
from ui.platform_labels import format_platform_name, platform_badge_label


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _index(*, platform: str, source_type: str = "") -> ModFilterIndex:
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


def _card_data(*, platform: str, source_type: str = "external") -> ModCardData:
    return ModCardData(
        id="9001",
        title="Mod",
        platform=platform,
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path="x",
        game_folder="G",
        source_type=source_type,
    )


def test_case1_nexus_platform_beats_external_sticky(
    qapp: QApplication, tmp_path: Path
) -> None:
    folder = tmp_path / "G" / "M"
    folder.mkdir(parents=True)
    data = _card_data(platform=PLATFORM_NEXUS, source_type="external")
    meta = ModMetadata(
        published_file_id="9001",
        title="M",
        managed_path=str(folder),
        source_type=PLATFORM_NEXUS,
    )
    card = ModCardWidget(folder, meta, card_data=data)
    card._render_platform_badge()
    assert card.platform_badge.text() == "Nexus"
    assert platform_badge_label(data.platform) == "Nexus"
    assert "Nexus" in format_platform_name(PLATFORM_NEXUS)

    idx = _index(platform=PLATFORM_NEXUS, source_type="external")
    assert effective_source_token(idx) == "nexus"
    assert matches_platform_filter(idx, FILTER_PLATFORM_NEXUS)
    assert not matches_platform_filter(idx, FILTER_PLATFORM_STEAM)


def test_case2_steam_with_external_sticky(qapp: QApplication, tmp_path: Path) -> None:
    folder = tmp_path / "G" / "S"
    folder.mkdir(parents=True)
    data = _card_data(platform=PLATFORM_STEAM, source_type="external")
    card = ModCardWidget(
        folder,
        ModMetadata(published_file_id="9002", title="S", managed_path=str(folder)),
        card_data=data,
    )
    card._render_platform_badge()
    assert card.platform_badge.text() == "Steam"
    idx = _index(platform=PLATFORM_STEAM, source_type="external")
    assert effective_source_token(idx) == "steam"


def test_case3_resolve_display_ignores_sticky_external() -> None:
    assert (
        resolve_display_platform(
            db_platform=PLATFORM_NEXUS,
            metadata_platform=PLATFORM_NEXUS,
        )
        == PLATFORM_NEXUS
    )
    assert (
        resolve_display_platform(
            db_platform=PLATFORM_NEXUS,
            metadata_platform="external",
        )
        == PLATFORM_NEXUS
    )
    assert format_platform_label(PLATFORM_NEXUS) == "Nexus"


def test_case4_empty_platform_falls_back_to_metadata_not_sticky() -> None:
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform=PLATFORM_NEXUS,
        )
        == PLATFORM_NEXUS
    )
    # sticky/provenance external must not become platform
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform="external",
        )
        == PLATFORM_STEAM
    )


def test_other_platforms_not_all_external() -> None:
    assert format_platform_label(PLATFORM_STEAM) == "Steam"
    assert format_platform_label(PLATFORM_NEXUS) == "Nexus"
    assert format_platform_label(PLATFORM_GITHUB) == "GitHub"
    assert format_platform_label("modio") == "mod.io"
    assert effective_source_token(_index(platform="github", source_type="external")) == (
        "github"
    )
