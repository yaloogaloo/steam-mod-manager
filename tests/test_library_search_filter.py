"""Mod Library search / filter / sort (local UI + query only)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_query import (
    FILTER_ALL,
    FILTER_CONFLICT,
    FILTER_DEPLOYED,
    FILTER_DISABLED,
    FILTER_FAVORITE,
    FILTER_INVALID,
    FILTER_OFFLINE_MISSING,
    FILTER_OFFLINE_PRESENT,
    FILTER_PLATFORM_ALL,
    FILTER_PLATFORM_GITHUB,
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    SORT_MTIME,
    SORT_NAME,
    ModFilterIndex,
    filter_and_sort,
    matches_search,
    matches_status_filter,
    offline_page_exists,
)
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
    manager = DatabaseManager(tmp_path / "lib_search.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _idx(**kwargs) -> ModFilterIndex:
    base = dict(
        mod_id="1",
        display_name="Alpha",
        steam_name="Steam Alpha",
        notes="",
        game_name="Palworld",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="Alpha",
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def _visible_cards(view: ModLibraryView) -> list:
    """Prefer layout membership — QWidget.isVisible needs a shown ancestor."""
    return [
        card
        for _index, card in view._card_entries
        if not card.isHidden() and card.parent() is view.library_host
    ]


def test_matches_search_display_steam_notes_id_game() -> None:
    item = _idx(
        display_name="自定义名",
        steam_name="Workshop Title",
        notes="我的备注关键字",
        workspace_id="424242",
        game_name="Palworld",
    )
    assert matches_search(item, "自定义")
    assert matches_search(item, "workshop")
    assert matches_search(item, "备注")
    assert matches_search(item, "424242")
    assert matches_search(item, "pal")
    assert not matches_search(item, "zzz-no-match")


def test_matches_search_external_id_and_source_url() -> None:
    item = _idx(
        display_name="Pal Analyzer",
        steam_name="Pal Analyzer",
        mod_id="9000000000000336",
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        platform="nexus",
    )
    assert matches_search(item, "336")
    assert matches_search(item, "nexusmods.com/palworld")
    assert not matches_search(item, "999999")


def test_status_filters() -> None:
    fav = _idx(favorite=True)
    dep = _idx(deployed=True)
    online = _idx(has_offline=True)
    missing = _idx(has_offline=False)

    assert matches_status_filter(fav, FILTER_FAVORITE)
    assert not matches_status_filter(dep, FILTER_FAVORITE)
    assert matches_status_filter(dep, FILTER_DEPLOYED)
    assert matches_status_filter(online, FILTER_OFFLINE_PRESENT)
    assert matches_status_filter(missing, FILTER_OFFLINE_MISSING)
    assert matches_status_filter(fav, FILTER_ALL)


def test_filter_and_sort_order() -> None:
    a = _idx(mod_id="1", display_name="Bravo", sort_name="Bravo", mtime=10)
    b = _idx(mod_id="2", display_name="Alpha", sort_name="Alpha", mtime=5)
    entries = [(a, "A"), (b, "B")]
    by_name = filter_and_sort(entries, sort_mode=SORT_NAME)
    assert by_name == ["B", "A"]
    by_mtime = filter_and_sort(entries, sort_mode=SORT_MTIME)
    assert by_mtime == ["A", "B"]


def test_offline_page_exists_is_file_only(tmp_path: Path) -> None:
    mod = tmp_path / "G" / "M"
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    assert offline_page_exists(mod) is False
    index = info / "index.html"
    index.write_text("x", encoding="utf-8")
    assert offline_page_exists(mod) is True
    # Existence only (is_file) — empty stub still counts; never reads HTML bytes
    index.write_bytes(b"")
    assert offline_page_exists(mod) is True


def _seed_library(library: Path, db: DatabaseManager) -> dict[str, Path]:
    db.update_game_deploy_config(1623730, name="Palworld", mod_path="")

    paths: dict[str, Path] = {}
    specs = [
        ("1001", "Cool Mod", "Cool Mod", "", False, False, True),
        ("1002", "Other", "Other Steam", "note-xyz", True, True, False),
        ("1003", "Plain", "Plain", "", False, False, False),
    ]
    for mid, folder, title, notes, fav, deployed, offline in specs:
        mod = library / "Palworld" / folder
        info = mod / INFO_DIR_NAME
        info.mkdir(parents=True)
        (mod / "file.txt").write_text("x", encoding="utf-8")
        (info / METADATA_FILENAME).write_text(
            "{\n"
            f'  "published_file_id": "{mid}",\n'
            f'  "title": "{title}",\n'
            '  "app_id": 1623730,\n'
            '  "game_name": "Palworld"\n'
            "}\n",
            encoding="utf-8",
        )
        if offline:
            (info / "index.html").write_text("<html></html>", encoding="utf-8")
        db.upsert_mod(
            ModMetadata(published_file_id=mid, title=title, app_id=1623730)
        )
        db.update_mod_user_metadata(
            mid,
            {
                "display_name": folder if folder != title else "",
                "user_notes": notes,
                "favorite": fav,
            },
        )
        if deployed:
            db.update_mod_deploy_status(
                mid,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path="/tmp/out",
            )
        paths[mid] = mod
    return paths


def test_library_search_and_filters(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _seed_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    assert len(view._cards) == 3
    assert len(_visible_cards(view)) == 3

    view.search_box.setText("note-xyz")
    assert len(_visible_cards(view)) == 1
    assert _visible_cards(view)[0]._mod_id() == "1002"

    view.search_box.clear()
    view._filter_buttons[FILTER_FAVORITE].setChecked(True)
    assert [c._mod_id() for c in _visible_cards(view)] == ["1002"]

    view._filter_buttons[FILTER_DEPLOYED].setChecked(True)
    assert [c._mod_id() for c in _visible_cards(view)] == ["1002"]

    view._filter_buttons[FILTER_OFFLINE_MISSING].setChecked(True)
    ids = sorted(c._mod_id() for c in _visible_cards(view))
    assert ids == ["1002", "1003"]

    view._filter_buttons[FILTER_ALL].setChecked(True)
    view.search_box.setText("Palworld")
    assert len(_visible_cards(view)) == 3

    assert set(view._filter_buttons) == {
        FILTER_ALL,
        FILTER_FAVORITE,
        FILTER_DEPLOYED,
        FILTER_INVALID,
        FILTER_CONFLICT,
        FILTER_DISABLED,
        FILTER_OFFLINE_MISSING,
        FILTER_PLATFORM_ALL,
        FILTER_PLATFORM_STEAM,
        FILTER_PLATFORM_NEXUS,
        FILTER_PLATFORM_GITHUB,
    }
    assert [btn.text() for btn in view._filter_buttons.values()] == [
        "全部",
        "收藏",
        "已部署",
        "失效",
        "冲突",
        "已禁用",
        "离线页面缺失",
        "全部平台",
        "Steam",
        "Nexus",
        "GitHub",
    ]


def test_filter_keeps_detail_panel_singleton(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    paths = _seed_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    panel_id = id(view.detail_panel)
    view.detail_panel.show_mod(paths["1001"])

    view.search_box.setText("Other")
    view._filter_buttons[FILTER_FAVORITE].setChecked(True)
    view.sort_combo.setCurrentIndex(1)  # 名称

    assert id(view.detail_panel) == panel_id


def test_filter_does_not_touch_archive_or_read_html(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _seed_library(library, db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append("archive")
        raise AssertionError("archive must not run during library filter")

    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.archive", boom, raising=False
    )
    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.ensure_offline_page",
        boom,
        raising=False,
    )

    real_read = Path.read_text

    def guarded_read(self: Path, *a, **k):
        if self.name == "index.html":
            calls.append("read_html")
            raise AssertionError("must not read index.html during filter")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded_read)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    view.search_box.setText("Cool")
    view._filter_buttons[FILTER_OFFLINE_MISSING].setChecked(True)
    view._filter_buttons[FILTER_ALL].setChecked(True)

    assert calls == []
    assert matches_status_filter(_idx(has_offline=True), FILTER_OFFLINE_PRESENT)


def test_get_mods_search_fields_batch(db: DatabaseManager) -> None:
    db.update_game_deploy_config(1, name="TestGame", mod_path="")
    db.upsert_mod(ModMetadata(published_file_id="501", title="Steam Title", app_id=1))
    db.update_mod_user_metadata(
        "501",
        {"display_name": "Shown", "user_notes": "hello", "favorite": True},
    )
    db.update_mod_deploy_status(
        "501",
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="/x",
    )
    fields = db.get_mods_search_fields(["501", "999", "not-an-id"])
    assert "501" in fields
    assert "999" not in fields
    row = fields["501"]
    assert row.display_name == "Shown"
    assert row.steam_name == "Steam Title"
    assert row.user_notes == "hello"
    assert row.favorite is True
    assert row.deploy_status == DEPLOY_STATUS_DEPLOYED
