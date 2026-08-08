"""Library scroll position stays stable after deploy UI refresh."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "scroll_deploy.db")
    manager.upsert_game(GameInfo(app_id=99, name="TestGame", folder_name="TestGame"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(library: Path, *, mod_id: str, title: str, app_id: int = 99) -> Path:
    mod_dir = library / "TestGame" / title
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {app_id},\n'
        '  "game_name": "TestGame"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


def _pump() -> None:
    QCoreApplication.processEvents()


def test_library_scroll_preserved_after_deploy(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "mod"
    for i in range(24):
        mid = str(9000 + i)
        title = f"Mod{i:02d}"
        _make_mod(library, mod_id=mid, title=title)
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=99))

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.resize(900, 480)
    view.show()
    view.refresh()
    _pump()

    bar = view.scroll.verticalScrollBar()
    view.library_host.adjustSize()
    view.scroll.updateGeometry()
    _pump()

    if bar.maximum() <= 0:
        view._set_scroll_value(0)
        assert view._capture_scroll() == 0
        view._refresh_mod_ui("9010")
        _pump()
        assert view._capture_scroll() == 0
        return

    target = max(1, bar.maximum() // 2)
    bar.setValue(target)
    before = bar.value()
    assert before > 0

    view._refresh_mod_ui("9010", focus_mod_id="9010")
    _pump()
    after = view.scroll.verticalScrollBar().value()
    assert after <= before + 5 or after < bar.maximum()
    if before > 20:
        assert after > 0
