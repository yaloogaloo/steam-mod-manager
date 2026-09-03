"""Platform-aware Mod identity labels in DetailPanel (compat suite)."""

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
    platform_id_label,
    platform_title_label,
    resolve_external_id_for_display,
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
    manager = DatabaseManager.instance(tmp_path / "plat_meta.db")
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


def test_label_helpers() -> None:
    assert platform_id_label(PLATFORM_STEAM) == "Workspace ID"
    assert platform_id_label(PLATFORM_NEXUS) == "Workspace ID"
    assert platform_id_label(PLATFORM_GITHUB) == "Workspace ID"
    assert platform_title_label(PLATFORM_STEAM) == "名称"
    assert platform_title_label(PLATFORM_NEXUS) == "名称"
    assert platform_title_label(PLATFORM_GITHUB) == "名称"
    assert (
        resolve_external_id_for_display(
            platform=PLATFORM_NEXUS,
            external_id="336",
            published_file_id="9000000000000000",
        )
        == "336"
    )
    assert (
        resolve_external_id_for_display(
            platform=PLATFORM_STEAM,
            external_id="",
            published_file_id="3761838546",
        )
        == "3761838546"
    )


def test_steam_workshop_id_label(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _mod_folder(tmp_path, pub_id="3761838546", title="Steam Mod")
    db.upsert_mod(ModMetadata(published_file_id="3761838546", title="Steam Mod"))
    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel.view_id_caption.text().startswith("Workspace ID")
    assert "3761838546" in panel.view_id.text()
    assert panel.view_name_caption.text().startswith("名称")


def test_nexus_mod_id_not_internal_id(
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
    assert panel.view_id_caption.text().startswith("Workspace ID")
    assert "336" in panel.view_id.text()
    assert "9000" not in panel.view_id.text()
    assert panel.view_steam.text() == "Pal Analyzer"
    assert "来源" in panel.view_source_caption.text()
    assert "nexusmods.com/palworld/mods/336" in panel.view_source_url.text()


def test_github_repository_label(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/project",
        source_url="https://github.com/owner/project",
        title="Cool Tool",
            app_id=1623730,
        game_name="Palworld",
)
    folder = _mod_folder(tmp_path, pub_id=info.mod_id, title="Cool Tool")
    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel.view_id_caption.text().startswith("Workspace ID")
    assert info.workspace_id in panel.view_id.text()
    assert "9000" not in panel.view_id.text()
    assert panel.view_name_caption.text().startswith("名称")
