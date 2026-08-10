"""Smoke tests for ModDetailPanel (Phase 1 — UI only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from ui.edit_mod_dialog import EditModDialog
from ui.mod_detail_panel import MODE_EMPTY, MODE_VIEW, ModDetailPanel


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


def test_panel_created_once_and_reused(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    panel = ModDetailPanel()
    assert panel._mode == MODE_EMPTY

    pub = "91001"
    folder = _mod_folder(tmp_path, pub_id=pub, title="Test Mod")
    db.upsert_mod(ModMetadata(published_file_id=pub, title="Test Mod"))

    panel.show_mod(folder)
    assert panel._mode == MODE_VIEW
    assert "Test Mod" in panel.view_title.text()
    assert hasattr(panel, "btn_edit_info")

    def _accept_edit(self: EditModDialog) -> int:
        self.display_name_edit.setText("Nick")
        self.description_edit.setPlainText("note-desc")
        self.source_url_edit.setText("https://www.nexusmods.com/x/mods/1")
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(EditModDialog, "exec", _accept_edit)
    folder_before = folder.resolve()
    panel.enter_edit()
    assert panel._mode == MODE_VIEW
    assert "Nick" in panel.view_title.text()
    assert folder_before == folder.resolve()
    assert folder.is_dir()

    info = db.get_mod_display_info(pub)
    assert info is not None
    assert info.user_display_name == "Nick"
    assert info.custom_description == "note-desc"
    assert info.source_url == "https://www.nexusmods.com/x/mods/1"

    panel.clear()
    assert panel._mode == MODE_EMPTY


def test_edit_does_not_rename_folder(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    pub = "91002"
    folder = _mod_folder(tmp_path, pub_id=pub, title="FolderStay")
    db.upsert_mod(ModMetadata(published_file_id=pub, title="FolderStay"))
    panel = ModDetailPanel()
    panel.show_mod(folder)

    def _accept(self: EditModDialog) -> int:
        self.display_name_edit.setText("Brand New Name")
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(EditModDialog, "exec", _accept)
    before = folder.resolve()
    panel.open_edit_info_dialog()
    assert before.exists()
    assert before.name == "FolderStay"
    assert "Brand New Name" in panel.view_title.text()
