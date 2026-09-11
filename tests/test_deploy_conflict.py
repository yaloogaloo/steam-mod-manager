"""Deploy integrates conflict preview + post-deploy check_all_mods."""

from __future__ import annotations

import json
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
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _prove_managed_folder(db: DatabaseManager, mid: str, folder: Path) -> None:
    """Stamp ``.info.internal_id`` so Deploy path resolve accepts the folder."""
    proof = str(mid)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    meta_path = info / METADATA_FILENAME
    payload: dict = {}
    if meta_path.is_file():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["internal_id"] = proof
    payload.setdefault("published_file_id", mid)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    db.update_mod_identity_fields(
        mid,
        internal_id=proof,
        last_known_path=str(folder),
        folder_present=True,
    )


def _seed_mod(library: Path, mod_id: str, db: DatabaseManager | None = None) -> Path:
    folder = library / "Game" / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "internal_id": "{mod_id}",\n'
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "M{mod_id}",\n'
        '  "app_id": 1,\n'
        '  "game_name": "Game"\n'
        "}\n",
        encoding="utf-8",
    )
    (folder / "payload.txt").write_text("x", encoding="utf-8")
    if db is not None:
        _prove_managed_folder(db, mod_id, folder)
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
    create_steam_test_mod(db, external_id="501", title="M501")
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
    create_steam_test_mod(db, external_id="601", title="A", app_id=1)
    create_steam_test_mod(db, external_id="602", title="B", app_id=1)
    _prove_managed_folder(db, "601", a)
    _prove_managed_folder(db, "602", b)

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
        internal_id="602",
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
