"""Phase 6 — platform metadata labels on DetailPanel."""

from __future__ import annotations

import json
from pathlib import Path

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
from ui.mod_detail_panel import ModDetailPanel
from ui.platform_labels import (
    format_external_id,
    format_platform_name,
    get_platform_metadata_labels,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "meta_ui.db")
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(root: Path, *, pub_id: str, title: str) -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_get_platform_metadata_labels() -> None:
    steam = get_platform_metadata_labels(PLATFORM_STEAM)
    assert steam.name == "名称"
    assert steam.external_id == "Workshop ID"
    assert steam.badge == "Steam"
    assert format_platform_name(PLATFORM_NEXUS) == "Nexus Mods"
    nexus = get_platform_metadata_labels(PLATFORM_NEXUS)
    assert nexus.external_id == "Nexus Mod ID"
    github = get_platform_metadata_labels(PLATFORM_GITHUB)
    assert github.external_id == "GitHub Repository"
    assert github.badge == "GitHub"


def test_steam_workshop_id_ui(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _mod_folder(tmp_path, pub_id="3761838546", title="Steam Mod")
    db.upsert_mod(ModMetadata(published_file_id="3761838546", title="Steam Mod"))
    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel.view_id_caption.text().startswith("Workshop ID")
    assert panel.view_id.text() == "3761838546"
    assert panel.view_name_caption.text().startswith("名称")
    assert "Steam Workshop" in panel.view_platform.text()


def test_nexus_mod_id_from_external_not_internal(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Pal Analyzer",
            app_id=1623730,
        game_name="Palworld",
)
    folder = _mod_folder(tmp_path, pub_id=info.mod_id, title="Pal Analyzer")
    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel.view_id_caption.text().startswith("Nexus Mod ID")
    assert panel.view_id.text() == "336"
    assert "9000" not in panel.view_id.text()
    assert panel.view_steam.text() == "Pal Analyzer"
    assert "Nexus Mods" in panel.view_platform.text()
    assert "来源" in panel.view_source_caption.text()


def test_github_repository_ui(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="Cool Tool",
            app_id=1623730,
        game_name="Palworld",
)
    folder = _mod_folder(tmp_path, pub_id=info.mod_id, title="Cool Tool")
    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel.view_id_caption.text().startswith("GitHub Repository")
    assert panel.view_id.text() == "owner/repo"


def test_format_external_id_parses_nexus_url_when_missing() -> None:
    assert (
        format_external_id(
            PLATFORM_NEXUS,
            "",
            source_url="https://www.nexusmods.com/palworld/mods/336",
            published_file_id="9000000000000000",
        )
        == "336"
    )
