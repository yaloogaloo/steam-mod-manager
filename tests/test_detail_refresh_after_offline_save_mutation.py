"""Regression: Detail must reload by mods.mod_id after offline-save mutation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_NONE,
    PLATFORM_STEAM,
)
from services.file_ops import INFO_DIR_NAME
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
    manager = DatabaseManager.instance(tmp_path / "detail_offline_refresh.db")
    manager.upsert_game(
        GameInfo(app_id=292030, name="Witcher3", folder_name="Witcher3")
    )
    yield manager
    DatabaseManager.reset_instance()


def test_detail_refresh_after_offline_save_mutation(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Mutation → Projection → View: offline save must not blank Detail metadata."""
    workshop = "3596053192"
    lib = tmp_path / "library"
    # Non-digit folder name: path-only show_mod cannot invent identity.
    folder = lib / "Witcher3" / "Cool_Mod_Offline"
    info = folder / INFO_DIR_NAME
    offline_dir = info / "offline"
    offline_dir.mkdir(parents=True)
    (folder / "mod.dll").write_bytes(b"dll")

    created = create_steam_test_mod(
        db,
        external_id=workshop,
        title="Offline Save Meta Title",
        app_id=292030,
        game_name="Witcher3",
    )
    pk = str(created.mod_id)
    entity_uuid = str(created.internal_id or "")
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title="Offline Save Meta Title",
        app_id=292030,
        game_name="Witcher3",
        platform=PLATFORM_STEAM,
        extra={
            "display_name": "Offline Save Meta Title",
            "author": "OfflineAuthor",
            "description": "Keep this description after save.",
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop}",
        },
    )
    # Ensure author/description survive panel paint (sidecar + DB display fields).
    meta_path = info / "metadata.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "author": "OfflineAuthor",
            "description": "Keep this description after save.",
            "display_name": "Offline Save Meta Title",
        }
    )
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    db.update_mod_offline_status(pk, status=OFFLINE_STATUS_NONE)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    qapp.processEvents()

    assert "Offline Save Meta Title" in (panel.view_title.text() or "")
    assert "OfflineAuthor" in (panel.meta_author_line.text() or "")
    assert "离线未保存" in (panel.view_offline.text() or "")
    assert panel.current_mod_id() == pk

    index = offline_dir / "index.html"
    index.write_text("<html><body>offline</body></html>", encoding="utf-8")
    db.update_mod_offline_status(pk, status=OFFLINE_STATUS_ARCHIVED)

    panel._on_offline_archive_finished(str(index))
    qapp.processEvents()

    assert panel.current_mod_id() == pk
    assert "Offline Save Meta Title" in (panel.view_title.text() or "")
    assert "OfflineAuthor" in (panel.meta_author_line.text() or "")
    assert "Keep this description after save." in (panel.meta_desc_line.text() or "")
    offline_text = panel.view_offline.text() or ""
    assert "已保存" in offline_text
    assert "离线未保存" not in offline_text
    assert entity_uuid  # Entity UUID stamped via prove_managed_folder


def test_path_only_show_mod_blanks_without_internal_id(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Guard: path-only reload is what caused blank metadata after mutation."""
    workshop = "8802"
    folder = tmp_path / "library" / "Game" / "Named_Folder"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "mod.dll").write_bytes(b"x")
    db.upsert_game(GameInfo(app_id=1, name="Game", folder_name="Game"))

    created = create_steam_test_mod(
        db, external_id=workshop, title="Should Not Vanish", app_id=1, game_name="Game"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title="Should Not Vanish",
        app_id=1,
        game_name="Game",
        extra={"display_name": "Should Not Vanish", "author": "PathOnlyAuthor"},
    )
    meta_path = info / "metadata.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["author"] = "PathOnlyAuthor"
    payload["display_name"] = "Should Not Vanish"
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    qapp.processEvents()
    assert "Should Not Vanish" in (panel.view_title.text() or "")

    panel.show_mod(folder)  # path only — resolver requires explicit mod_id
    qapp.processEvents()
    assert "Should Not Vanish" not in (panel.view_title.text() or "")
    assert panel.current_mod_id() != pk

    panel._reload_current_detail_from_projection(folder, mod_id=pk)
    qapp.processEvents()
    assert "Should Not Vanish" in (panel.view_title.text() or "")
    assert "PathOnlyAuthor" in (panel.meta_author_line.text() or "")
    assert panel.current_mod_id() == pk
