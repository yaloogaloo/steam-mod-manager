"""Phase 2: library three-column selection → ModDetailPanel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import MODE_EDIT, MODE_EMPTY, MODE_VIEW


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
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(root: Path, *, pub_id: str, title: str) -> Path:
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


def test_click_card_shows_detail_panel_no_dialog(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    a = _seed_mod(lib, pub_id="92001", title="Mod A")
    b = _seed_mod(lib, pub_id="92002", title="Mod B")
    db.upsert_mod(ModMetadata(published_file_id="92001", title="Mod A"))
    db.upsert_mod(ModMetadata(published_file_id="92002", title="Mod B"))

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()

    assert view.detail_panel is not None
    assert view.detail_panel._mode == MODE_EMPTY
    panel_id = id(view.detail_panel)

    assert len(view._cards) == 2
    assert not hasattr(view._cards[0], "detail_btn")
    assert not hasattr(view._cards[0], "edit_btn")

    view.on_mod_selected(a)
    assert view.detail_panel._mode == MODE_VIEW
    assert "Mod A" in view.detail_panel.view_title.text()
    assert view._selected_card is not None
    assert view._selected_card._selected is True

    view.on_mod_selected(b)
    assert id(view.detail_panel) == panel_id  # same panel instance
    assert "Mod B" in view.detail_panel.view_title.text()
    selected = [c for c in view._cards if c._selected]
    assert len(selected) == 1
    assert selected[0].managed_path.resolve() == b.resolve()


def test_edit_save_updates_card_without_rescan(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed_mod(lib, pub_id="92003", title="Editable")
    db.upsert_mod(ModMetadata(published_file_id="92003", title="Editable"))

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    view.on_mod_selected(folder)

    panel = view.detail_panel
    panel.enter_edit()
    assert panel._mode == MODE_EDIT
    panel.edit_display_name.setText("NewNick")
    panel._save_edit()

    assert panel._mode == MODE_VIEW
    assert "NewNick" in panel.view_title.text()
    card = view._card_for_path(folder)
    assert card is not None
    assert "NewNick" in card.title_label.text()

    info = db.get_mod_display_info(92003)
    assert info is not None
    assert info.user_display_name == "NewNick"
