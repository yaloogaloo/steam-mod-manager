"""Mod Library search / filter / sort (local UI + query only)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    flush_library_search,
    patch_library_get_db,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from services.file_ops import INFO_DIR_NAME
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
    manager = DatabaseManager.instance(tmp_path / "lib_search.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
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


def test_matches_status_filters() -> None:
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


def test_offline_page_exists_uses_manifest_probe(tmp_path: Path) -> None:
    from services.info_asset_runtime import probe_live_offline_available

    mod = tmp_path / "G" / "M"
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    assert probe_live_offline_available(mod) is None
    assert offline_page_exists(mod) is False
    index = info / "index.html"
    index.write_text("x", encoding="utf-8")
    # HTML without manifest is not OPEN-capable.
    assert probe_live_offline_available(mod) is None
    assert offline_page_exists(mod) is False
    (info / "manifest.json").write_text(
        '{"schema_version": 1, "assets": []}', encoding="utf-8"
    )
    assert probe_live_offline_available(mod) is not None
    assert offline_page_exists(mod) is True


def _seed_library(library: Path, db: DatabaseManager) -> dict[str, Path]:
    db.update_game_deploy_config(1623730, name="Palworld", mod_path="")

    paths: dict[str, Path] = {}
    pks: dict[str, str] = {}
    specs = [
        ("1001", "Cool Mod", "Cool Mod", "", False, False, True),
        ("1002", "Other", "Other Steam", "note-xyz", True, True, False),
        ("1003", "Plain", "Plain", "", False, False, False),
    ]
    for mid, folder, title, notes, fav, deployed, offline in specs:
        created = create_steam_test_mod(
            db, external_id=mid, title=title, app_id=1623730, game_name="Palworld"
        )
        internal_id = str(created.mod_id)
        pks[mid] = internal_id
        mod = library / "Palworld" / folder
        mod.mkdir(parents=True, exist_ok=True)
        (mod / "file.txt").write_text("x", encoding="utf-8")
        write_info_sidecar(
            mod,
            internal_id=internal_id,
            title=title,
            external_id=mid,
            workspace_id=mid,
            app_id=1623730,
            game_name="Palworld",
        )
        if offline:
            (mod / INFO_DIR_NAME / "index.html").write_text(
                "<html></html>", encoding="utf-8"
            )
            db.update_mod_offline_status(internal_id, status="generated")
        bind_managed_path(
            db, internal_id, mod, game_name="Palworld", title=title
        )
        db.update_mod_user_metadata(
            internal_id,
            {
                "display_name": folder if folder != title else "",
                "user_notes": notes,
                "favorite": fav,
            },
        )
        if deployed:
            db.update_mod_deploy_status(
                internal_id,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path="/tmp/out",
            )
        paths[mid] = mod
    return paths, pks


def test_library_search_and_filters(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _paths, pks = _seed_library(library, db)
    patch_library_get_db(monkeypatch, db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    assert len(view._cards) == 3
    assert len(view._visible_cards()) == 3

    view.search_box.setText("note-xyz")
    flush_library_search(view)
    assert len(view._visible_cards()) == 1
    assert view._visible_cards()[0]._mod_id() == pks["1002"]

    view.search_box.clear()
    flush_library_search(view)
    view._filter_buttons[FILTER_FAVORITE].setChecked(True)
    assert [c._mod_id() for c in view._visible_cards()] == [pks["1002"]]

    view._filter_buttons[FILTER_DEPLOYED].setChecked(True)
    assert [c._mod_id() for c in view._visible_cards()] == [pks["1002"]]

    view._filter_buttons[FILTER_ALL].setChecked(True)
    view.search_box.setText("Palworld")
    flush_library_search(view)
    assert len(view._visible_cards()) == 3

    from ui.library_query import (
        FILTER_ANOMALY,
        FILTER_CONTENT_MISSING,
        STATUS_FILTER_LABELS,
    )

    assert set(view._filter_buttons) == {key for key, _ in STATUS_FILTER_LABELS}
    assert [btn.text() for btn in view._filter_buttons.values()] == [
        label for _key, label in STATUS_FILTER_LABELS
    ]
    assert FILTER_FAVORITE in view._filter_buttons
    assert FILTER_DEPLOYED in view._filter_buttons
    assert FILTER_CONTENT_MISSING in view._filter_buttons
    assert FILTER_ANOMALY in view._filter_buttons


def test_filter_keeps_detail_panel_singleton(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    paths, pks = _seed_library(library, db)
    patch_library_get_db(monkeypatch, db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    panel_id = id(view.detail_panel)
    view.detail_panel.show_mod(paths["1001"], mod_id=pks["1001"])

    view.search_box.setText("Other")
    flush_library_search(view)
    view._filter_buttons[FILTER_FAVORITE].setChecked(True)
    view.sort_combo.setCurrentIndex(1)  # 名称

    assert id(view.detail_panel) == panel_id


def test_filter_does_not_touch_archive_or_read_html(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _paths, pks = _seed_library(library, db)
    patch_library_get_db(monkeypatch, db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)

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
    flush_library_search(view)
    view._filter_buttons[FILTER_FAVORITE].setChecked(True)
    view._filter_buttons[FILTER_ALL].setChecked(True)

    assert calls == []
    assert matches_status_filter(_idx(has_offline=True), FILTER_OFFLINE_PRESENT)


def test_get_mods_search_fields_batch(db: DatabaseManager) -> None:
    db.update_game_deploy_config(1, name="TestGame", mod_path="")
    created = create_steam_test_mod(db, external_id="501", title="Steam Title", app_id=1)
    pk = str(created.mod_id)

    db.update_mod_user_metadata(
        pk,
        {"display_name": "Shown", "user_notes": "hello", "favorite": True},
    )
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="/x",
    )
    fields = db.get_mods_search_fields([pk, "999", "not-an-id"])
    assert pk in fields
    assert "999" not in fields
    row = fields[pk]
    assert row.display_name == "Shown"
    assert row.steam_name == "Steam Title"
    assert row.user_notes == "hello"
    assert row.favorite is True
    assert row.deploy_status == DEPLOY_STATUS_DEPLOYED
