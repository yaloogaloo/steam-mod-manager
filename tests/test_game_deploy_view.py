"""Phase 2: game deploy settings UI — path checks + save via DB API."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from ui.game_deploy_view import GameDeployView, validate_deploy_paths
from ui.main_window import PAGE_DEPLOY, PAGE_LIBRARY, PAGE_SYNC, MainWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_ui.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_validate_deploy_paths_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    errors = validate_deploy_paths(str(missing), str(missing / "mods"))
    assert any("游戏目录不存在" in e for e in errors)
    assert any("Mod 部署目录不存在" in e for e in errors)


def test_validate_deploy_paths_ok(tmp_path: Path) -> None:
    install = tmp_path / "game"
    mods = install / "Mods"
    install.mkdir()
    mods.mkdir()
    assert validate_deploy_paths(str(install), str(mods)) == []


def test_validate_empty_fields() -> None:
    errors = validate_deploy_paths("", "")
    assert any("未填写" in e for e in errors)


def test_save_config_via_view(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    install = tmp_path / "Palworld"
    mods = install / "Mods"
    install.mkdir()
    mods.mkdir()

    view = GameDeployView(db=db)
    view.refresh()
    view.app_id_edit.setText("1623730")
    view.name_edit.setText("Palworld")
    view.install_edit.setText(str(install))
    view.mod_path_edit.setText(str(mods))
    view._on_save()

    cfg = db.get_game_deploy_config(1623730)
    assert cfg is not None
    assert cfg.name == "Palworld"
    assert cfg.install_path == str(install)
    assert cfg.mod_path == str(mods)
    assert cfg.deploy_type == DEPLOY_TYPE_FOLDER_COPY

    # Reload into form
    view.refresh()
    assert view.game_combo.findData(1623730) >= 0
    view._load_game(1623730)
    assert view.name_edit.text() == "Palworld"
    assert view.install_edit.text() == str(install)


def test_main_window_has_deploy_page(qapp: QApplication, monkeypatch, tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager(tmp_path / "main_deploy.db")
    monkeypatch.setattr("core.db_manager.DatabaseManager.instance", classmethod(lambda cls, db_path=None: db))
    monkeypatch.setattr("ui.game_deploy_view.get_db", lambda: db)

    win = MainWindow()
    assert win.nav_list.count() == 3
    assert win.nav_list.item(PAGE_DEPLOY).text() == "游戏部署"
    assert win.stack.count() == 3
    assert win.stack.widget(PAGE_SYNC) is win.sync_view
    assert win.stack.widget(PAGE_LIBRARY) is win.library_view
    assert win.stack.widget(PAGE_DEPLOY) is win.deploy_view

    win.nav_list.setCurrentRow(PAGE_DEPLOY)
    assert win.stack.currentIndex() == PAGE_DEPLOY
    win.close()
    db.close()
    DatabaseManager.reset_instance()
