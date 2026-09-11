"""Redeploy / double-deploy semantics — lock + manifest consistency."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from services.deploy import ModDeployer
from services.deploy_rules.manifest import load_manifest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "redeploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(
    db: DatabaseManager, library: Path, mods: Path, *, mid: str = "88001"
) -> Path:
    folder = library / "Game" / "IdemMod"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("payload", encoding="utf-8")
    mods.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(
        db, external_id=mid, title="IdemMod", app_id=1, game_name="Game"
    )
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title="IdemMod",
        external_id=mid,
        workspace_id=str(created.workspace_id or mid),
        app_id=1,
        game_name="Game",
    )
    bind_managed_path(db, created.mod_id, folder, title="IdemMod", game_name="Game")
    return folder


def test_redeploy_twice_idempotent(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "game" / "Mods"
    db.update_game_deploy_config(
        1,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    _seed(db, library, mods)

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod("88001")
    assert first.get("success") is True
    target = Path(first["target"])
    assert (target / "a.txt").read_text(encoding="utf-8") == "payload"
    man1 = load_manifest(library / "Game" / "IdemMod")
    assert man1 is not None
    files1 = len(man1.files)

    second = deployer.deploy_mod("88001")
    assert second.get("success") is True
    assert (target / "a.txt").read_text(encoding="utf-8") == "payload"
    man2 = load_manifest(library / "Game" / "IdemMod")
    assert man2 is not None
    assert len(man2.files) == files1


def test_concurrent_second_deploy_rejected(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "game" / "Mods"
    db.update_game_deploy_config(
        1,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    _seed(db, library, mods, mid="88002")

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
        results.append(deployer.deploy_mod("88002"))

    t = threading.Thread(target=_first)
    t.start()
    assert gate.wait(timeout=3)
    blocked = deployer.deploy_mod("88002")
    proceed.set()
    t.join(timeout=10)

    assert blocked.get("success") is False
    assert blocked.get("error_code") == "deploy_in_progress"
    assert len(results) == 1
