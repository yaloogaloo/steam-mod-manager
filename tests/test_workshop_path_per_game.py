"""Per-game workshop_path on deploy config + Sync Center game picker."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from ui.game_deploy_view import GameDeployView
from ui.sync_view import SyncCenterView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "workshop_path.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_workshop_path_column_and_api(db: DatabaseManager) -> None:
    cols = {
        str(r[1]) for r in db._conn.execute("PRAGMA table_info(games)").fetchall()
    }
    assert "workshop_path" in cols

    saved = db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=r"D:/game",
        mod_path=r"D:/game/Mods",
        workshop_path=r"D:/Steam/workshop/content/1623730",
    )
    assert saved.workshop_path.endswith("1623730")

    loaded = db.get_game_deploy_config(1623730)
    assert loaded is not None
    assert loaded.workshop_path == saved.workshop_path

    patched = db.update_game_deploy_config(1623730, mod_path=r"E:/mods")
    assert patched.workshop_path == saved.workshop_path
    assert patched.mod_path == r"E:/mods"


def test_game_deploy_view_saves_workshop_path(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    install = tmp_path / "game"
    mods = install / "Mods"
    workshop = tmp_path / "workshop" / "1623730"
    install.mkdir()
    mods.mkdir()
    workshop.mkdir(parents=True)

    view = GameDeployView(db=db)
    view.refresh()
    view.app_id_edit.setText("1623730")
    view.name_edit.setText("Palworld")
    view.install_edit.setText(str(install))
    view.mod_path_edit.setText(str(mods))
    view.workshop_edit.setText(str(workshop))
    view._on_save()

    cfg = db.get_game_deploy_config(1623730)
    assert cfg is not None
    assert cfg.workshop_path == str(workshop)


def test_sync_view_reads_game_workshop_and_skips_empty(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path
) -> None:
    workshop = tmp_path / "workshop" / "1623730"
    workshop.mkdir(parents=True)
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        workshop_path=str(workshop),
    )
    db.update_game_deploy_config(
        480,
        name="NoWorkshopGame",
        workshop_path="",
    )

    view = SyncCenterView(db=db)
    # Avoid Steam store network worker in unit tests.
    view._load_game_preview = lambda _app_id: None  # type: ignore[method-assign]
    view.set_paths("", str(tmp_path / "lib"))
    view.refresh_games()

    # Select Palworld
    idx = view.game_combo.findData(1623730)
    assert idx >= 0
    view.game_combo.setCurrentIndex(idx)
    assert view.workshop_path() == str(workshop)

    # Select game without workshop — start_sync must not error
    idx2 = view.game_combo.findData(480)
    assert idx2 >= 0
    view.game_combo.setCurrentIndex(idx2)
    assert view.workshop_path() == ""
    view.start_sync()
    assert "跳过" in view.status_label.text()


def test_sync_view_has_no_workshop_line_edit(qapp: QApplication, db: DatabaseManager) -> None:
    view = SyncCenterView(db=db)
    assert hasattr(view, "game_combo")
    assert not hasattr(view, "workshop_edit")
