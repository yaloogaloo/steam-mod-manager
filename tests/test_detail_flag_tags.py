"""Flag tag chips: conflict / invalid toggle + reorder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import CONFLICT_STATUS_CONFLICT, DatabaseManager
from core.models import ModMetadata
from ui.mod_detail_panel import TAG_TYPE_ABANDONED, ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "flags.db")
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


def test_flag_chips_toggle_and_reorder(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    pub = "94001"
    folder = _mod_folder(tmp_path, pub_id=pub, title="FlagMod")
    db.upsert_mod(ModMetadata(published_file_id=pub, title="FlagMod"))

    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()

    assert not panel.btn_tag_conflict.isHidden()
    assert not panel.btn_tag_invalid.isHidden()
    assert not panel.btn_tag_conflict.isChecked()

    # Default order: 冲突 then 失效
    assert panel._flag_tags_row.itemAt(0).widget() is panel.btn_tag_conflict

    panel.btn_tag_invalid.setChecked(True)
    qapp.processEvents()
    assert panel.btn_tag_invalid.isChecked()
    # Active chip moves to front
    assert panel._flag_tags_row.itemAt(0).widget() is panel.btn_tag_invalid

    st = db.get_mod_status(pub)
    assert st is not None
    assert st.invalid is True

    panel.btn_tag_conflict.setChecked(True)
    qapp.processEvents()
    assert panel._flag_tags_row.itemAt(0).widget() is panel.btn_tag_conflict
    st = db.get_mod_status(pub)
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT

    panel.btn_tag_conflict.setChecked(False)
    panel.btn_tag_invalid.setChecked(False)
    qapp.processEvents()
    st = db.get_mod_status(pub)
    assert st.invalid is False
    assert st.conflict_status != CONFLICT_STATUS_CONFLICT

    assert not panel.btn_tag_abandoned.isHidden()
    panel.btn_tag_abandoned.setChecked(True)
    qapp.processEvents()
    assert any(
        t.tag_type == TAG_TYPE_ABANDONED for t in db.get_mod_tags(pub)
    )
    panel.btn_tag_abandoned.setChecked(False)
    qapp.processEvents()
    assert not any(
        t.tag_type == TAG_TYPE_ABANDONED for t in db.get_mod_tags(pub)
    )
