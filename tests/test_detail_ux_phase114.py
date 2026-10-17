"""Phase 11.4 — Detail refresh sync, op feedback, dependency, import close."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QWidget

from core.db_manager import RELATIONSHIP_DEPENDENCY, DatabaseManager
from core.models import ModMetadata
from core.mod_platform import PLATFORM_NEXUS
from services.identity_service import create_mod_identity
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


def _seed_steam_mod(
    db: DatabaseManager,
    lib: Path,
    *,
    external_id: str,
    payload: bool = False,
) -> tuple[str, Path]:
    title = f"Mod_{external_id}"
    created = create_steam_test_mod(db, external_id=external_id, title=title)
    internal_id = str(created.mod_id)
    folder = lib / "Game" / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=external_id,
        workspace_id=str(created.workspace_id or external_id),
        game_name="Game",
    )
    if payload:
        (folder / "mod.dll").write_bytes(b"dll")
        (folder / "config.json").write_text("{}", encoding="utf-8")
    bind_managed_path(db, internal_id, folder, title=title)
    return internal_id, folder


def test_detail_refresh_heals_content_missing_and_invalidates_size(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    mid, folder = _seed_steam_mod(db, lib, external_id="1141001", payload=False)
    apply_missing_content_marker(folder)

    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder),
    )
    db.update_mod_content_status(
        mid,
        content_status=CONTENT_CONTENT_MISSING,
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
    mid, folder = _seed_steam_mod(db, lib, external_id="1141002", payload=True)

    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder),
    )
    db.update_mod_content_status(
        mid,
        content_status=CONTENT_HEALTHY,
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
        published_file_id=mid,
        internal_id=mid,
        title=f"Mod_{mid}",
        managed_path=str(folder),
    )
    card = ModCardWidget(folder, meta)
    card.refresh_display()
    qapp.processEvents()
    card._card_data = _Stale()
    card._render_missing_content_badge()
    qapp.processEvents()
    assert "内容缺失" in (card.missing_badge.text() or "") or "文件缺失" in (
        card.missing_badge.text() or ""
    )

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
    mid, folder = _seed_steam_mod(db, lib, external_id="1141003", payload=True)

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
    main = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1144001",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1144001",
        title="Mod_main",
        app_id=413150,
        game_name="Stardew",
        operation="import",
    )
    dep = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1144002",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1144002",
        title="Lustiest Lair Expanded v1.8",
        app_id=413150,
        game_name="Stardew",
        operation="import",
    )
    mid = str(main.mod_id)
    folder = lib / "Game" / f"Mod_{mid}"
    folder.mkdir(parents=True)
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    (folder / INFO_DIR_NAME / "metadata.json").write_text(
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
    (folder / "mod.dll").write_bytes(b"dll")
    write_info_sidecar(
        folder,
        internal_id=mid,
        title="Mod_main",
        external_id="1144001",
        workspace_id=str(main.workspace_id or "1144001"),
        game_name="Stardew",
        platform=PLATFORM_NEXUS,
    )
    bind_managed_path(db, mid, folder, title="Mod_main")
    db.add_mod_relationship(mid, str(dep.mod_id), RELATIONSHIP_DEPENDENCY)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    assert panel.btn_add_dependency.text() == "+ 添加依赖"
    from ui.dependency_item_widget import DependencyItem

    items = panel.dep_list_host.findChildren(DependencyItem)
    assert len(items) == 1
    assert items[0].name_text() == "Lustiest Lair Expanded v1.8"
    assert "ID " not in items[0].name_text()
    assert "依赖于\n" not in (panel.dep_summary_label.text() or "")


def test_import_success_accepts_dialog(
    qapp: QApplication, tmp_path: Path, monkeypatch
) -> None:
    parent = QWidget()
    dlg = ModImportDialog(library_root=tmp_path / "lib", parent=parent)
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
