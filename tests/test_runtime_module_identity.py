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
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
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


def _make_managed_mod(library: Path, *, mod_id: str, app_id: int) -> Path:
    mod_dir = library / "Game" / "RuntimeMod"
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "mod.txt").write_text("payload", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "RuntimeMod",\n'
        f'  "app_id": {app_id},\n'
        '  "game_name": "Game"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


def test_deploy_success_clears_deploy_error(
    tmp_path: Path, db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful deploy must set deployed status and clear stale deploy_error."""
    library = tmp_path / "mod"
    game_mods = tmp_path / "GameMods"
    game_mods.mkdir(parents=True)
    mod_id = "99001"
    app_id = 424242

    _make_managed_mod(library, mod_id=mod_id, app_id=app_id)
    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=str(tmp_path / "GameInstall"),
        mod_path=str(game_mods),
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="RuntimeMod",
            app_id=app_id,
            game_name="Game",
        )
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=DEPLOY_STATUS_FAILED,
        deploy_error="部署失败: 缺少 RAR 解压组件 (unrar)",
        deploy_path="",
        deploy_time="",
    )

    caplog.set_level(logging.INFO)
    result = ModDeployer(library_root=library, db=db).deploy_mod(mod_id)

    assert result["success"] is True
    info = db.get_mod_deploy_info(mod_id)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_error == ""
    assert "[DEPLOY_RUNTIME] archive_module_path=" in caplog.text
    assert "[DEPLOY_RUNTIME] archive_module_mtime=" in caplog.text
