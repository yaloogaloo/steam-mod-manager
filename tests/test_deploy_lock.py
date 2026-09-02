"""Deploy concurrency lock — reject overlapping deploy/undeploy for same mod."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_lock import deploy_operation_lock
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_lock.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(library: Path, mod_id: str = "9001") -> Path:
    folder = library / "Game" / mod_id
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "data.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        '  "title": "LockTest",\n'
        '  "app_id": 1,\n'
        '  "game_name": "Game"\n'
        "}\n",
        encoding="utf-8",
    )
    return folder


def test_deploy_operation_lock_rejects_concurrent() -> None:
    entered = threading.Event()
    release = threading.Event()
    errors: list[str] = []

    def _holder() -> None:
        with deploy_operation_lock("9001", app_id=1):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=_holder)
    t.start()
    assert entered.wait(timeout=2)

    with pytest.raises(RuntimeError, match="已有部署任务"):
        with deploy_operation_lock("9001", app_id=1):
            pass

    release.set()
    t.join(timeout=2)


def test_deploy_mod_rejects_second_inflight(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _seed_mod(library)
    db.update_game_deploy_config(
        1,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(tmp_path / "mods"),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    (tmp_path / "mods").mkdir(parents=True)
    db.upsert_mod(ModMetadata(published_file_id="9001", title="LockTest", app_id=1))

    gate = threading.Event()
    proceed = threading.Event()

    class SlowStrategy:
        deploy_type = DEPLOY_TYPE_FOLDER_COPY

        def plan(self, ctx):
            from services.deploy_rules.base import StrategyResult

            gate.set()
            proceed.wait(timeout=5)
            return StrategyResult(success=False, error="slow-abort")

        def deploy(self, ctx):
            from services.deploy_rules.base import StrategyResult

            return StrategyResult(success=False, error="slow-abort")

        def undeploy(self, ctx, manifest):
            from services.deploy_rules.base import StrategyResult

            return StrategyResult(success=False, error="slow-abort")

    monkeypatch.setattr("services.deploy.resolve_strategy", lambda ctx: SlowStrategy())

    deployer = ModDeployer(library_root=library, db=db)
    results: list[dict] = []

    def _first() -> None:
        results.append(deployer.deploy_mod("9001"))

    t = threading.Thread(target=_first)
    t.start()
    assert gate.wait(timeout=3)

    second = deployer.deploy_mod("9001")
    proceed.set()
    t.join(timeout=3)

    assert second.get("success") is False
    assert second.get("error_code") == "deploy_in_progress"
    assert len(results) == 1
