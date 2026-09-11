"""Phase 6 — platform metadata labels on DetailPanel."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    DatabaseManager,
)
from core.mod_platform import PLATFORM_GITHUB as CORE_GITHUB
from core.mod_platform import PLATFORM_NEXUS as CORE_NEXUS
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


def _bind_folder(
    db: DatabaseManager,
    root: Path,
    *,
    internal_id: str,
    title: str,
    external_id: str = "",
    workspace_id: str = "",
    platform: str = PLATFORM_STEAM,
) -> Path:
    folder = root / "Palworld" / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=str(internal_id),
        title=title,
        external_id=str(external_id or ""),
        workspace_id=str(workspace_id or external_id or ""),
        app_id=1623730,
        game_name="Palworld",
        platform=platform,
    )
    bind_managed_path(db, internal_id, folder, game_name="Palworld", title=title)
    return folder


def test_get_platform_metadata_labels() -> None:
    steam = get_platform_metadata_labels(PLATFORM_STEAM)
    assert steam.name == "名称"
    assert steam.external_id == "Workspace ID"
    assert steam.badge == "Steam"
    assert format_platform_name(PLATFORM_NEXUS) == "Nexus Mods"
    nexus = get_platform_metadata_labels(PLATFORM_NEXUS)
    assert nexus.external_id == "Workspace ID"
    github = get_platform_metadata_labels(PLATFORM_GITHUB)
    assert github.external_id == "Workspace ID"
    assert github.badge == "GitHub"


def test_steam_workshop_id_ui(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    created = create_steam_test_mod(db, external_id="3761838546", title="Steam Mod")
    folder = _bind_folder(
        db,
        tmp_path,
        internal_id=str(created.mod_id),
        title="Steam Mod",
        external_id="3761838546",
        workspace_id=str(created.workspace_id or "3761838546"),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=created.mod_id)
    assert panel.view_id_caption.text().startswith("Workspace ID")
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
    folder = _bind_folder(
        db,
        tmp_path,
        internal_id=str(info.mod_id),
        title="Pal Analyzer",
        external_id="336",
        workspace_id=str(info.workspace_id or "336"),
        platform=CORE_NEXUS,
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=info.mod_id)
    assert panel.view_id_caption.text().startswith("Workspace ID")
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
    folder = _bind_folder(
        db,
        tmp_path,
        internal_id=str(info.mod_id),
        title="Cool Tool",
        external_id="owner/repo",
        workspace_id=str(info.workspace_id or ""),
        platform=CORE_GITHUB,
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=info.mod_id)
    assert panel.view_id_caption.text().startswith("Workspace ID")
    assert panel.view_id.text() == info.workspace_id
    assert "9000" not in panel.view_id.text()
    assert "owner/repo" not in panel.view_id.text()


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
