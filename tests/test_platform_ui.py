"""Platform badge + library filter for Steam/Nexus/GitHub."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    DatabaseManager,
)
from core.models import ModMetadata
from ui.library_query import (
    FILTER_PLATFORM_GITHUB,
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    ModFilterIndex,
    matches_search,
    matches_status_filter,
)
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "platform_ui.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _idx(**kwargs) -> ModFilterIndex:
    base = dict(
        mod_id="1",
        display_name="Alpha",
        steam_name="Steam Alpha",
        notes="",
        game_name="Game",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="Alpha",
        platform=PLATFORM_STEAM,
        source_url="",
        external_id="",
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def test_platform_filters_and_search() -> None:
    steam = _idx(platform=PLATFORM_STEAM, external_id="111", source_url="https://steam...")
    nexus = _idx(
        platform=PLATFORM_NEXUS,
        external_id="999",
        source_url="https://www.nexusmods.com/x/mods/999",
        display_name="Character",
    )
    github = _idx(
        platform=PLATFORM_GITHUB,
        external_id="user/project",
        source_url="https://github.com/user/project",
    )

    assert matches_status_filter(steam, FILTER_PLATFORM_STEAM)
    assert not matches_status_filter(nexus, FILTER_PLATFORM_STEAM)
    assert matches_status_filter(nexus, FILTER_PLATFORM_NEXUS)
    assert matches_status_filter(github, FILTER_PLATFORM_GITHUB)

    assert matches_search(nexus, "nexusmods")
    assert matches_search(github, "user/project")
    assert matches_search(steam, "111")


def test_platform_badge_on_card(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    mod = tmp_path / "Game" / "NexusMod"
    mod.mkdir(parents=True)
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="42",
        source_url="https://www.nexusmods.com/x/mods/42",
        title="Nexus Mod",
            app_id=1623730,
        game_name="Palworld",
)
    card = ModCardWidget(
        mod,
        ModMetadata(
            published_file_id=info.mod_id,
            title="Nexus Mod",
            managed_path=str(mod),
        ),
    )
    assert not card.platform_badge.isHidden()
    assert "Nexus" in card.platform_badge.text()

    plain = tmp_path / "Game" / "Plain"
    plain.mkdir(parents=True)
    card_b = ModCardWidget(plain)
    assert card.height() == card_b.height()
