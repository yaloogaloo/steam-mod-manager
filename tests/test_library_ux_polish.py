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
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
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


def _three_mod_library(
    library: Path, db: DatabaseManager
) -> list[Path]:
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    paths: list[Path] = []
    for mid, title in (("7001", "Mod A"), ("7002", "Mod B"), ("7003", "Mod C")):
        paths.append(_mod(library, mid=mid, title=title))
        db.upsert_mod(
            ModMetadata(published_file_id=mid, title=title, app_id=42)
        )
    return paths


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
    assert "No mods found" in view.empty_title.text()
    assert view.empty_action_btn.isVisible() or not view.empty_action_btn.isHidden()
    assert "Import" in view.empty_action_btn.text()
    assert view.path_hint.isHidden()
    assert view.deploy_audit_banner.isHidden()


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
    assert "No matching mods" in view.empty_title.text()

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
    assert "No mods" in view.empty_title.text()
    assert "EmptyGame" in view.empty_title.text()
    assert not view.empty_overlay.isHidden()


def test_detail_panel_hierarchy_labels(qapp: QApplication) -> None:
    from PySide6.QtWidgets import QLabel, QToolButton

    panel = ModDetailPanel()
    texts = [lab.text() for lab in panel._view_page.findChildren(QLabel)]
    tool_texts = [btn.text() for btn in panel._view_page.findChildren(QToolButton)]
    # Header is cover+title (no section caption). Status + 文件 + Actions + collapsibles.
    assert "Status" in texts
    assert "文件" in texts
    assert "操作" in texts
    assert "元数据" in texts
    assert "Version" in tool_texts
    assert "Tags & Relations" in tool_texts


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
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

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

    view.on_mod_selected(visible[0].managed_path)
    assert view._selected_cards == [visible[0]]
    assert view._last_clicked_index == 0

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[2].managed_path)

    assert view._selected_cards == visible[:3]
    assert view._last_clicked_index == 0

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[1].managed_path)

    assert view._selected_cards == visible[:2]
    assert view._last_clicked_index == 0


def test_shift_range_uses_sorted_layout_order(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    """Shift slice follows on-screen sort order, not _cards insertion order."""
    import os
    import time

    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    # Create in Z→A folder order; mtime desc puts Alpha first on screen.
    for mid, title, bump in (
        ("7003", "Zulu", 1),
        ("7002", "Bravo", 5),
        ("7001", "Alpha", 10),
    ):
        path = _mod(library, mid=mid, title=title)
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=42))
        t = time.time() + bump
        os.utime(path, (t, t))

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()
    assert [c._mod_id() for c in visible] == ["7001", "7002", "7003"]
    # Simulate internal list out of sync with on-screen layout order.
    view._cards = list(reversed(view._cards))
    assert [c._mod_id() for c in view._cards] == ["7003", "7002", "7001"]

    view.on_mod_selected(visible[1].managed_path)  # Bravo
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )
    view.on_mod_selected(visible[2].managed_path)  # Zulu

    assert {c._mod_id() for c in view._selected_cards} == {"7002", "7003"}


def test_select_all_mods_shortcut(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()
    assert len(visible) == 3

    view.select_all_mods()
    assert view._selected_cards == visible
    assert view._selection_anchor is visible[0]
    assert len(view.detail_panel._batch_mod_ids or []) == 3


def test_batch_set_category_syncs_db_and_sidecar(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    from services.info_sidecar import load_info_sidecar

    library = tmp_path / "mod"
    _three_mod_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    visible = view._visible_cards()

    view.on_mod_selected(visible[0].managed_path)
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    view.on_mod_selected(visible[2].managed_path)

    view._on_batch_set_category("Gameplay")

    assert db.get_category_tags("7001") == ["Gameplay"]
    assert db.get_category_tags("7003") == ["Gameplay"]
    assert db.get_category_tags("7002") == []

    for mid in ("7001", "7003"):
        card = next(c for c in visible if c._mod_id() == mid)
        side = load_info_sidecar(card.managed_path)
        assert side is not None
        assert side.category == "Gameplay"


def test_add_game_category_renders_sidebar_node(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

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
    assert any(label.strip() == "Gameplay" for label in labels)
    assert not any("├─" in label for label in labels)


def test_sidebar_category_filters_mod_list(
    qapp: QApplication,
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = tmp_path / "mod"
    _three_mod_library(library, db)
    db.add_game_category(42, "Gameplay")
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

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
    view.on_mod_selected(visible[0].managed_path)
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    view.on_mod_selected(visible[2].managed_path)
    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.NoModifier),
    )
    view._on_batch_set_category("Gameplay")
    assert len(view._visible_cards()) == 3

    cat_row = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if str(item.data(GAME_CATEGORY_ROLE) or "") == "Gameplay":
            cat_row = i
            break
    assert cat_row is not None
    # Categories are collapsed by default — expand parent game first.
    view._expanded_games.add("GameX")
    view._sync_category_row_visibility()
    view.game_list.setCurrentRow(cat_row)
    qapp.processEvents()

    filtered = view._visible_cards()
    assert len(filtered) == 2
    mids = {c._mod_id() for c in filtered}
    assert mids == {"7001", "7003"}
