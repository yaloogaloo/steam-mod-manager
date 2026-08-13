"""Header size badge mirrors platform badge; size removed from rich metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "size_badge.db")
    manager.upsert_game(GameInfo(app_id=1, name="Game", folder_name="Game"))
    yield manager
    DatabaseManager.reset_instance()


def test_size_badge_next_to_platform_and_not_in_rich_html(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = tmp_path / "Game" / "SizedMod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        '{"published_file_id":"88","title":"SizedMod","app_id":1}',
        encoding="utf-8",
    )
    (folder / "payload.pak").write_bytes(b"x" * 4096)
    db.upsert_mod(ModMetadata(published_file_id="88", title="SizedMod", app_id=1))

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    assert hasattr(panel, "size_badge")
    assert not panel.size_badge.isHidden()
    assert "KB" in panel.size_badge.text() or "B" in panel.size_badge.text()
    assert panel.size_badge.objectName() == panel.header_platform_badge.objectName()
    assert "background-color" in panel.size_badge.styleSheet()
    assert panel.size_badge.styleSheet() == panel.header_platform_badge.styleSheet()
    assert "大小" not in (panel.meta_rich_label.text() or "")
