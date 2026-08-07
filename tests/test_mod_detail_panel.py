"""Smoke tests for ModDetailPanel (Phase 1 — UI only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from ui.mod_detail_panel import MODE_EDIT, MODE_EMPTY, MODE_VIEW, ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "panel.db")
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
                "description": "Steam desc",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_panel_created_once_and_reused(qapp: QApplication, tmp_path: Path, db: DatabaseManager) -> None:
    panel = ModDetailPanel()
    assert panel._mode == MODE_EMPTY

    pub = "91001"
    folder = _mod_folder(tmp_path, pub_id=pub, title="Test Mod")
    db.upsert_mod(ModMetadata(published_file_id=pub, title="Test Mod"))

    panel.show_mod(folder)
    assert panel._mode == MODE_VIEW
    assert "Test Mod" in panel.view_title.text()

    panel.enter_edit()
    assert panel._mode == MODE_EDIT

    panel.edit_display_name.setText("Nick")
    panel.edit_notes.setPlainText("note")
    panel._save_edit()
    assert panel._mode == MODE_VIEW
    assert "Nick" in panel.view_title.text()

    info = db.get_mod_display_info(pub)
    assert info is not None
    assert info.user_display_name == "Nick"
    assert info.user_notes == "note"

    panel.clear()
    assert panel._mode == MODE_EMPTY


def test_offline_missing_label_without_reading_html(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    panel = ModDetailPanel()
    pub = "91002"
    folder = _mod_folder(tmp_path, pub_id=pub, title="No Offline")
    db.upsert_mod(ModMetadata(published_file_id=pub, title="No Offline"))
    panel.show_mod(folder)
    assert "未保存" in panel.view_offline.text()
