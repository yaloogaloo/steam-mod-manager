"""Library card cache — game switch / refresh must reuse ModCardWidget."""

from __future__ import annotations

import json
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

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from ui.library_view import ModLibraryView
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    from services.mod_library_cache import reset_library_cache

    reset_library_cache()
    manager = DatabaseManager.instance(tmp_path / "cache.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()
    reset_library_cache()


def _seed(lib: Path, db: DatabaseManager, game: str, title: str, mid: str) -> Path:
    app_id = abs(hash(game)) % 900000 + 100000
    db.update_game_deploy_config(app_id, name=game)
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game
    )
    internal_id = str(created.mod_id)
    folder = lib / game / title
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=mid,
        workspace_id=mid,
        app_id=app_id,
        game_name=game,
    )
    bind_managed_path(db, internal_id, folder, title=title, game_name=game)
    return folder


def test_refresh_reuses_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    patch_library_get_db(monkeypatch, db)
    monkeypatch.setattr("ui.library_view._library_load_sync", lambda: True)
    lib = tmp_path / "library"
    for i in range(5):
        _seed(lib, db, "GameA", f"Mod{i}", str(81000 + i))

    view = ModLibraryView()
    try:
        view.set_target_root(str(lib))
        view.refresh()
        qapp.processEvents()
        flush_library_search(view)

        assert view._card_create_count == 5
        first_ids = {id(c) for c in view._cards}
        assert len(first_ids) == 5

        view.refresh()
        qapp.processEvents()
        flush_library_search(view)

        assert view._card_create_count == 0
        assert view._card_reuse_count == 5
        assert {id(c) for c in view._cards} == first_ids
        assert len(view._card_cache) == 5
    finally:
        view.cancel_pending_library_load()
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_game_switch_reuses_cached_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    patch_library_get_db(monkeypatch, db)
    lib = tmp_path / "library"
    _seed(lib, db, "GameA", "Alpha", "82001")
    _seed(lib, db, "GameB", "Beta", "82002")


    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    # Select GameA via filter context
    view._set_current_game_context("GameA")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    alpha = view._cards[0]
    assert isinstance(alpha, ModCardWidget)
    alpha_id = id(alpha)

    view._set_current_game_context("GameB")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    assert view._cards[0].managed_path.name == "Beta"

    view._set_current_game_context("GameA")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    assert id(view._cards[0]) == alpha_id
    assert view._card_create_count == 0
    assert view._card_reuse_count == 1


def test_filter_does_not_create_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    patch_library_get_db(monkeypatch, db)
    lib = tmp_path / "library"
    for i in range(4):
        _seed(lib, db, "GameA", f"Mod{i}", str(83000 + i))

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    cache_n = len(view._card_cache)

    view.search_box.setText("Mod1")
    flush_library_search(view)
    qapp.processEvents()

    # create_count is per-render (reset each bind); filter must reuse cache.
    assert view._card_create_count == 0
    assert len(view._card_cache) == cache_n
    visible = view._visible_cards()
    assert len(visible) == 1
    assert "Mod1" in visible[0].managed_path.name
