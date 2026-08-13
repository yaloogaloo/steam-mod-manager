"""Deploy → Undeploy regression — deployment state must stay isolated from content."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules.anno import ANNO_1800_APP_ID
from services.deploy_rules.base import StrategyResult
from services.deploy_rules import load_manifest
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    MISSING_CONTENT_METADATA_KEY,
    set_is_missing_content,
)
from services.library_status import (
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    row_content_status,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "undeploy_reg.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_meta(folder: Path, *, mod_id: str, app_id: int, game: str, **extra: object) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload = {
        "published_file_id": mod_id,
        "title": folder.name,
        "app_id": app_id,
        "game_name": game,
        **extra,
    }
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _setup_folder_copy_game(db: DatabaseManager, tmp_path: Path, *, app_id: int = 424242) -> Path:
    mods_root = tmp_path / "game" / "mods"
    mods_root.mkdir(parents=True)
    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(mods_root),
        deploy_type="folder_copy",
    )
    return mods_root


def _content_snapshot(db: DatabaseManager, mod_id: str) -> tuple[str, int]:
    row = db.get_mod_backup_row(mod_id) or {}
    return row_content_status(row), int(row.get("folder_present") or 0)


def test_normal_deploy_undeploy_cycle(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mod_id = "930001"
    folder = library / "Game" / "PayloadMod"
    folder.mkdir(parents=True)
    (folder / "mod.txt").write_text("data", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    mods_root = _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="PayloadMod",
            app_id=424242,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(mod_id).get("success") is True
    target = mods_root / "PayloadMod"
    assert target.is_dir()
    assert (db.get_mod_deploy_info(mod_id) or {}).deploy_status == DEPLOY_STATUS_DEPLOYED

    before_cs, before_fp = _content_snapshot(db, mod_id)
    assert dep.undeploy_mod(mod_id).get("success") is True
    assert not target.exists()
    info = db.get_mod_deploy_info(mod_id)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs == CONTENT_HEALTHY
    assert after_fp == before_fp == 1


def test_anno_1800_deploy_undeploy(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    game = "Anno 1800"
    mod_id = "9000000000009030"
    folder = library / game / "Golden Tickets"
    folder.mkdir(parents=True)
    archive = folder / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("my-mod/modinfo.json", '{"ModName":"Test"}')
        zf.writestr("my-mod/data/config/export/main/asset/assets.xml", "<A/>")
    _write_meta(folder, mod_id=mod_id, app_id=0, game=game, source_type="modio")

    install = tmp_path / "AnnoInstall"
    (install / "mods").mkdir(parents=True)
    db.update_game_deploy_config(
        ANNO_1800_APP_ID,
        name=game,
        install_path=str(install),
        mod_path="",
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="Golden Tickets",
            app_id=0,
            game_name=game,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    dep = ModDeployer(library_root=library, db=db)
    out = dep.deploy_mod(mod_id)
    assert out.get("success") is True, out
    assert out.get("deploy_type") == "anno_1800"
    deployed_files = list((install / "mods").rglob("*"))
    assert any(p.is_file() for p in deployed_files)

    before_cs, before_fp = _content_snapshot(db, mod_id)
    und = dep.undeploy_mod(mod_id)
    assert und.get("success") is True, und
    assert und.get("removed_files", 0) >= 1
    assert load_manifest(folder) is None
    assert (db.get_mod_deploy_info(mod_id) or {}).deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs == CONTENT_HEALTHY
    assert after_fp == before_fp == 1


def test_stale_missing_flag_deploy_then_undeploy(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "930002"
    folder = library / "Game" / "ZipMod"
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("inside/readme.txt", "ok")
    _write_meta(
        folder,
        mod_id=mod_id,
        app_id=424242,
        game="Game",
        **{MISSING_CONTENT_METADATA_KEY: True},
    )
    _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="ZipMod", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )
    set_is_missing_content(folder, True)

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(mod_id).get("success") is True
    before_cs, before_fp = _content_snapshot(db, mod_id)
    assert dep.undeploy_mod(mod_id).get("success") is True
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs == CONTENT_HEALTHY
    assert after_fp == before_fp == 1


def test_undeploy_after_source_removed_when_manifest_survives(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Source index may break after folder move/delete; last_known_path + manifest still undeploy."""
    library = tmp_path / "mod"
    mod_id = "930003"
    folder = library / "Game" / "GoneLater"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("1", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    mods_root = _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="GoneLater", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(mod_id).get("success") is True
    target = mods_root / "GoneLater"
    assert target.is_dir()

    # Simulate resolver index miss while folder still exists on disk
    with patch.object(
        dep.files,
        "find_by_published_id",
        return_value=None,
    ):
        assert dep.undeploy_mod(mod_id).get("success") is True
    assert not target.exists()


def test_source_deleted_marks_folder_missing_but_undeploy_clears_deployment(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """When source + manifest are gone, undeploy cannot remove targets safely."""
    library = tmp_path / "mod"
    mod_id = "930004"
    folder = library / "Game" / "Removed"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("1", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    mods_root = _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="Removed", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(mod_id).get("success") is True
    target = mods_root / "Removed"
    assert target.is_dir()

    shutil.rmtree(folder)
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_FOLDER_MISSING,
        folder_present=False,
        last_known_path=str(folder),
    )
    before_cs, before_fp = _content_snapshot(db, mod_id)
    assert before_cs == CONTENT_FOLDER_MISSING
    assert before_fp == 0

    out = dep.undeploy_mod(mod_id)
    assert out.get("success") is False
    # Target remains — no manifest to safely identify owned files
    assert target.is_dir()
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs
    assert after_fp == before_fp


def test_deploy_failure_does_not_change_content_status(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "930005"
    folder = library / "Game" / "FailDeploy"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("1", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="FailDeploy", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )
    before_cs, before_fp = _content_snapshot(db, mod_id)

    dep = ModDeployer(library_root=library, db=db)
    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.deploy",
        return_value=StrategyResult(
            success=False, error="Permission denied", deploy_type="folder_copy"
        ),
    ):
        out = dep.deploy_mod(mod_id)
    assert out.get("success") is False
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs == CONTENT_HEALTHY
    assert after_fp == before_fp == 1


def test_undeploy_failure_does_not_change_content_status(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "930006"
    folder = library / "Game" / "FailUndeploy"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("1", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    _setup_folder_copy_game(db, tmp_path)
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="FailUndeploy", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(mod_id).get("success") is True
    before_cs, before_fp = _content_snapshot(db, mod_id)

    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.undeploy",
        return_value=StrategyResult(
            success=False, error="Permission denied", deploy_type="folder_copy"
        ),
    ):
        out = dep.undeploy_mod(mod_id)
    assert out.get("success") is False
    after_cs, after_fp = _content_snapshot(db, mod_id)
    assert after_cs == before_cs == CONTENT_HEALTHY
    assert after_fp == before_fp == 1


@pytest.mark.skipif(
    not Path(r"E:\project\steam-mod-manager\mod\Anno 1800").is_dir(),
    reason="local Anno library not present",
)
def test_real_anno_mod_deploy_undeploy_cycle() -> None:
    from core.db_manager import get_db
    from core.paths import default_mod_library

    db = get_db()
    mid = db.find_mod_id_by_workspace_id("17866204912338831")
    if not mid:
        pytest.skip("target mod not in library")
    row = db.get_mod_backup_row(mid) or {}
    before_cs, before_fp = _content_snapshot(db, mid)

    dep = ModDeployer(library_root=default_mod_library(), db=db)
    dep_out = dep.deploy_mod(mid)
    assert dep_out.get("success") is True, dep_out

    und = dep.undeploy_mod(mid)
    assert und.get("success") is True, und
    assert (db.get_mod_deploy_info(mid) or {}).deploy_status == DEPLOY_STATUS_NOT_DEPLOYED

    after_cs, after_fp = _content_snapshot(db, mid)
    assert after_cs == before_cs
    assert after_fp == before_fp


def test_ui_apply_deploy_result_undeploy_shows_not_deployed(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from core.db_manager import DEPLOY_STATUS_DEPLOYED, DEPLOY_STATUS_NOT_DEPLOYED
    from ui.mod_detail_panel import ModDetailPanel

    if QApplication.instance() is None:
        QApplication([])

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    mod_id = "930007"
    folder = library / "Game" / "UiMod"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    db.update_game_deploy_config(424242, name="Game")
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="UiMod", app_id=424242)
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "game" / "mods" / "UiMod"),
        deploy_time="2026-01-01T00:00:00+00:00",
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    panel.set_deploy_busy(True, action="undeploy")
    db.update_mod_deploy_status(mod_id, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED)
    panel.apply_deploy_result(
        {
            "success": True,
            "mod_id": mod_id,
            "removed_files": 2,
            "deploy_type": "folder_copy",
        }
    )

    assert "未部署" in panel.view_deploy.text()
    assert not panel.btn_undeploy.isEnabled()
    assert panel.btn_deploy.isEnabled()


def test_ui_undeploy_enabled_when_source_folder_missing_but_deployed(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from core.db_manager import DEPLOY_STATUS_DEPLOYED
    from ui.mod_detail_panel import ModDetailPanel

    if QApplication.instance() is None:
        QApplication([])

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    mod_id = "930008"
    folder = library / "Game" / "Absent"
    folder.mkdir(parents=True)
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    db.update_game_deploy_config(424242, name="Game")
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="Absent", app_id=424242)
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "game" / "mods" / "Absent"),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    panel._folder_absent = True
    panel._set_deploy_buttons(DEPLOY_STATUS_DEPLOYED)

    assert not panel.btn_deploy.isEnabled()
    assert not panel.btn_redeploy.isEnabled()
    assert panel.btn_undeploy.isEnabled()
