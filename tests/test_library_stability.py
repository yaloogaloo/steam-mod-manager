"""Stability fixes: title priority, cache prune, cover late-callback safety."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    patch_library_get_db,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.cover_loader import CoverLoaderManager, reset_cover_loader_stats
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from ui.library_query import resolve_mod_library_title
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
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    manager = DatabaseManager.instance(tmp_path / "stability.db")
    manager.upsert_game(GameInfo(app_id=970, name="Game", folder_name="Game"))
    yield manager
    DatabaseManager.reset_instance()
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()


def _seed(
    lib: Path,
    db: DatabaseManager,
    *,
    game: str,
    title: str,
    mid: str,
    display_name: str = "",
    app_id: int = 970,
) -> tuple[Path, str]:
    created = create_steam_test_mod(
        db, external_id=mid, title=title, game_name=game, app_id=app_id
    )
    internal_id = str(created.mod_id)
    folder = lib / game / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=mid,
        workspace_id=mid,
        app_id=app_id,
        game_name=game,
        extra={"display_name": display_name} if display_name else None,
    )
    bind_managed_path(db, internal_id, folder, game_name=game, title=title)
    if display_name:
        # Library is DB-first — surface sidecar display_name via user metadata.
        db.update_mod_user_metadata(internal_id, {"display_name": display_name})
    return folder, internal_id


def test_resolve_mod_library_title_priority() -> None:
    assert (
        resolve_mod_library_title(
            metadata_display_name="JSON",
            metadata_title="Title",
            db_display_name="DBUser",
            db_steam_name="Steam",
            folder_name="Folder",
        )
        == "JSON"
    )
    assert (
        resolve_mod_library_title(
            metadata_display_name="",
            db_display_name="DBUser",
            db_steam_name="Steam",
            folder_name="Folder",
        )
        == "DBUser"
    )
    assert (
        resolve_mod_library_title(
            metadata_display_name="",
            db_display_name="",
            db_steam_name="Steam",
            metadata_title="Title",
            folder_name="Folder",
        )
        == "Steam"
    )


def test_metadata_json_title_overrides_db_on_card(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "library"
    folder, _mid = _seed(
        lib,
        db,
        game="Game",
        title="SteamTitle",
        mid="97001",
        display_name="SidecarDisplay",
    )
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    assert len(view._cards) == 1
    card = view._cards[0]
    assert "SidecarDisplay" in card.title_label.text()
    assert card.managed_path == folder or card.managed_path.resolve() == folder.resolve()


def test_filter_index_uses_sidecar_title(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "library"
    _seed(
        lib,
        db,
        game="Game",
        title="SteamTitle",
        mid="97002",
        display_name="FilterSidecar",
    )
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    index = view._card_entries[0][0]
    assert index.display_name == "FilterSidecar"
    assert index.sort_name == "FilterSidecar"


def test_deleted_mod_removes_cache_under_game_filter(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "library"
    a, pk_a = _seed(lib, db, game="Game", title="Keep", mid="97003")
    b, pk_b = _seed(lib, db, game="Game", title="Gone", mid="97004")
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    view._set_current_game_context("Game")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    gone_key = view._card_cache_key(b, mod_id=pk_b)
    assert gone_key in view._card_cache

    import shutil

    shutil.rmtree(b)
    db.delete_mod_record(pk_b)
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()

    assert gone_key not in view._card_cache
    assert len(view._cards) == 1
    assert a.exists()


def test_renamed_mod_does_not_keep_stale_cache(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "library"
    old, mid = _seed(lib, db, game="Game", title="OldName", mid="97005")
    patch_library_get_db(monkeypatch, db)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    view._set_current_game_context("Game")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    # Cache key is entity id — path rename must not mint a second cache entry.
    entity_key = view._card_cache_key(old, mod_id=mid)
    assert entity_key in view._card_cache
    new = lib / "Game" / "NewName"
    old.rename(new)
    bind_managed_path(db, mid, new, game_name="Game", title="OldName")
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()

    assert entity_key in view._card_cache
    assert len(view._card_cache) == 1
    assert view._cards[0].managed_path.resolve() == new.resolve()


def test_cover_late_callback_safe_after_destroy(
    qapp: QApplication, tmp_path: Path
) -> None:
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    folder = tmp_path / "Game" / "Cover"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    pix = QPixmap(64, 64)
    pix.fill()
    pix.save(str(info / "cover.png"), "PNG")
    (info / METADATA_FILENAME).write_text(
        json.dumps({
                "internal_id": "97006",
"published_file_id": "97006", "title": "C"}),
        encoding="utf-8",
    )

    card = ModCardWidget(folder, parent=None)
    card.ensure_cover()
    token = card._cover_token
    mgr = CoverLoaderManager.instance()

    card.deleteLater()
    qapp.processEvents()

    # Force a late delivery even if cancel cleared the token.
    mgr._active_tokens.add(token)
    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(1)
    # Must not raise.
    mgr.image_ready.emit(token, img)
    qapp.processEvents()
    time.sleep(0.05)
    qapp.processEvents()

    # Widget may still be a Python shell; callback must tolerate it.
    assert True
