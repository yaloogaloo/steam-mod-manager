"""Deploy failure state lifecycle — FAILED persists after rollback / projection."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.identity import bind_managed_path, create_steam_test_mod

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_apply import ApplyResult
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.deploy_thread import DeployWorker
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
    manager = DatabaseManager(tmp_path / "fail_lifecycle.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _setup_game(db: DatabaseManager, tmp_path: Path) -> Path:
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))
    return mods


def _prove_folder(db: DatabaseManager, mid: str, folder: Path) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": str(mid),
                "published_file_id": str(mid),
                "title": folder.name,
                "app_id": 100,
                "game_name": "SomeGame",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        mid,
        internal_id=str(mid),
        last_known_path=str(folder),
        folder_present=True,
    )


def _make_mod(library: Path, db: DatabaseManager, *, mid: str) -> Path:
    folder = library / "SomeGame" / f"Mod{mid}"
    folder.mkdir(parents=True)
    (folder / "file1.txt").write_text("NEW", encoding="utf-8")
    _prove_folder(db, mid, folder)
    create_steam_test_mod(db, external_id=mid, title=folder.name, app_id=100)
    bind_managed_path(db, mid, folder, title=folder.name)

    return folder


def test_case1_apply_fail_rollback_ok_persists_failed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Apply failure + successful rollback → DB status=FAILED with error."""
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="96001")

    prior = mods / mod_dir.name / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    def _fail_apply(plan, **kwargs):  # noqa: ANN001
        return ApplyResult(
            success=False,
            error="simulated apply failure",
            applied=0,
            failed_details=["file1.txt: boom"],
        )

    with patch("services.deploy_apply.apply_file_plan", side_effect=_fail_apply):
        out = ModDeployer(library_root=library, db=db).deploy_mod("96001")

    assert out["success"] is False
    assert "simulated apply failure" in str(out.get("error") or "")
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    info = db.get_mod_deploy_info("96001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert "simulated apply failure" in str(info.deploy_error or "")


def test_case2_projection_refresh_keeps_failed_ui(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """After FAILED + deploy_error, _fill_deploy_status_from_db still shows failure."""
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="96002")
    db.update_mod_deploy_status(
        "96002",
        deploy_status=DEPLOY_STATUS_FAILED,
        deploy_path="",
        deploy_time="",
        deploy_error="copy failed: disk full",
        app_id=100,
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id="96002", game_id=100)
    panel._fill_deploy_status_from_db()

    assert "部署失败" in panel.view_deploy.text()
    assert "disk full" in panel.view_deploy_error.text()


def test_case3_clean_not_deployed_shows_undepoyed(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """not_deployed with empty deploy_error → 未部署 (no residual failure UI)."""
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="96003")
    db.update_mod_deploy_status(
        "96003",
        deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
        deploy_path="",
        deploy_time="",
        deploy_error="",
        app_id=100,
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id="96003", game_id=100)
    panel._fill_deploy_status_from_db()

    assert "未部署" in panel.view_deploy.text()
    assert panel.view_deploy_error.text() == ""


def test_case3b_legacy_not_deployed_with_error_shows_failed(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """Legacy row: not_deployed + residual deploy_error still surfaces as failure."""
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="96004")
    db.update_mod_deploy_status(
        "96004",
        deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
        deploy_path="",
        deploy_time="",
        deploy_error="legacy residual error",
        app_id=100,
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id="96004", game_id=100)
    panel._fill_deploy_status_from_db()

    assert "部署失败" in panel.view_deploy.text()
    assert "legacy residual error" in panel.view_deploy_error.text()


def test_case4_early_gate_persists_failed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Early gate failure (disabled mod) writes FAILED + deploy_error."""
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    _make_mod(library, db, mid="96005")
    db.disable_mod("96005")
    assert not db.is_mod_enabled("96005")

    out = ModDeployer(library_root=library, db=db).deploy_mod("96005")
    assert out["success"] is False
    assert "disabled" in str(out.get("error") or "").lower()
    info = db.get_mod_deploy_info("96005")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


def test_worker_failure_emits_only_deploy_failed(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """FAILED must not dual-emit deploy_finished + deploy_failed."""
    from PySide6.QtCore import QCoreApplication

    library = tmp_path / "library"
    library.mkdir()
    finished: list[object] = []
    failed: list[object] = []

    class _BoomDeployer:
        def deploy_mod(self, mid):  # noqa: ANN001
            return {"success": False, "error": "boom", "mod_id": str(mid)}

    worker = DeployWorker("96006", library_root=library, deployer=_BoomDeployer())  # type: ignore[arg-type]
    worker.deploy_finished.connect(finished.append)
    worker.deploy_failed.connect(failed.append)
    worker.start()
    assert worker.wait(10_000)
    QCoreApplication.processEvents()

    assert finished == []
    assert len(failed) == 1
    assert isinstance(failed[0], dict)
    assert failed[0].get("success") is False
    assert "boom" in str(failed[0].get("error") or "")
