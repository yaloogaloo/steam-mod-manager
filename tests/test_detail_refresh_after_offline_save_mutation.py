"""Regression: Detail must reload by internal_id after offline-save mutation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_NONE,
    PLATFORM_STEAM,
)
from core.models import ModMetadata
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
    yield manager
    DatabaseManager.reset_instance()


def test_detail_refresh_after_offline_save_mutation(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Mutation → Projection → View: offline save must not blank Detail metadata."""
    mid = "8801"
    lib = tmp_path / "library"
    # Non-digit folder name: path-only show_mod cannot invent identity.
    folder = lib / "Witcher3" / "Cool_Mod_Offline"
    info = folder / INFO_DIR_NAME
    offline_dir = info / "offline"
    offline_dir.mkdir(parents=True)
    meta_payload = {
        "internal_id": mid,
        "workspace_id": "3596053192",
        "published_file_id": mid,
        "title": "Offline Save Meta Title",
        "display_name": "Offline Save Meta Title",
        "author": "OfflineAuthor",
        "description": "Keep this description after save.",
        "source_type": PLATFORM_STEAM,
        "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=3596053192",
    }
    (info / "metadata.json").write_text(
        json.dumps(meta_payload, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "mod.dll").write_bytes(b"dll")

    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title="Offline Save Meta Title",
            description="Keep this description after save.",
            source_type=PLATFORM_STEAM,
            url=meta_payload["url"],
            author="OfflineAuthor",
        )
    )
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder),
        workspace_id="3596053192",
        external_id="3596053192",
    )
    db.update_mod_offline_status(mid, status=OFFLINE_STATUS_NONE)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()

    assert "Offline Save Meta Title" in (panel.view_title.text() or "")
    assert "OfflineAuthor" in (panel.meta_author_line.text() or "")
    assert "离线未保存" in (panel.view_offline.text() or "")
    assert panel.current_mod_id() == mid

    # Simulate successful offline mutation (DB + disk), without leaving Detail.
    index = offline_dir / "index.html"
    index.write_text("<html><body>offline</body></html>", encoding="utf-8")
    db.update_mod_offline_status(mid, status=OFFLINE_STATUS_ARCHIVED)

    panel._on_offline_archive_finished(str(index))
    qapp.processEvents()

    assert panel.current_mod_id() == mid
    assert "Offline Save Meta Title" in (panel.view_title.text() or "")
    assert "OfflineAuthor" in (panel.meta_author_line.text() or "")
    assert "Keep this description after save." in (panel.meta_desc_line.text() or "")
    offline_text = panel.view_offline.text() or ""
    assert "已保存" in offline_text
    assert "离线未保存" not in offline_text


def test_path_only_show_mod_blanks_without_internal_id(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Guard: path-only reload is what caused blank metadata after mutation."""
    mid = "8802"
    folder = tmp_path / "library" / "Game" / "Named_Folder"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": mid,
                "title": "Should Not Vanish",
                "display_name": "Should Not Vanish",
                "author": "PathOnlyAuthor",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="Should Not Vanish", author="PathOnlyAuthor")
    )
    db.update_mod_identity_fields(
        mid, folder_present=True, last_known_path=str(folder)
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    assert "Should Not Vanish" in (panel.view_title.text() or "")

    panel.show_mod(folder)  # path only — resolver requires internal_id
    qapp.processEvents()
    assert "Should Not Vanish" not in (panel.view_title.text() or "")
    assert panel.current_mod_id() != mid

    panel._reload_current_detail_from_projection(folder, mod_id=mid)
    qapp.processEvents()
    assert "Should Not Vanish" in (panel.view_title.text() or "")
    assert "PathOnlyAuthor" in (panel.meta_author_line.text() or "")
    assert panel.current_mod_id() == mid
