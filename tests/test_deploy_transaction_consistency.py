"""Deploy transaction / rollback / DB consistency lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.backup_manager import (
    BACKUPS_DIRNAME,
    TRANSACTION_FILENAME,
    TXN_BACKUP_DONE,
    TXN_FAILED,
    TXN_PREPARED,
    BackupIntegrityError,
    BackupManager,
    BackupRestoreError,
    transaction_path_for,
)
from services.deploy import ModDeployer
from services.deploy_rules.base import StrategyResult
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_HEALTHY


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "txn_consistency.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(
    library: Path,
    *,
    mid: str = "95001",
    folder: str = "TxnMod",
    app_id: int = 424242,
) -> Path:
    mod_dir = library / "Game" / folder
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "file1.txt").write_text("NEW", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{folder}",\n'
        f'  "app_id": {app_id},\n'
        '  "game_name": "Game"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


def _setup_game(db: DatabaseManager, tmp_path: Path, *, app_id: int = 424242) -> Path:
    install = tmp_path / "fake_game"
    mods = install / "mods"
    mods.mkdir(parents=True)
    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    return mods


def _register(db: DatabaseManager, *, mid: str, path: str, app_id: int = 424242) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title="TxnMod",
            app_id=app_id,
            game_name="Game",
        )
    )
    db.update_mod_identity_fields(
        mid,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=path,
        library_status=CONTENT_HEALTHY,
    )


def _backup_files(managed: Path) -> list[Path]:
    root = managed / INFO_DIR_NAME / BACKUPS_DIRNAME
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_file()]


# ---------------------------------------------------------------------------
# Case 1 — deploy fail, rollback ok → clean slate
# ---------------------------------------------------------------------------


def test_case1_deploy_fail_rollback_ok_cleans_state(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mid="95001")
    _register(db, mid="95001", path=str(mod_dir))

    prior = mods / "TxnMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    def _fail_deploy(self: FolderCopyStrategy, ctx: object) -> StrategyResult:
        return StrategyResult(
            success=False,
            error="simulated strategy failure",
            deploy_type="folder_copy",
        )

    with patch.object(FolderCopyStrategy, "deploy", _fail_deploy):
        out = ModDeployer(library_root=library, db=db).deploy_mod("95001")

    assert out["success"] is False
    assert load_manifest(mod_dir) is None
    assert _backup_files(mod_dir) == []
    assert not transaction_path_for(mod_dir).exists()
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    info = db.get_mod_deploy_info("95001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED


# ---------------------------------------------------------------------------
# Case 2 — deploy fail, rollback fail → keep recovery data
# ---------------------------------------------------------------------------


def test_case2_deploy_fail_rollback_fail_keeps_recovery(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mid="95002", folder="KeepMod")
    _register(db, mid="95002", path=str(mod_dir))

    prior = mods / "KeepMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    # First successful deploy establishes manifest + backups.
    first = ModDeployer(library_root=library, db=db).deploy_mod("95002")
    assert first["success"] is True
    assert load_manifest(mod_dir) is not None
    backups_after_first = _backup_files(mod_dir)
    assert backups_after_first

    def _fail_deploy(self: FolderCopyStrategy, ctx: object) -> StrategyResult:
        return StrategyResult(
            success=False,
            error="second deploy failed",
            deploy_type="folder_copy",
        )

    with patch.object(FolderCopyStrategy, "deploy", _fail_deploy), patch.object(
        BackupManager,
        "restore_one",
        side_effect=BackupIntegrityError("simulated restore failure"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod("95002")

    assert out["success"] is False
    # Recovery data must survive.
    assert load_manifest(mod_dir) is not None
    assert _backup_files(mod_dir), "backups must remain after rollback failure"
    txn_path = transaction_path_for(mod_dir)
    assert txn_path.is_file()
    txn = BackupManager(mod_dir).load_transaction()
    assert txn is not None
    assert str(txn.get("status") or "") == TXN_FAILED
    info = db.get_mod_deploy_info("95002")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED


# ---------------------------------------------------------------------------
# Case 3 — filesystem ok, DB update fails → success + warning
# ---------------------------------------------------------------------------


def test_case3_db_update_fail_still_success_with_warning(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mid="95003", folder="DbWarn")
    _register(db, mid="95003", path=str(mod_dir))

    real_update = db.update_mod_deploy_status

    def _update_fail(mod_id, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("deploy_status") == DEPLOY_STATUS_DEPLOYED:
            raise RuntimeError("simulated db failure")
        return real_update(mod_id, **kwargs)

    with patch.object(db, "update_mod_deploy_status", side_effect=_update_fail):
        out = ModDeployer(library_root=library, db=db).deploy_mod("95003")

    assert out["success"] is True
    assert out.get("warning") == "database_update_failed"
    assert (mods / "DbWarn" / "file1.txt").read_text(encoding="utf-8") == "NEW"
    assert load_manifest(mod_dir) is not None


# ---------------------------------------------------------------------------
# Case 4 — prepare_overwrite backup error → not stuck in prepared
# ---------------------------------------------------------------------------


def test_case4_prepare_overwrite_error_marks_txn_failed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mid="95004", folder="PrepFail")
    _register(db, mid="95004", path=str(mod_dir))

    prior = mods / "PrepFail" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    with patch.object(
        BackupManager,
        "_backup_one",
        side_effect=BackupIntegrityError("backup write broken"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod("95004")

    assert out["success"] is False
    txn = BackupManager(mod_dir).load_transaction()
    assert txn is not None
    status = str(txn.get("status") or "")
    assert status == TXN_FAILED
    assert status not in (TXN_PREPARED, TXN_BACKUP_DONE)
    info = db.get_mod_deploy_info("95004")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED


# ---------------------------------------------------------------------------
# Case 5 — redeploy reuses backup; undeploy restores original
# ---------------------------------------------------------------------------


def test_case5_redeploy_reuses_backup_undeploy_restores(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mid="95005", folder="ReuseMod")
    _register(db, mid="95005", path=str(mod_dir))

    prior = mods / "ReuseMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("95005")["success"] is True
    man1 = load_manifest(mod_dir)
    assert man1 is not None
    assert man1.files and man1.files[0].backup is not None
    backup_path_1 = man1.files[0].backup.path
    assert prior.read_text(encoding="utf-8") == "NEW"

    # Change source and deploy again (overwrite without undeploy).
    (mod_dir / "file1.txt").write_text("NEWER", encoding="utf-8")
    assert deployer.deploy_mod("95005")["success"] is True
    man2 = load_manifest(mod_dir)
    assert man2 is not None
    assert man2.files and man2.files[0].backup is not None
    # Must reuse original game-file backup, not overwrite with Mod payload.
    assert man2.files[0].backup.path == backup_path_1
    assert man2.files[0].backup.hash == man1.files[0].backup.hash
    assert prior.read_text(encoding="utf-8") == "NEWER"

    und = deployer.undeploy_mod("95005")
    assert und["success"] is True
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
