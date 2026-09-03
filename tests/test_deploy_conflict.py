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
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(library: Path, mod_id: str) -> Path:
    folder = library / "Game" / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "M{mod_id}",\n'
        '  "app_id": 1,\n'
        '  "game_name": "Game"\n'
        "}\n",
        encoding="utf-8",
    )
    (folder / "payload.txt").write_text("x", encoding="utf-8")
    return folder


def test_check_conflict_preview_reports_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "dest" / "same.pak").resolve())
    a = _seed_mod(library, "501")
    save_manifest(
        a,
        DeployManifest(
            mod_id="501",
            deploy_time="t",
            deploy_type="folder_copy",
            files=[ManifestFileEntry(source="payload.txt", target=shared)],
        ),
    )
    db.upsert_mod(ModMetadata(published_file_id="501", title="M501"))
    deployer = ModDeployer(library_root=library, db=db)
    preview = deployer.check_conflict_preview("502", [shared])
    assert preview is not None
    assert preview["overwrite"] is True
    assert preview["conflict"] is False
    assert preview["status"] == "none"
    assert preview["files"][0]["existing_mod"] == "501"
    assert preview["conflicts"][0]["type"] == "FILE_OVERWRITE"


def test_preview_none_when_free(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    _seed_mod(library, "701")
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

    a = _seed_mod(library, "601")
    b = _seed_mod(library, "602")
    save_manifest(
        a,
        DeployManifest(
            mod_id="601",
            deploy_time="t",
            deploy_type=DEPLOY_TYPE_FOLDER_COPY,
            files=[
                ManifestFileEntry(source=str(a / "payload.txt"), target=target_s)
            ],
        ),
    )
    db.update_game_deploy_config(
        1,
        name="Game",
        install_path=str(dest),
        mod_path=str(dest),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(ModMetadata(published_file_id="601", title="A", app_id=1))
    db.upsert_mod(ModMetadata(published_file_id="602", title="B", app_id=1))

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
        mod_id="602",
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
            # Persist B's overlapping manifest as a real deploy would
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
        mod_id="602",
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
    out = deployer.deploy_mod(602)
    assert out.get("success") is True
    assert called["ok"] is True
    assert db.get_mod_status(602).conflict_status == "none"
    assert db.get_mod_status(601).conflict_status == "none"
