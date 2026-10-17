"""Flag tag chips: conflict / invalid toggle + reorder."""

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

from core.db_manager import CONFLICT_STATUS_CONFLICT, DatabaseManager
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


def test_flag_chips_toggle_and_reorder(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    pub = "94001"
    created = create_steam_test_mod(db, external_id=pub, title="FlagMod")
    internal_id = str(created.mod_id)
    folder = tmp_path / "Palworld" / "FlagMod"
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title="FlagMod",
        external_id=pub,
        workspace_id=str(created.workspace_id or pub),
        game_name="Palworld",
    )
    bind_managed_path(db, internal_id, folder, title="FlagMod")

    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder, mod_id=internal_id)
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

    st = db.get_mod_status(internal_id)
    assert st is not None
    assert st.invalid is True

    panel.btn_tag_conflict.setChecked(True)
    qapp.processEvents()
    assert panel._flag_tags_row.itemAt(0).widget() is panel.btn_tag_conflict
    st = db.get_mod_status(internal_id)
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT

    panel.btn_tag_conflict.setChecked(False)
    panel.btn_tag_invalid.setChecked(False)
    qapp.processEvents()
    st = db.get_mod_status(internal_id)
    assert st.invalid is False
    assert st.conflict_status != CONFLICT_STATUS_CONFLICT

    assert not panel.btn_tag_abandoned.isHidden()
    panel.btn_tag_abandoned.setChecked(True)
    qapp.processEvents()
    assert any(
        t.tag_type == TAG_TYPE_ABANDONED for t in db.get_mod_tags(internal_id)
    )
