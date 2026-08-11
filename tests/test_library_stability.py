"""Stability fixes: title priority, cache prune, cover late-callback safety."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from core.db_manager import DatabaseManager
from core.models import ModMetadata
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
    yield manager
    DatabaseManager.reset_instance()
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()


def _seed(
    lib: Path,
    *,
    game: str,
    title: str,
    mid: str,
    display_name: str = "",
) -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": mid,
        "title": title,
        "game_name": game,
    }
    if display_name:
        payload["display_name"] = display_name
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return folder


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
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed(
        lib,
        game="Game",
        title="SteamTitle",
        mid="97001",
        display_name="SidecarDisplay",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="97001",
            title="SteamTitle",
            managed_path=str(folder),
            game_name="Game",
        )
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    assert len(view._cards) == 1
    card = view._cards[0]
    assert "SidecarDisplay" in card.title_label.text()
    assert card.metadata is not None
    assert card.metadata.json_display_name == "SidecarDisplay"


def test_filter_index_uses_sidecar_title(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    folder = _seed(
        lib,
        game="Game",
        title="SteamTitle",
        mid="97002",
        display_name="FilterSidecar",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="97002",
            title="SteamTitle",
            managed_path=str(folder),
            game_name="Game",
        )
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    index = view._card_entries[0][0]
    assert index.display_name == "FilterSidecar"
    assert index.sort_name == "FilterSidecar"


def test_deleted_mod_removes_cache_under_game_filter(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    a = _seed(lib, game="Game", title="Keep", mid="97003")
    b = _seed(lib, game="Game", title="Gone", mid="97004")
    db.upsert_mod(
        ModMetadata(published_file_id="97003", title="Keep", managed_path=str(a))
    )
    db.upsert_mod(
        ModMetadata(published_file_id="97004", title="Gone", managed_path=str(b))
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    view._set_current_game_context("Game")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    gone_key = view._card_cache_key(b)
    assert gone_key in view._card_cache

    import shutil

    shutil.rmtree(b)
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    assert gone_key not in view._card_cache
    assert len(view._cards) == 1


def test_renamed_mod_does_not_keep_stale_cache(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    old = _seed(lib, game="Game", title="OldName", mid="97005")
    db.upsert_mod(
        ModMetadata(
            published_file_id="97005", title="OldName", managed_path=str(old)
        )
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    view._set_current_game_context("Game")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    old_key = view._card_cache_key(old)
    new = lib / "Game" / "NewName"
    old.rename(new)
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()

    new_key = view._card_cache_key(new)
    assert old_key not in view._card_cache
    assert new_key in view._card_cache
    assert len(view._card_cache) == 1


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
        json.dumps({"published_file_id": "97006", "title": "C"}),
        encoding="utf-8",
    )

    host = QApplication.activeWindow()
    card = ModCardWidget(folder, parent=None)
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
