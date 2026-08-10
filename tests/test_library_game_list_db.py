"""Game list loads from SQLite — independent of workshop sync / on-disk mods."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from ui.library_view import ALL_GAMES_LABEL, GAME_ID_ROLE, GAME_ROLE, ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "game_list.db")
    yield manager
    DatabaseManager.reset_instance()


def test_db_game_without_workshop_appears_in_list(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    # Configured in DB only — no workshop_path, no mods on disk.
    db.update_game_deploy_config(
        480,
        name="NoWorkshopGame",
        workshop_path="",
        install_path="",
        mod_path="",
    )
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)

    lib = tmp_path / "mod"
    lib.mkdir()
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()

    keys = []
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        assert item is not None
        keys.append(item.data(GAME_ROLE) or "")
        widget = view.game_list.itemWidget(item)
        assert widget is not None
        if item.data(GAME_ROLE) == "NoWorkshopGame":
            assert widget.name_label.text() == "NoWorkshopGame"
            assert widget.count_label.text() == "0"
            assert int(item.data(GAME_ID_ROLE) or 0) == 480

    assert "NoWorkshopGame" in keys
    assert "" in keys  # 全部游戏


def test_select_no_workshop_game_gives_import_context(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    db.update_game_deploy_config(999001, name="NexusOnlyGame", workshop_path="")
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)

    lib = tmp_path / "mod"
    lib.mkdir()
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()

    target = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item is not None and item.data(GAME_ROLE) == "NexusOnlyGame":
            target = i
            break
    assert target is not None
    view.game_list.setCurrentRow(target)

    ctx = view.get_current_game_context()
    assert ctx is not None
    assert ctx["game_id"] == 999001
    assert ctx["game_name"] == "NexusOnlyGame"
    # Empty mod list is fine — no crash / still selected
    assert view.current_game_id == 999001
