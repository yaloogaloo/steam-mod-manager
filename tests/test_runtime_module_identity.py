"""Runtime module identity and deploy success state cleanup."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DatabaseManager,
)
from services.deploy import ModDeployer
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from services.runtime_identity import (
    get_archive_module_identity,
    log_archive_runtime_identity,
)


def test_runtime_module_identity() -> None:
    ident = get_archive_module_identity()
    path = Path(ident.path)
    assert path.name == "archive.py"
    assert path.is_file()
    normalized = ident.path.replace("\\", "/")
    assert normalized.endswith("services/importers/archive.py")
    assert ident.mtime != "unknown"
    datetime.fromisoformat(ident.mtime)


def test_log_archive_runtime_identity(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    ident = log_archive_runtime_identity(
        logging.getLogger("test.runtime"), prefix="[RUNTIME]"
    )
    assert f"[RUNTIME] archive_module_path={ident.path}" in caplog.text
    assert f"[RUNTIME] archive_module_mtime={ident.mtime}" in caplog.text


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "runtime_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_managed_mod(
    db: DatabaseManager, library: Path, *, mod_id: str, app_id: int
) -> tuple[Path, str]:
    mod_dir = library / "Game" / "RuntimeMod"
    mod_dir.mkdir(parents=True)
    (mod_dir / "mod.txt").write_text("payload", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mod_id, title="RuntimeMod", app_id=app_id, game_name="Game"
    )
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title="RuntimeMod",
        app_id=app_id,
        game_name="Game",
    )
    return mod_dir, pk


def test_deploy_success_clears_deploy_error(
    tmp_path: Path, db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful deploy must set deployed status and clear stale deploy_error."""
    library = tmp_path / "mod"
    game_mods = tmp_path / "GameMods"
    game_mods.mkdir(parents=True)
    workshop = "99001"
    app_id = 424242

    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=str(tmp_path / "GameInstall"),
        mod_path=str(game_mods),
    )
    _mod_dir, pk = _make_managed_mod(db, library, mod_id=workshop, app_id=app_id)
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_FAILED,
        deploy_error="部署失败: 缺少 RAR 解压组件 (unrar)",
        deploy_path="",
        deploy_time="",
    )

    caplog.set_level(logging.INFO)
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert result["success"] is True
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_error == ""
    assert "[DEPLOY_RUNTIME] archive_module_path=" in caplog.text
    assert "[DEPLOY_RUNTIME] archive_module_mtime=" in caplog.text
