"""Library scroll range shrinks when switching to a smaller Mod set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME
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
    yield manager
    DatabaseManager.reset_instance()


def _seed_game(lib: Path, game: str, n: int, *, app_id: int) -> None:
    root = lib / game
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        folder = root / f"Mod{i:03d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        mid = str(app_id * 1000 + i)
        (info / "mod.json").write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Mod{i:03d}",
                    "app_id": app_id,
                    "game_name": game,
                }
            ),
            encoding="utf-8",
        )


def test_scroll_range_shrinks_when_switching_to_fewer_mods(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
) -> None:
    lib = tmp_path / "library"
    _seed_game(lib, "Palworld", 24, app_id=1623730)
    _seed_game(lib, "Anno 1800", 4, app_id=916440)

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
    assert len(view._cards) == 24
    assert many_host_h > 0

    view.game_list.setCurrentRow(anno_row)
    qapp.processEvents()
    view._sync_library_host_size()
    qapp.processEvents()

    few_max = view.scroll.verticalScrollBar().maximum()
    few_host_h = view.library_host.minimumHeight()
    assert len(view._cards) == 4
    assert few_host_h < many_host_h
    assert few_max <= many_max
    assert few_max < many_max or few_max == 0
