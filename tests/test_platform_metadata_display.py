"""Platform-aware Mod identity labels in DetailPanel (compat suite)."""

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
    folder = _bind_folder(
        db,
        tmp_path,
        internal_id=str(info.mod_id),
        title="Cool Tool",
        external_id="owner/project",
        workspace_id=str(info.workspace_id or ""),
        platform=CORE_GITHUB,
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=info.mod_id)
    assert panel.view_id_caption.text().startswith("Workspace ID")
    assert info.workspace_id in panel.view_id.text()
    assert "9000" not in panel.view_id.text()
    assert panel.view_name_caption.text().startswith("名称")
