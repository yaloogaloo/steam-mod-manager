"""Batch edit source platform for multiple Mods."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_OTHER, PLATFORM_STEAM
from ui.edit_mod_dialog import EditModDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "batch_edit.db")
    manager.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    yield manager
    DatabaseManager.reset_instance()


def test_edit_dialog_batch_locks_non_source_fields(qapp: QApplication) -> None:
    dlg = EditModDialog(
        mod_ids=["1", "2", "3"],
        platform=PLATFORM_STEAM,
        game_name="Palworld",
        game_id=1623730,
        display_name="should clear",
        description="should clear",
        source_url="https://example.com",
    )
    assert dlg.is_batch_mode
    assert "批量编辑" in dlg.windowTitle()
    assert "3" in dlg.windowTitle()
    assert not dlg.display_name_edit.isEnabled()
    assert not dlg.description_edit.isEnabled()
    assert not dlg.source_url_edit.isEnabled()
    assert dlg.platform_combo.isEnabled()
    assert dlg.display_name_edit.text() == ""
    assert dlg.source_url_edit.text() == ""
    values = dlg.values()
    assert set(values.keys()) == {"platform"}
    assert "display_name" not in values
    assert "source_url" not in values
    dlg.close()


def test_batch_update_platform_only_touches_platform(db: DatabaseManager) -> None:
    for mid, plat, url, name in (
        (900001, PLATFORM_STEAM, "https://steam/a", "A"),
        (900002, PLATFORM_NEXUS, "https://nexus/b", "B"),
    ):
        db.update_mod_user_metadata(
            mid,
            {
                "display_name": name,
                "custom_description": f"desc-{name}",
                "user_notes": "note",
                "favorite": False,
                "platform": plat,
                "source_url": url,
            },
        )

    updated = db.batch_update_platform([900001, 900002], PLATFORM_OTHER)
    assert updated == 2

    a = db.get_mod_display_info(900001)
    b = db.get_mod_display_info(900002)
    assert a is not None and b is not None
    assert a.platform == PLATFORM_OTHER
    assert b.platform == PLATFORM_OTHER
    assert a.user_display_name == "A"
    assert b.user_display_name == "B"
    assert a.custom_description == "desc-A"
    assert b.custom_description == "desc-B"
    assert a.source_url == "https://steam/a"
    assert b.source_url == "https://nexus/b"


def test_single_edit_dialog_unchanged(qapp: QApplication) -> None:
    dlg = EditModDialog(
        mod_id="42",
        platform=PLATFORM_GITHUB,
        display_name="Solo",
        source_url="https://github.com/x/y",
        game_name="Palworld",
        game_id=1623730,
    )
    assert not dlg.is_batch_mode
    assert dlg.display_name_edit.isEnabled()
    assert dlg.source_url_edit.isEnabled()
    values = dlg.values()
    assert values["display_name"] == "Solo"
    assert values["source_url"] == "https://github.com/x/y"
    assert values["platform"] == PLATFORM_GITHUB
    assert "custom_deploy_path" in values
    assert values["custom_deploy_path"] == ""
    dlg.close()
