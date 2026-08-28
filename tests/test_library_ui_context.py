"""Phase 11: game-scoped Library sources / categories and compact filters."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ui.library_query import (
    FILTER_ANOMALY,
    FILTER_BACKUP_INVALID,
    FILTER_CATEGORY_ALL,
    FILTER_CONTENT_MISSING,
    FILTER_DEPLOYED,
    FILTER_FAVORITE,
    FILTER_FOLDER_MISSING,
    FILTER_IDENTITY_CONFLICT,
    FILTER_PLATFORM_ALL,
    FILTER_PLATFORM_GITHUB,
    FILTER_PLATFORM_MODIO,
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    STATUS_FILTER_LABELS,
    ModFilterIndex,
    coerce_filter_selection,
    collect_category_labels,
    collect_source_keys,
    filter_and_sort,
    matches_status_filter,
)


def _idx(
    *,
    mod_id: str = "1",
    source_type: str = "steam",
    platform: str = "",
    category_tags: str = "",
    content_status: str = "",
    deployed: bool = False,
    favorite: bool = False,
    is_invalid: bool = False,
    conflict_status: str = "none",
    enabled: bool = True,
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="G",
        favorite=favorite,
        deployed=deployed,
        has_offline=True,
        mtime=1.0,
        sort_name=f"Mod {mod_id}",
        platform=platform or source_type,
        source_type=source_type,
        category_tags=category_tags,
        content_status=content_status,
        is_invalid=is_invalid,
        invalid=is_invalid,
        conflict_status=conflict_status,
        enabled=enabled,
    )


def test_case1_sources_are_game_scoped() -> None:
    game_a = [
        _idx(mod_id="a1", platform="steam", source_type="external"),
        _idx(mod_id="a2", platform="github", source_type="external"),
    ]
    game_b = [
        _idx(mod_id="b1", platform="modio", source_type="external"),
        _idx(mod_id="b2", platform="nexus", source_type="external"),
    ]
    assert collect_source_keys(game_a) == [
        FILTER_PLATFORM_STEAM,
        FILTER_PLATFORM_GITHUB,
    ]
    assert collect_source_keys(game_b) == [
        FILTER_PLATFORM_NEXUS,
        FILTER_PLATFORM_MODIO,
    ]
    assert FILTER_PLATFORM_STEAM not in collect_source_keys(game_b)
    assert FILTER_PLATFORM_MODIO not in collect_source_keys(game_a)
    # Provenance-only rows must not invent an External platform chip
    assert "platform_external" not in collect_source_keys(
        [_idx(mod_id="x", platform="", source_type="external")]
    )


def test_case2_categories_are_game_scoped() -> None:
    game_a = [
        _idx(mod_id="a1", category_tags="角色"),
        _idx(mod_id="a2", category_tags="美化"),
    ]
    game_b = [
        _idx(mod_id="b1", category_tags="建筑"),
        _idx(mod_id="b2", category_tags="地图"),
    ]
    assert set(collect_category_labels(game_a)) == {"角色", "美化"}
    assert set(collect_category_labels(game_b)) == {"建筑", "地图"}
    assert "建筑" not in collect_category_labels(game_a)


def test_case3_switch_game_resets_missing_filters() -> None:
    sources_b = [FILTER_PLATFORM_MODIO, FILTER_PLATFORM_GITHUB]
    cats_b = ["建筑", "地图"]
    assert (
        coerce_filter_selection(
            FILTER_PLATFORM_STEAM, sources_b, all_key=FILTER_PLATFORM_ALL
        )
        == FILTER_PLATFORM_ALL
    )
    assert (
        coerce_filter_selection("角色", cats_b, all_key=FILTER_CATEGORY_ALL)
        == FILTER_CATEGORY_ALL
    )
    assert (
        coerce_filter_selection(
            FILTER_PLATFORM_MODIO, sources_b, all_key=FILTER_PLATFORM_ALL
        )
        == FILTER_PLATFORM_MODIO
    )


def test_case4_game_list_has_no_category_tree_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from core.db_manager import DatabaseManager
    from core.models import ModMetadata
    from ui.library_view import GAME_CATEGORY_ROLE, ModLibraryView

    app = QApplication.instance() or QApplication([])
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "ui_ctx.db")
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    library = tmp_path / "mod"
    for mid, game, title, cat in (
        ("9101", "GameA", "Alpha", "角色"),
        ("9102", "GameB", "Beta", "建筑"),
    ):
        folder = library / game / title
        info = folder / ".info"
        info.mkdir(parents=True)
        (info / "mod.json").write_text(
            '{"published_file_id":"%s","title":"%s","game_name":"%s"}'
            % (mid, title, game),
            encoding="utf-8",
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title))
        db.add_category_tag(mid, cat)
        db.add_game_category(int(mid[:2]), cat)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    names = []
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        widget = view.game_list.itemWidget(item)
        assert widget is not None
        assert widget.objectName() == "GameTreeItem"
        assert not str(item.data(GAME_CATEGORY_ROLE) or "").strip()
        names.append(widget.name_label.text())
    assert "角色" not in names
    assert "建筑" not in names
    del app
    DatabaseManager.reset_instance()


def test_case5_game_rows_share_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.library_view import _GameFilterRow

    app = QApplication.instance() or QApplication([])
    a = _GameFilterRow("Anno 1800", 70, kind=_GameFilterRow.KIND_GAME)
    b = _GameFilterRow("战锤III", 0, kind=_GameFilterRow.KIND_GAME, overall_status="warning")
    c = _GameFilterRow("文明VI", 198, kind=_GameFilterRow.KIND_GAME)
    rows = (a, b, c)
    heights = {r.minimumHeight() for r in rows}
    assert heights == {_GameFilterRow.ROW_HEIGHT}
    assert {r.icon_label.text() for r in rows} == {"🎮"}
    assert a.name_label.alignment() == b.name_label.alignment()
    assert a.count_label.alignment() == b.count_label.alignment()
    assert a.icon_label.minimumWidth() == b.icon_label.minimumWidth() == 18
    assert not hasattr(a, "status_label")
    assert "⚠" not in a.name_label.text()
    assert "⚠" not in b.name_label.text()
    del app


def test_case6_source_category_helpers_do_not_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.mod_metadata_resolver as resolver

    spy = MagicMock(side_effect=AssertionError("must not scan disk"))
    monkeypatch.setattr(resolver, "list_visible_mods", spy)
    collect_source_keys([_idx(source_type="steam"), _idx(source_type="nexus")])
    collect_category_labels([_idx(category_tags="美化")])
    spy.assert_not_called()

    src = inspect.getsource(collect_source_keys) + inspect.getsource(
        collect_category_labels
    )
    assert "list_visible_mods" not in src

    from ui import library_view as lv

    bar_src = inspect.getsource(lv.ModLibraryView._rebuild_platform_filter_bar)
    cat_src = inspect.getsource(lv.ModLibraryView._refresh_category_combo)
    collect_src = inspect.getsource(lv.ModLibraryView._collect_active_platform_sources)
    assert "list_visible_mods" not in bar_src
    assert "list_visible_mods" not in cat_src
    assert "list_visible_mods" not in collect_src


def test_case7_status_filters_map_existing_model() -> None:
    missing = _idx(mod_id="1", content_status=FILTER_CONTENT_MISSING)
    conflict = _idx(mod_id="2", content_status=FILTER_IDENTITY_CONFLICT)
    backup = _idx(mod_id="3", content_status=FILTER_BACKUP_INVALID)
    folder = _idx(mod_id="4", content_status=FILTER_FOLDER_MISSING)
    invalid = _idx(mod_id="5", is_invalid=True)
    deployed = _idx(mod_id="6", deployed=True)
    healthy = _idx(mod_id="7", content_status="healthy")

    assert matches_status_filter(missing, FILTER_CONTENT_MISSING)
    assert not matches_status_filter(healthy, FILTER_CONTENT_MISSING)
    assert matches_status_filter(conflict, FILTER_ANOMALY)
    assert matches_status_filter(backup, FILTER_ANOMALY)
    assert matches_status_filter(folder, FILTER_ANOMALY)
    assert matches_status_filter(invalid, FILTER_ANOMALY)
    assert not matches_status_filter(missing, FILTER_ANOMALY)
    assert matches_status_filter(deployed, FILTER_DEPLOYED)
    assert not matches_status_filter(healthy, FILTER_DEPLOYED)

    labels = {key for key, _ in STATUS_FILTER_LABELS}
    assert labels == {
        "all",
        FILTER_CONTENT_MISSING,
        FILTER_ANOMALY,
        FILTER_DEPLOYED,
        FILTER_FAVORITE,
    }
    assert FILTER_BACKUP_INVALID not in labels
    assert FILTER_FOLDER_MISSING not in labels

    out = filter_and_sort(
        [(missing, "m"), (conflict, "c"), (deployed, "d")],
        filter_key=FILTER_ANOMALY,
    )
    assert out == ["c"]


def test_case8_favorite_filter_uses_real_db_flag() -> None:
    """Favorite is implemented (SQLite ``mods.favorite`` + card/detail toggle)."""
    fav = _idx(mod_id="1", favorite=True)
    plain = _idx(mod_id="2", favorite=False)
    assert matches_status_filter(fav, FILTER_FAVORITE)
    assert not matches_status_filter(plain, FILTER_FAVORITE)
    assert FILTER_FAVORITE in {key for key, _ in STATUS_FILTER_LABELS}


def _qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_nav_and_game_panel_share_top_layout() -> None:
    """Case 1: game panel is the first child of Library root (same top as nav)."""
    _qapp()
    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    root = view.layout()
    assert root is not None
    assert root.contentsMargins().top() == 0
    assert root.spacing() == 0
    first = root.itemAt(0).widget()
    assert first is view.splitter
    assert view.game_panel.parent() is view.splitter
    assert not hasattr(view, "deploy_audit_banner")
    assert view.game_panel.parentWidget() is view.splitter


def test_game_count_visible_with_long_name() -> None:
    """Case 2: long names elide; count stays fully visible."""
    _qapp()
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from ui.library_view import _GameFilterRow

    host = QWidget()
    host.setFixedWidth(168)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    row = _GameFilterRow("一个非常长的游戏名称用于挤压数量列测试XXXX", 461)
    lay.addWidget(row)
    host.show()
    QApplication.processEvents()
    row._apply_name_elide()
    QApplication.processEvents()
    assert row.count_label.text() == "461"
    count_w = row.count_label.minimumWidth()
    assert count_w >= row.count_label.fontMetrics().horizontalAdvance("461")
    assert row.count_label.x() >= row.name_label.x()
    assert row.count_label.x() + row.count_label.width() <= row.width()
    assert "461" in row.count_label.text()
    assert row.name_label.width() < row.name_label.fontMetrics().horizontalAdvance(
        row._base_name
    )
    host.close()


def test_source_bar_hidden_for_zero_or_one_source() -> None:
    """Case 3 / 6: 0–1 sources hide the whole source row."""
    _qapp()
    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    view._rebuild_platform_filter_bar([])
    assert view._source_row.isHidden()
    assert view._platform_filter == FILTER_PLATFORM_ALL

    view._rebuild_platform_filter_bar([FILTER_PLATFORM_STEAM])
    assert view._source_row.isHidden()
    assert view._platform_filter == FILTER_PLATFORM_ALL


def test_source_bar_visible_for_two_sources() -> None:
    """Case 4: two or more sources show 来源 + chips, vertically centered."""
    _qapp()
    from PySide6.QtCore import Qt

    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    view._rebuild_platform_filter_bar(
        [FILTER_PLATFORM_STEAM, FILTER_PLATFORM_NEXUS]
    )
    assert not view._source_row.isHidden()
    assert FILTER_PLATFORM_STEAM in view._platform_buttons
    assert FILTER_PLATFORM_NEXUS in view._platform_buttons
    layout = view._source_row.layout()
    assert layout.alignment() & Qt.AlignmentFlag.AlignVCenter
    item0 = layout.itemAt(0)
    assert item0.alignment() & Qt.AlignmentFlag.AlignVCenter


def test_source_resets_when_unavailable_for_game() -> None:
    """Case 5: switching to a game without the current source resets to 全部."""
    _qapp()
    from ui.library_view import ModLibraryView

    view = ModLibraryView()
    view._platform_filter = FILTER_PLATFORM_STEAM
    view._rebuild_platform_filter_bar(
        [FILTER_PLATFORM_MODIO, FILTER_PLATFORM_GITHUB]
    )
    assert view._platform_filter == FILTER_PLATFORM_ALL
    assert not view._source_row.isHidden()


def test_source_bar_uses_current_game_indexes_only() -> None:
    """Case 7 / 8: hide/show uses collect_source_keys on current entries, no scan."""
    from ui.library_query import collect_source_keys

    game_only = [
        _idx(mod_id="1", source_type="steam"),
        _idx(mod_id="2", source_type="steam"),
    ]
    assert collect_source_keys(game_only) == [FILTER_PLATFORM_STEAM]
    src = inspect.getsource(
        __import__("ui.library_view", fromlist=["ModLibraryView"]).ModLibraryView._rebuild_platform_filter_bar
    )
    assert "list_visible_mods" not in src
    assert "load_snapshot" not in src

