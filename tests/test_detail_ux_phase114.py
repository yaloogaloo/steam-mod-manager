"""Phase 11.4 — Detail refresh sync, op feedback, dependency, import close."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from core.db_manager import RELATIONSHIP_DEPENDENCY, DatabaseManager
from core.models import ModMetadata
from services.dir_size import (
    directory_size,
    invalidate_directory_size,
    reset_directory_size_cache,
)
from services.file_ops import INFO_DIR_NAME, apply_missing_content_marker
from services.importers.importer_base import ImportResult
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.mod_refresh import refresh_mod
from ui.mod_card import ModCardWidget
from ui.mod_detail_panel import ModDetailPanel
from ui.mod_import_dialog import ModImportDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "phase114.db")
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(
    lib: Path, mid: str = "9000000000001141", *, payload: bool = False
) -> Path:
    folder = lib / "Game" / f"Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": f"Mod_{mid}",
                "display_name": f"Mod_{mid}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if payload:
        (folder / "mod.dll").write_bytes(b"dll")
        (folder / "config.json").write_text("{}", encoding="utf-8")
    return folder


def test_detail_refresh_heals_content_missing_and_invalidates_size(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    mid = "9000000000001141"
    folder = _mod_folder(lib, mid, payload=False)
    apply_missing_content_marker(folder)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Mod_{mid}"))
    db.update_mod_identity_fields(
        mid,
        content_status=CONTENT_CONTENT_MISSING,
        library_status="content_missing",
        folder_present=True,
        last_known_path=str(folder),
    )
    db.set_official_metadata_synced(mid, True)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    assert not hasattr(panel, "content_status_badge")

    reset_directory_size_cache()
    _ = directory_size(folder)
    (folder / "mod.dll").write_bytes(b"x" * 50)
    (folder / "config.json").write_text("{}", encoding="utf-8")

    result = refresh_mod(mid, folder, platform="other", library_root=lib, db=db)
    assert result.success
    assert result.local is not None
    assert result.local.content_status == CONTENT_HEALTHY
    compat = result.to_metadata_refresh_result()
    assert compat.managed_path == folder

    invalidate_directory_size(folder)
    panel._on_metadata_refresh_finished(compat)
    qapp.processEvents()
    assert not hasattr(panel, "content_status_badge")
    joined = " ".join(
        (panel.header_platform_badge.text() or "", panel.size_badge.text() or "")
    )
    assert "✓" not in joined
    assert "正常" not in joined
    assert directory_size(folder) >= 50
    assert "刷新完成" in (panel.op_status_label.text() or "")


def test_card_badge_updates_after_stale_clear(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    mid = "9000000000001142"
    folder = _mod_folder(lib, mid, payload=True)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Mod_{mid}"))
    db.update_mod_identity_fields(
        mid,
        content_status=CONTENT_HEALTHY,
        library_status="healthy",
        folder_present=True,
        last_known_path=str(folder),
    )

    class _Stale:
        folder_absent = False
        missing_content = True
        content_status = CONTENT_CONTENT_MISSING
        library_status = "content_missing"
        cover = ""
        source_type = "other"
        steam_name = ""
        display_name = ""
        favorite = False
        offline_status = ""
        deploy_status = ""
        game_status = ""
        conflict = False
        invalid = False
        enabled = True

    meta = ModMetadata(
        published_file_id=mid, title=f"Mod_{mid}", managed_path=str(folder)
    )
    card = ModCardWidget(folder, meta)
    card.refresh_display()
    qapp.processEvents()
    card._card_data = _Stale()
    card._render_missing_content_badge()
    qapp.processEvents()
    assert "文件缺失" in (card.missing_badge.text() or "")

    card._card_data = None
    card._render_missing_content_badge()
    qapp.processEvents()
    assert card.missing_badge.isHidden()
    assert "✓" not in (card.missing_badge.text() or "")
    assert "正常" not in (card.missing_badge.text() or "")


def test_deploy_busy_disables_and_relabels(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    mid = "9000000000001143"
    folder = _mod_folder(lib, mid, payload=True)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Mod_{mid}"))
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()

    panel.set_deploy_busy(True, action="deploy")
    assert panel.btn_deploy.text() == "部署中…"
    assert not panel.btn_deploy.isEnabled()
    assert not panel.btn_redeploy.isEnabled()
    assert "正在部署" in (panel.op_status_label.text() or "")

    panel.apply_deploy_result(
        {
            "success": True,
            "mod_id": mid,
            "target": str(tmp_path / "game"),
            "deploy_type": "copy",
            "deploy_time": "2026-01-01",
        }
    )
    assert "部署完成" in (panel.op_status_label.text() or "")
    assert panel.btn_deploy.text() == "部署"


def test_dependency_block_compact_copy(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    mid = "9000000000001144"
    dep = "9000000000001678"
    folder = _mod_folder(lib, mid, payload=True)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Mod_{mid}"))
    db.upsert_mod(
        ModMetadata(published_file_id=dep, title="Lustiest Lair Expanded v1.8")
    )
    db.add_mod_relationship(mid, dep, RELATIONSHIP_DEPENDENCY)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    assert panel.btn_add_dependency.text() == "+ 添加依赖"
    text = panel.dep_summary_label.text() or ""
    assert "Lustiest Lair Expanded v1.8" in text
    assert "ID " not in text
    assert "依赖于\n" not in text


def test_import_success_accepts_dialog(
    qapp: QApplication, tmp_path: Path, monkeypatch
) -> None:
    dlg = ModImportDialog(library_root=tmp_path / "lib")
    accepted = {"ok": False}

    def _mark_accept(self: ModImportDialog) -> None:
        accepted["ok"] = True
        QDialog.accept(self)

    monkeypatch.setattr(ModImportDialog, "accept", _mark_accept)
    dlg._on_import_ok(
        ImportResult(
            success=True,
            mod_id="1",
            title="Demo",
            managed_path=str(tmp_path / "x"),
            imported_count=1,
        )
    )
    assert accepted["ok"] is True
