"""Phase 6: Mod Library UX polish — empty / loading / context menu / panel singleton."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_view import (
    EMPTY_LIBRARY,
    EMPTY_SEARCH,
    ModLibraryView,
)
from ui.mod_card import ModCardWidget
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "ux_polish.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _mod(library: Path, mid: str = "7001", title: str = "UX Mod") -> Path:
    mod = library / "GameX" / title
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        '  "app_id": 42,\n'
        '  "game_name": "GameX"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod


def test_empty_library_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    assert view.empty_overlay.isVisible() or not view.empty_overlay.isHidden()
    assert view._empty_kind == EMPTY_LIBRARY
    assert "空" in view.empty_title.text()
    assert view.empty_action_btn.isVisible() or not view.empty_action_btn.isHidden()


def test_empty_search_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    path = _mod(library)
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="UX Mod", app_id=42))
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    assert path.exists()
    assert len(view._cards) == 1

    view.search_box.setText("zzz-no-such-mod")
    assert view._empty_kind == EMPTY_SEARCH
    assert not view.empty_overlay.isHidden()
    assert "符合条件" in view.empty_title.text()

    view.empty_action_btn.click()
    assert view.search_box.text() == ""
    assert view._empty_kind is None
    assert view.empty_overlay.isHidden()


def test_context_menu_actions_emit_signals(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    path = _mod(library)
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="UX Mod", app_id=42))
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)

    card = ModCardWidget(path)
    seen: dict[str, object] = {}
    card.edit_requested.connect(lambda p: seen.setdefault("edit", p))
    card.deploy_requested.connect(lambda m: seen.setdefault("deploy", m))
    card.open_folder_requested.connect(lambda p: seen.setdefault("folder", p))
    card.open_steam_requested.connect(lambda p: seen.setdefault("steam", p))
    card.favorite_toggle_requested.connect(lambda m: seen.setdefault("fav", m))
    card.selection_requested.connect(lambda p: seen.setdefault("detail", p))

    # Drive menu actions directly (avoid platform-dependent popup)
    card._emit_view_detail()
    card.edit_requested.emit(card.managed_path)
    card._emit_deploy()
    card.open_folder_requested.emit(card.managed_path)
    card.open_steam_requested.emit(card.managed_path)
    card._emit_favorite_toggle()

    assert seen["detail"] == path
    assert seen["edit"] == path
    assert seen["deploy"] == "7001"
    assert seen["folder"] == path
    assert seen["steam"] == path
    assert seen["fav"] == "7001"

    # contextMenuEvent builds a QMenu without crashing
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(10, 10)
    )
    # Patch exec to avoid blocking
    original_exec = QMenu.exec

    def fake_exec(self, *a, **k):  # noqa: ANN001
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    card.contextMenuEvent(event)
    monkeypatch.setattr(QMenu, "exec", original_exec)


def test_detail_panel_singleton_across_filter(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _mod(library)
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="UX Mod", app_id=42))
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    panel = view.detail_panel
    panel_id = id(panel)
    assert isinstance(panel, ModDetailPanel)

    view.search_box.setText("nope")
    view.search_box.clear()
    view.refresh()
    assert id(view.detail_panel) == panel_id


def test_ux_filter_no_network_or_archive(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _mod(library)
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="UX Mod", app_id=42))
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append("net")
        raise AssertionError("network/archive must not run")

    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.ensure_offline_page",
        boom,
        raising=False,
    )
    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.archive", boom, raising=False
    )

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    assert view._loading is False
    view.search_box.setText("UX")
    view.search_box.clear()
    assert calls == []


def test_empty_game_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from ui.library_view import EMPTY_GAME, GAME_ROLE

    library = tmp_path / "mod"
    _mod(library)
    (library / "EmptyGame").mkdir(parents=True)
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="7001", title="UX Mod", app_id=42))
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    target_row = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item is not None and item.data(GAME_ROLE) == "EmptyGame":
            target_row = i
            break
    assert target_row is not None
    view.game_list.setCurrentRow(target_row)
    assert view._empty_kind == EMPTY_GAME
    assert "还没有" in view.empty_title.text()
    assert not view.empty_overlay.isHidden()


def test_detail_panel_hierarchy_labels(qapp: QApplication) -> None:
    from PySide6.QtWidgets import QLabel

    panel = ModDetailPanel()
    texts = [lab.text() for lab in panel._view_page.findChildren(QLabel)]
    assert "概览" in texts
    assert "状态" in texts
    assert "操作" in texts


def test_loading_flag_clears_after_refresh(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    assert view.loading_overlay.isHidden()
    view.refresh()
    assert view._loading is False
    assert view.loading_overlay.isHidden()
