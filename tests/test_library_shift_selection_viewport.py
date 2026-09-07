"""Shift multi-select must use filtered mod_id range under viewport virtualization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.mod_library_cache import reset_library_cache
from ui.library_view import ModLibraryView
from ui.library_viewport import estimate_total_height
from ui.mod_card import CARD_WIDTH


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "shift_viewport.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()


def _seed(lib: Path, db: DatabaseManager, *, n: int = 24, app_id: int = 880) -> list[str]:
    game = "ShiftGame"
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    ids: list[str] = []
    for i in range(n):
        mid = str(app_id * 1000 + i)
        ids.append(mid)
        folder = lib / game / f"Mod{i:03d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Mod{i:03d}",
                    "game_name": game,
                    "app_id": app_id,
                }
            ),
            encoding="utf-8",
        )
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=f"Mod{i:03d}",
                app_id=app_id,
                game_name=game,
                managed_path=str(folder),
            )
        )
        db.update_mod_identity_fields(
            mid,
            folder_present=True,
            last_known_path=str(folder),
            app_id=app_id,
        )
    return ids


def _open_game(view: ModLibraryView, lib: Path, qapp: QApplication) -> None:
    view.resize(520, 420)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "ShiftGame"
    view.current_game_id = 880
    view.current_game_name = "ShiftGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=False)
    qapp.processEvents()
    # Narrow viewport so only a small window binds (≈ first ~8 cards).
    view.resize(520, 420)
    qapp.processEvents()
    view._sync_viewport_cards(scroll_y=0)
    qapp.processEvents()


def test_shift_range_spans_beyond_viewport_window(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    lib = tmp_path / "mod"
    ids = _seed(lib, db, n=24)
    del ids  # order follows library sort, not seed insertion
    view = ModLibraryView()
    _open_game(view, lib, qapp)

    filtered = view._filtered_mod_ids()
    assert len(filtered) == 24
    assert len(view._cards) < 20
    assert len(view._cards) <= 16

    view.on_mod_selected(filtered[0])
    assert view._selected_mod_ids == [filtered[0]]
    assert view._selection_anchor_mod_id == filtered[0]

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    # Target may be outside the bound window — Shift uses filtered indices.
    view.on_mod_selected(filtered[19])

    assert view._selected_mod_ids == filtered[0:20]
    assert len(view._selected_mod_ids) == 20
    assert view._selection_anchor_mod_id == filtered[0]
    view.close()


def test_selection_survives_viewport_scroll_rebind(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    lib = tmp_path / "mod"
    _seed(lib, db, n=24)
    view = ModLibraryView()
    _open_game(view, lib, qapp)
    filtered = view._filtered_mod_ids()

    view.on_mod_selected(filtered[0])
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(filtered[19])
    selected_before = list(view._selected_mod_ids)
    assert len(selected_before) == 20

    # Scroll far enough that early selected mods leave the window.
    total_h = estimate_total_height(len(filtered), max(CARD_WIDTH * 2, 400))
    view._sync_viewport_cards(scroll_y=max(400, total_h // 2))
    qapp.processEvents()

    assert view._selected_mod_ids == selected_before
    bound_ids = {c._mod_id() for c in view._cards}
    # At least one originally-selected id should be off-window after scroll.
    assert not set(selected_before).issubset(bound_ids)
    for card in view._cards:
        mid = card._mod_id()
        if mid in selected_before:
            assert card._selected is True
        else:
            assert card._selected is False
    view.close()


def test_ctrl_toggle_unchanged_under_viewport(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    lib = tmp_path / "mod"
    _seed(lib, db, n=24)
    view = ModLibraryView()
    _open_game(view, lib, qapp)
    filtered = view._filtered_mod_ids()
    visible_ids = [c._mod_id() for c in view._visible_cards()]
    assert len(visible_ids) >= 2

    view.on_mod_selected(visible_ids[0])
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    view.on_mod_selected(visible_ids[1])
    assert set(view._selected_mod_ids) == {visible_ids[0], visible_ids[1]}

    view.on_mod_selected(visible_ids[0])
    assert view._selected_mod_ids == [visible_ids[1]]
    view.close()
