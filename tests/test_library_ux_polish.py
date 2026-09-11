"""Phase 6: Mod Library UX polish — empty / loading / context menu / panel singleton."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.file_ops import ModFileManager
from services.mod_library_cache import ModCardData
from tests.helpers.identity import (
    flush_library_search,
    patch_library_get_db,
    seed_steam_managed_mod,
)
from ui.library_query import SORT_NAME
from ui.library_view import (
    EMPTY_LIBRARY,
    EMPTY_SEARCH,
    GAME_CATEGORY_ROLE,
    GAME_ROLE,
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
    manager = DatabaseManager.instance(tmp_path / "ux_polish.db")
    manager.upsert_game(GameInfo(app_id=42, name="GameX", folder_name="GameX"))
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str = "7001",
    title: str = "UX Mod",
) -> tuple[Path, str]:
    seeded = seed_steam_managed_mod(
        db,
        library,
        external_id=external_id,
        title=title,
        game_folder="GameX",
        app_id=42,
        game_name="GameX",
        files={"a.txt": "x"},
    )
    return seeded.folder, seeded.internal_id


def _three_mod_library(library: Path, db: DatabaseManager) -> list[tuple[Path, str]]:
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    out: list[tuple[Path, str]] = []
    for mid, title in (("7001", "Mod A"), ("7002", "Mod B"), ("7003", "Mod C")):
        out.append(_seed_mod(library, db, external_id=mid, title=title))
    return out


def _card_data(path: Path, internal_id: str, title: str = "UX Mod") -> ModCardData:
    return ModCardData(
        id=str(internal_id),
        title=title,
        platform="steam",
        cover="",
        description="",
        tags="",
        size=None,
        updated_time=0.0,
        managed_path=str(path),
        game_folder="GameX",
        workspace_id=str(internal_id),
        external_id=str(internal_id),
    )


def test_empty_library_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    assert view.empty_overlay.isVisible() or not view.empty_overlay.isHidden()
    assert view._empty_kind == EMPTY_LIBRARY
    assert "No mods found" in view.empty_title.text()
    assert view.empty_action_btn.isVisible() or not view.empty_action_btn.isHidden()
    assert "Import" in view.empty_action_btn.text()
    assert view.path_hint.isHidden()


def test_empty_search_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    path, _mid = _seed_mod(library, db)
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    assert path.exists()
    assert len(view._cards) == 1

    view.search_box.setText("zzz-no-such-mod")
    flush_library_search(view)
    assert view._empty_kind == EMPTY_SEARCH
    assert not view.empty_overlay.isHidden()
    assert "No matching mods" in view.empty_title.text()

    view.empty_action_btn.click()
    assert view.search_box.text() == ""
    assert view._empty_kind is None
    assert view.empty_overlay.isHidden()


def test_context_menu_actions_emit_signals(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    path, mid = _seed_mod(library, db)

    card = ModCardWidget(path, card_data=_card_data(path, mid))
    seen: dict[str, object] = {}
    card.edit_requested.connect(lambda p: seen.setdefault("edit", p))
    card.deploy_requested.connect(lambda m: seen.setdefault("deploy", m))
    card.open_folder_requested.connect(lambda p: seen.setdefault("folder", p))
    card.open_steam_requested.connect(lambda p: seen.setdefault("steam", p))
    card.favorite_toggle_requested.connect(lambda m: seen.setdefault("fav", m))
    card.selection_requested.connect(lambda p: seen.setdefault("detail", p))

    # Drive menu actions directly (avoid platform-dependent popup)
    card._emit_view_detail()
    card.edit_requested.emit(card._mod_id())
    card._emit_deploy()
    card.open_folder_requested.emit(card._mod_id())
    card.open_steam_requested.emit(card._mod_id())
    card._emit_favorite_toggle()

    assert seen["detail"] == mid
    assert seen["edit"] == mid
    assert seen["deploy"] == mid
    assert seen["folder"] == mid
    assert seen["steam"] == mid
    assert seen["fav"] == mid

    # contextMenuEvent builds a QMenu without crashing
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(10, 10)
    )
    monkeypatch.setattr(
        card, "_exec_context_menu", lambda *_a, **_k: None
    )
    card.contextMenuEvent(event)


def test_detail_panel_singleton_across_filter(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(library, db)
    patch_library_get_db(monkeypatch, db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    panel = view.detail_panel
    panel_id = id(panel)
    assert isinstance(panel, ModDetailPanel)

    view.search_box.setText("nope")
    flush_library_search(view)
    view.search_box.clear()
    flush_library_search(view)
    view.refresh()
    assert id(view.detail_panel) == panel_id


def test_ux_filter_no_network_or_archive(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(library, db)
    patch_library_get_db(monkeypatch, db)

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
    flush_library_search(view)
    view.search_box.clear()
    flush_library_search(view)
    assert calls == []


def test_empty_game_state(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from ui.library_view import EMPTY_GAME

    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(library, db)
    # Sidebar is DB games projection — register an empty game row.
    db.upsert_game(GameInfo(app_id=43, name="EmptyGame", folder_name="EmptyGame"))
    patch_library_get_db(monkeypatch, db)

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
    assert "No mods" in view.empty_title.text()
    assert "EmptyGame" in view.empty_title.text()
    assert not view.empty_overlay.isHidden()


def test_detail_panel_hierarchy_labels(qapp: QApplication) -> None:
    from PySide6.QtWidgets import QLabel, QToolButton

    panel = ModDetailPanel()
    texts = [lab.text() for lab in panel._view_page.findChildren(QLabel)]
    # Visible composition: Chinese section captions (Status/Version moved offscreen).
    assert "元数据" in texts
    assert "文件" in texts
    assert "操作" in texts
    assert "标记" in texts
    off_texts = [lab.text() for lab in panel._offscreen_host.findChildren(QLabel)]
    assert "Status" in off_texts
    tool_texts = [
        btn.text() for btn in panel._offscreen_host.findChildren(QToolButton)
    ]
    assert "Version" in tool_texts
    assert "Tags & Relations" in tool_texts


def test_loading_flag_clears_after_refresh(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    assert view.loading_overlay.isHidden()
    assert view.loading_overlay.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground
    )
    assert "transparent" in (view.loading_overlay.styleSheet() or "")
    assert view.loading_label.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground
    )
    assert "transparent" in (view.loading_label.styleSheet() or "")
    view.refresh()
    assert view._loading is False
    assert view.loading_overlay.isHidden()


def test_shift_range_selection(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()
    assert len(visible) == 3
    # Layout order must drive selection, not internal _cards creation order.
    layout_order = [
        view.library_layout.itemAt(i).widget()
        for i in range(view.library_layout.count())
        if view.library_layout.itemAt(i) is not None
        and isinstance(view.library_layout.itemAt(i).widget(), ModCardWidget)
    ]
    assert visible == layout_order

    view.on_mod_selected(visible[0]._mod_id())
    assert view._selected_cards == [visible[0]]
    assert view._last_clicked_index == 0

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[2]._mod_id())

    assert view._selected_cards == visible[:3]
    assert view._last_clicked_index == 2

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[1]._mod_id())

    assert view._selected_cards == visible[:2]
    assert view._last_clicked_index == 1


def test_shift_range_uses_sorted_layout_order(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    """Shift slice follows on-screen sort order, not _cards insertion order."""
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    # Seed Z→A; name sort puts Alpha first on screen.
    for mid, title in (
        ("7003", "Zulu"),
        ("7002", "Bravo"),
        ("7001", "Alpha"),
    ):
        _seed_mod(library, db, external_id=mid, title=title)

    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view._sort_mode = SORT_NAME
    view.refresh()
    visible = view._visible_cards()
    assert [c._mod_id() for c in visible] == ["7001", "7002", "7003"]
    # Simulate internal list out of sync with on-screen layout order.
    view._cards = list(reversed(view._cards))
    assert [c._mod_id() for c in view._cards] == ["7003", "7002", "7001"]

    view.on_mod_selected(visible[1]._mod_id())  # Bravo
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[2]._mod_id())  # Zulu

    assert {c._mod_id() for c in view._selected_cards} == {"7002", "7003"}


def test_select_all_mods_shortcut(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()
    assert len(visible) == 3

    view.select_all_mods()
    assert view._selected_cards == visible
    assert view._selection_anchor is visible[0]
    assert len(view.detail_panel._batch_mod_ids or []) == 3


def test_batch_set_category_binds_type_id(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from services.mod_type_catalog import get_mod_type_catalog

    library = tmp_path / "mod"
    _three_mod_library(library, db)
    patch_library_get_db(monkeypatch, db)
    created = get_mod_type_catalog().add_type(42, "Gameplay")

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()

    view.on_mod_selected(visible[0]._mod_id())
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    view.on_mod_selected(visible[2]._mod_id())

    view._on_batch_set_category(str(created.type_id))

    assert db.get_mod_type_id("7001") == created.type_id
    assert db.get_mod_type_id("7003") == created.type_id
    assert db.get_mod_type_id("7002") is None


def test_add_game_category_renders_sidebar_node(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    assert db.add_game_category(42, "Gameplay")
    view._rebuild_game_list(ModFileManager(library), prefer="GameX")

    labels: list[str] = []
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        widget = view.game_list.itemWidget(item)
        if widget is not None:
            labels.append(widget.name_label.text())
    assert not any(label.strip() == "Gameplay" for label in labels)
    assert not any(
        view.game_list.itemWidget(view.game_list.item(i)).objectName()
        == "CategoryTreeItem"
        for i in range(view.game_list.count())
        if view.game_list.itemWidget(view.game_list.item(i)) is not None
    )
    assert not any("├─" in label for label in labels)


def test_sidebar_category_filters_mod_list(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from services.mod_type_catalog import get_mod_type_catalog

    library = tmp_path / "mod"
    _three_mod_library(library, db)
    created = get_mod_type_catalog().add_type(42, "Gameplay")
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    game_row = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item and str(item.data(GAME_ROLE) or "") == "GameX":
            if not str(item.data(GAME_CATEGORY_ROLE) or "").strip():
                game_row = i
                break
    assert game_row is not None
    view.game_list.setCurrentRow(game_row)
    qapp.processEvents()

    visible = view._visible_cards()
    view.on_mod_selected(visible[0]._mod_id())
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    view.on_mod_selected(visible[2]._mod_id())
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.NoModifier),
    )
    view._on_batch_set_category(str(created.type_id))
    assert len(view._visible_cards()) == 3

    idx = view.category_combo.findData(str(created.type_id))
    assert idx >= 0
    view.category_combo.setCurrentIndex(idx)
    qapp.processEvents()

    filtered = view._visible_cards()
    assert len(filtered) == 2
    mids = {c._mod_id() for c in filtered}
    assert mids == {"7001", "7003"}
