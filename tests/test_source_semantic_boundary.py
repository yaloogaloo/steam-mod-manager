"""Freeze platform vs provenance semantics across UI surfaces."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.models import ModMetadata
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.mod_library_cache import ModCardData
from services.platform_identity import (
    format_platform_label,
    resolve_display_platform,
)
from ui.library_query import (
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    PLATFORM_FILTER_LABELS,
    ModFilterIndex,
    collect_source_keys,
    effective_source_token,
    matches_platform_filter,
)
from ui.mod_card import ModCardWidget
from ui.mod_detail_panel import ModDetailPanel
from ui.platform_labels import format_platform_name, platform_badge_label


def _fake_resolved(*, platform: str, title: str = "M") -> SimpleNamespace:
    return SimpleNamespace(
        display_name=title,
        title=title,
        platform=platform,
        source_url="",
        workspace_id="",
        description="",
        folder_present=True,
        author="",
        dependencies=[],
    )


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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


def _card_data(*, platform: str, source_type: str) -> ModCardData:
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


def _ui_labels(*, db_platform: str, meta_platform: str = "", source_type: str = "") -> dict:
    resolved = resolve_display_platform(
        db_platform=db_platform,
        metadata_platform=meta_platform,
    )
    idx = _idx(platform=db_platform, source_type=source_type)
    return {
        "resolve": resolved,
        "badge": platform_badge_label(resolved),
        "label": format_platform_label(resolved),
        "detail_name": format_platform_name(resolved),
        "filter_token": effective_source_token(idx),
    }


def test_case1_nexus_platform_external_provenance_shows_nexus(
    qapp: QApplication, tmp_path: Path
) -> None:
    tokens = _ui_labels(db_platform=PLATFORM_NEXUS, source_type="external")
    assert tokens["resolve"] == PLATFORM_NEXUS
    assert tokens["badge"] == "Nexus"
    assert tokens["label"] == "Nexus"
    assert "Nexus" in tokens["detail_name"]
    assert tokens["filter_token"] == "nexus"
    assert matches_platform_filter(
        _idx(platform=PLATFORM_NEXUS, source_type="external"), FILTER_PLATFORM_NEXUS
    )

    folder = tmp_path / "G" / "N"
    folder.mkdir(parents=True)
    card = ModCardWidget(
        folder,
        ModMetadata(
            published_file_id="9001",
            title="N",
            managed_path=str(folder),
            source_type=PLATFORM_NEXUS,
        ),
        card_data=_card_data(platform=PLATFORM_NEXUS, source_type="external"),
    )
    card._render_platform_badge()
    assert card.platform_badge.text() == "Nexus"

    panel = ModDetailPanel()
    panel._deploy_busy = True
    panel._metadata = ModMetadata(
        published_file_id="9001",
        title="N",
        managed_path=str(folder),
        source_type=PLATFORM_NEXUS,
    )
    panel._resolved = _fake_resolved(platform=PLATFORM_NEXUS, title="N")
    panel._display_info = None
    panel._fill_view()
    assert panel.header_platform_badge.text() == "Nexus"
    assert "Nexus" in panel.meta_source_line.text()
    panel.close()
    panel.deleteLater()


def test_case2_steam_platform_backup_provenance_shows_steam(
    qapp: QApplication, tmp_path: Path
) -> None:
    tokens = _ui_labels(db_platform=PLATFORM_STEAM, source_type="backup")
    assert tokens["resolve"] == PLATFORM_STEAM
    assert tokens["badge"] == "Steam"
    assert tokens["filter_token"] == "steam"

    folder = tmp_path / "G" / "S"
    folder.mkdir(parents=True)
    card = ModCardWidget(
        folder,
        ModMetadata(published_file_id="9002", title="S", managed_path=str(folder)),
        card_data=_card_data(platform=PLATFORM_STEAM, source_type="backup"),
    )
    card._render_platform_badge()
    assert card.platform_badge.text() == "Steam"

    panel = ModDetailPanel()
    panel._deploy_busy = True
    panel._metadata = ModMetadata(
        published_file_id="9002", title="S", managed_path=str(folder)
    )
    panel._resolved = _fake_resolved(platform=PLATFORM_STEAM, title="S")
    panel._display_info = None
    panel._fill_view()
    assert panel.header_platform_badge.text() == "Steam"
    panel.close()
    panel.deleteLater()


def test_case3_empty_platform_metadata_nexus_not_external() -> None:
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform=PLATFORM_NEXUS,
        )
        == PLATFORM_NEXUS
    )
    assert format_platform_label(PLATFORM_NEXUS) == "Nexus"
    # provenance must not win when metadata provides store platform
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform=PLATFORM_NEXUS,
        )
        != "external"
    )


def test_case4_source_type_nexus_cannot_display_when_platform_empty() -> None:
    # Passing provenance-looking value only via sticky field on index
    idx = _idx(platform="", source_type="nexus")
    assert effective_source_token(idx) == "steam"
    assert not matches_platform_filter(idx, FILTER_PLATFORM_NEXUS)

    # Must not treat sticky as metadata_platform either when callers misuse it
    assert (
        resolve_display_platform(
            db_platform="",
            metadata_platform="",  # sticky intentionally omitted
        )
        == PLATFORM_STEAM
    )
    labels = _ui_labels(db_platform="", source_type="nexus")
    assert labels["badge"] != "Nexus"
    assert labels["filter_token"] != "nexus"


def test_case5_platform_filter_external_ignores_source_type() -> None:
    idx = _idx(platform=PLATFORM_STEAM, source_type="external")
    assert matches_platform_filter(idx, FILTER_PLATFORM_STEAM)
    assert not matches_platform_filter(idx, "platform_external")
    assert "platform_external" not in {key for key, _ in PLATFORM_FILTER_LABELS}
    assert "platform_external" not in collect_source_keys([idx])
    # Even if index.platform were wrongly "external", chip collection must not emit it
    assert "platform_external" not in collect_source_keys(
        [_idx(platform="external", source_type="external")]
    )


def test_source_badge_label_removed() -> None:
    import services.library_status as ls

    assert not hasattr(ls, "source_badge_label")
