"""Library scroll range shrinks when switching to a smaller Mod set."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from tests.helpers.identity import patch_library_get_db, seed_steam_managed_mod
from ui.library_view import GAME_ROLE, ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "scroll_shrink.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    manager.upsert_game(
        GameInfo(app_id=916440, name="Anno 1800", folder_name="Anno 1800")
    )
    yield manager
    DatabaseManager.reset_instance()


def _seed_game(
    lib: Path, db: DatabaseManager, game: str, n: int, *, app_id: int
) -> None:
    for i in range(n):
        mid = str(app_id * 1000 + i)
        seed_steam_managed_mod(
            db,
            lib,
            external_id=mid,
            title=f"Mod{i:03d}",
            game_folder=game,
            app_id=app_id,
            game_name=game,
            files={"a.txt": "x"},
        )


def test_scroll_range_shrinks_when_switching_to_fewer_mods(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch,
) -> None:
    lib = tmp_path / "library"
    _seed_game(lib, db, "Palworld", 24, app_id=1623730)
    _seed_game(lib, db, "Anno 1800", 4, app_id=916440)
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.resize(900, 500)
    view.show()
    qapp.processEvents()
    view.refresh()
    qapp.processEvents()

    pal_row = anno_row = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        key = item.data(GAME_ROLE) if item else ""
        if key == "Palworld":
            pal_row = i
        elif key == "Anno 1800":
            anno_row = i
    assert pal_row is not None and anno_row is not None

    view.game_list.setCurrentRow(pal_row)
    qapp.processEvents()
    view._sync_library_host_size()
    qapp.processEvents()

    many_max = view.scroll.verticalScrollBar().maximum()
    many_host_h = view.library_host.minimumHeight()
    assert len(view._filtered_row_entries) == 24
    assert many_host_h > 0

    view.game_list.setCurrentRow(anno_row)
    qapp.processEvents()
    view._sync_library_host_size()
    qapp.processEvents()

    few_max = view.scroll.verticalScrollBar().maximum()
    few_host_h = view.library_host.minimumHeight()
    assert len(view._filtered_row_entries) == 4
    assert few_host_h < many_host_h
    assert few_max <= many_max
    assert few_max < many_max or few_max == 0
