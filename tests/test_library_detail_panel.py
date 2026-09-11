"""Phase 2: library three-column selection → ModDetailPanel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.identity_service import create_mod_identity, identity_create_scope
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import MODE_EMPTY, MODE_VIEW

APP_ID = 1623730


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lib_panel.db")
    manager.upsert_game(
        GameInfo(app_id=APP_ID, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(
    db: DatabaseManager, root: Path, *, pub_id: str, title: str
) -> tuple[Path, str]:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(pub_id),
            workshop_id=str(pub_id),
            title=title,
            app_id=APP_ID,
            game_name="Palworld",
        )
    entity_id = str(created.mod_id)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "internal_id": entity_id,
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
                "workspace_id": pub_id,
                "external_id": pub_id,
            }
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        entity_id,
        last_known_path=str(folder),
        folder_present=True,
    )
    return folder, entity_id


def test_click_card_shows_detail_panel_no_dialog(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    path_a, id_a = _seed_mod(db, lib, pub_id="92001", title="Mod A")
    path_b, id_b = _seed_mod(db, lib, pub_id="92002", title="Mod B")

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()

    assert view.detail_panel is not None
    assert view.detail_panel._mode == MODE_EMPTY
    panel_id = id(view.detail_panel)

    assert len(view._cards) == 2
    assert not hasattr(view._cards[0], "detail_btn")
    assert not hasattr(view._cards[0], "edit_btn")

    # Selection authority is internal mod_id (not managed_path).
    view.on_mod_selected(id_a)
    assert view.detail_panel._mode == MODE_VIEW
    assert "Mod A" in view.detail_panel.view_title.text()
    assert view._selected_card is not None
    assert view._selected_card._selected is True

    view.on_mod_selected(id_b)
    assert id(view.detail_panel) == panel_id  # same panel instance
    assert "Mod B" in view.detail_panel.view_title.text()
    selected = [c for c in view._cards if c._selected]
    assert len(selected) == 1
    assert selected[0].managed_path.resolve() == path_b.resolve()
    assert path_a.is_dir()


def test_edit_save_updates_card_without_rescan(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    from PySide6.QtWidgets import QDialog

    from ui.edit_mod_dialog import EditModDialog

    lib = tmp_path / "library"
    folder, entity_id = _seed_mod(db, lib, pub_id="92003", title="Editable")

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    view.on_mod_selected(entity_id)

    panel = view.detail_panel

    def _accept(self: EditModDialog) -> int:
        self.display_name_edit.setText("NewNick")
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(EditModDialog, "exec", _accept)
    panel.enter_edit()

    assert panel._mode == MODE_VIEW
    assert "NewNick" in panel.view_title.text()
    card = view._card_for_mod_id(entity_id)
    assert card is not None
    assert "NewNick" in card.title_label.text()
    assert folder.name == "Editable"

    info = db.get_mod_display_info(entity_id)
    assert info is not None
    assert info.user_display_name == "NewNick"
