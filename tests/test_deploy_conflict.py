"""Deploy integrates conflict preview + post-deploy check_all_mods."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.mod_status import CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.conflict import ConflictDetector
from services.deploy import ModDeployer
from services.deploy_rules.base import DeployContext, StrategyResult
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "deploy_conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    workshop_id: str,
    title: str | None = None,
) -> tuple[Path, str]:
    created = create_steam_test_mod(
        db, external_id=workshop_id, title=title or f"M{workshop_id}", app_id=1
    )
    pk = str(created.mod_id)
    folder = library / "Game" / workshop_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "payload.txt").write_text("x", encoding="utf-8")
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title=title or f"M{workshop_id}",
        app_id=1,
        game_name="Game",
    )
    return folder, pk


def test_check_conflict_preview_reports_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "dest" / "same.pak").resolve())
    db.update_game_deploy_config(
        1, name="Game", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    a, pk_a = _seed_mod(library, db, workshop_id="501")
    save_manifest(
        a,
        DeployManifest(
            mod_id=pk_a,
            deploy_time="t",
            deploy_type="folder_copy",
            files=[ManifestFileEntry(source="payload.txt", target=shared)],
        ),
    )
    deployer = ModDeployer(library_root=library, db=db)
    preview = deployer.check_conflict_preview("502", [shared])
    assert preview is not None
    assert preview["overwrite"] is True
    assert preview["conflict"] is False
    assert preview["status"] == "none"
    assert preview["files"][0]["existing_mod"] == pk_a
    assert preview["conflicts"][0]["type"] == "FILE_OVERWRITE"


def test_preview_none_when_free(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(
        1, name="Game", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    _seed_mod(library, db, workshop_id="701")
    deployer = ModDeployer(library_root=library, db=db)
    free = str((tmp_path / "dest" / "free.pak").resolve())
    assert deployer.check_conflict_preview("701", [free]) is None


def test_post_deploy_runs_check_all(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    library = tmp_path / "mod"
    dest = tmp_path / "game_root"
    dest.mkdir()
    target = dest / "overlap.pak"
    target.write_text("a", encoding="utf-8")
    target_s = str(target.resolve())

    db.update_game_deploy_config(
        1,
        name="Game",
        install_path=str(dest),
        mod_path=str(dest),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    a, pk_a = _seed_mod(library, db, workshop_id="601", title="A")
    b, pk_b = _seed_mod(library, db, workshop_id="602", title="B")
    save_manifest(
        a,
        DeployManifest(
            mod_id=pk_a,
            deploy_time="t",
            deploy_type=DEPLOY_TYPE_FOLDER_COPY,
            files=[
                ManifestFileEntry(source=str(a / "payload.txt"), target=target_s)
            ],
        ),
    )

    called = {"ok": False}
    real_check = ConflictDetector.check_all_mods

    def _wrap(self, *args, **kwargs):
        called["ok"] = True
        return real_check(self, *args, **kwargs)

    monkeypatch.setattr(ConflictDetector, "check_all_mods", _wrap)

    def _sync_conflict_scan(
        library_root: Path,
        *,
        db: DatabaseManager | None = None,
        log_prefix: str = "",
    ) -> None:
        ConflictDetector(library_root, db=db).check_all_mods(persist=True)

    monkeypatch.setattr(
        "services.deploy._schedule_post_deploy_conflict_scan",
        _sync_conflict_scan,
    )

    man = DeployManifest(
        mod_id=pk_b,
        deploy_time="t2",
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        files=[
            ManifestFileEntry(source=str(b / "payload.txt"), target=target_s)
        ],
    )
    plan = StrategyResult(
        success=True,
        files=list(man.files),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    result = StrategyResult(
        success=True,
        target=str(dest),
        copied_files=1,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        deploy_time="t2",
        files=list(man.files),
        manifest=man,
    )

    class FakeStrategy:
        def plan(self, ctx):
            return plan

        def deploy(self, ctx):
            save_manifest(b, man)
            return result

        def undeploy(self, ctx, manifest):
            return result

    monkeypatch.setattr(
        "services.deploy.resolve_strategy", lambda ctx: FakeStrategy()
    )
    monkeypatch.setattr(
        "services.deploy.get_strategy", lambda *a, **k: FakeStrategy()
    )

    cfg = db.get_game_deploy_config(1)
    assert cfg is not None

    ctx = DeployContext(
        internal_id=pk_b,
        app_id=1,
        source=b,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        config=cfg,
        allowed_rel_paths=None,
        managed_path=b,
        custom_deploy_path="",
    )

    monkeypatch.setattr(
        ModDeployer,
        "_resolve_context",
        lambda self, mid, **kwargs: (ctx, None, None),
    )

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.deploy_mod(pk_b)
    assert out.get("success") is True
    assert called["ok"] is True
    assert db.get_mod_status(pk_b).conflict_status == "none"
    assert db.get_mod_status(pk_a).conflict_status == "none"
