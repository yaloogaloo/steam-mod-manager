"""Responsive Mod-list toolbar + detail header actions under narrow widths."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QRect
from PySide6.QtWidgets import QApplication, QPushButton

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import ModDetailPanel
from ui.styles import APP_STYLE


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "responsive.db")
    manager.upsert_game(GameInfo(app_id=99, name="TestGame", folder_name="TestGame"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _pump() -> None:
    QCoreApplication.processEvents()


def _seed(library: Path, *, mod_id: str, title: str) -> Path:
    mod_dir = library / "TestGame" / title
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "{title}",\n'
        '  "app_id": 99,\n'
        '  "game_name": "TestGame"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


def _assert_no_overlap(widgets: list[QPushButton]) -> None:
    visible = [w for w in widgets if w.isVisible()]
    for i, a in enumerate(visible):
        ra = a.geometry()
        assert ra.width() > 0 and ra.height() > 0
        for b in visible[i + 1 :]:
            rb = b.geometry()
            assert not ra.intersects(rb), (
                f"overlap: {a.text()!r}{ra.getRect()} vs {b.text()!r}{rb.getRect()}"
            )


@pytest.mark.parametrize("size", [(1280, 720), (1024, 768), (900, 600)])
def test_filter_toolbar_wraps_without_overlap(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    library = tmp_path / "mod"
    _seed(library, mod_id="8001", title="A")
    db.upsert_mod(ModMetadata(published_file_id="8001", title="A", app_id=99))
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.resize(*size)
    view.show()
    view.refresh()
    _pump()

    # Squeeze the center column via splitter (game | center | detail)
    total = max(400, size[0] - 40)
    view.splitter.setSizes([160, 180, max(220, total - 340)])
    _pump()
    view._status_bar.adjustSize()
    view._platform_bar.adjustSize()
    view._meta_bar.adjustSize()
    _pump()

    status_chips = [
        b
        for b in view._filter_buttons.values()
        if b.parentWidget() is view._status_bar and b.isVisible()
    ]
    platform_chips = list(view._platform_buttons.values())
    assert status_chips
    assert platform_chips
    _assert_no_overlap(status_chips)
    _assert_no_overlap(platform_chips)

    for btn in status_chips + platform_chips:
        # Fixed chips keep readable text — no empty / ellipsis truncation.
        assert btn.text().strip()
        assert "…" not in btn.text()
        assert btn.size().width() >= btn.minimumSizeHint().width()


def test_detail_actions_visible_with_long_title_narrow_panel(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_name = "Very_Long_Mod_Name_Test_Test_Test_Test"
    library = tmp_path / "mod"
    folder = _seed(library, mod_id="8002", title=long_name)
    db.upsert_mod(
        ModMetadata(published_file_id="8002", title=long_name, app_id=99)
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)

    panel = ModDetailPanel()
    # Two-row footer needs ~panel min width; 260px was for an older FlowLayout wrap.
    panel.resize(400, 720)
    panel.show()
    panel.show_mod(folder)
    _pump()
    panel.resize(360, 720)
    _pump()
    panel._elide_header_title()
    _pump()

    assert panel.view_title.toolTip() == long_name
    row1 = (
        panel.btn_folder,
        panel.btn_steam,
        panel.btn_offline,
        panel.btn_download_offline,
    )
    row2 = (
        panel.btn_deploy,
        panel.btn_redeploy,
        panel.btn_undeploy,
        panel.btn_edit_info,
    )
    for btn in (*row1, *row2):
        assert btn.isVisible()
        top_left = btn.mapTo(panel, btn.rect().topLeft())
        assert top_left.x() >= 0
        assert top_left.x() + btn.width() <= panel.width() + 2
    row1_ys = [b.mapTo(panel, b.rect().topLeft()).y() for b in row1]
    row2_ys = [b.mapTo(panel, b.rect().topLeft()).y() for b in row2]
    assert max(row1_ys) - min(row1_ys) <= 2
    assert max(row2_ys) - min(row2_ys) <= 2
    assert min(row2_ys) > max(row1_ys)
    assert panel.btn_remove_mod.isHidden()


def test_library_splitter_keeps_detail_actions(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_name = "Very_Long_Mod_Name_Test_Test_Test_Test"
    library = tmp_path / "mod"
    folder = _seed(library, mod_id="8003", title=long_name)
    db.upsert_mod(
        ModMetadata(published_file_id="8003", title=long_name, app_id=99)
    )
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.resize(1024, 768)
    view.show()
    view.refresh()
    _pump()

    view.splitter.setSizes([160, 520, 260])
    _pump()
    card = view._card_for_path(folder)
    assert card is not None
    view._select_card(card, show_panel=True)
    _pump()

    panel = view.detail_panel
    assert panel.btn_folder.isVisible()
    assert panel.btn_folder.text() == "打开目录"
    mapped = panel.btn_folder.mapTo(panel, panel.btn_folder.rect().topLeft())
    assert mapped.x() + panel.btn_folder.width() <= panel.width() + 2
    assert "名称：" in panel.meta_name_line.text()
    assert panel.btn_tag_conflict.isVisible()
    assert panel.btn_tag_invalid.isVisible()
