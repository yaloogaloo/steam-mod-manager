"""Deploy transaction / rollback / DB consistency lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DatabaseManager,
)
from services.backup_manager import (
    TRANSACTION_FILENAME,
    TXN_BACKUP_DONE,
    TXN_FAILED,
    TXN_PREPARED,
    BackupIntegrityError,
    BackupManager,
    transaction_path_for,
)
from services.deploy import ModDeployer
from services.deploy_rules.manifest import load_manifest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "txn_consistency.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(
    library: Path,
    db: DatabaseManager,
    *,
    mid: str = "95001",
    folder: str = "TxnMod",
    app_id: int = 424242,
) -> tuple[Path, str]:
    mod_dir = library / "Game" / folder
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "file1.txt").write_text("NEW", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mid, title=folder, app_id=app_id, game_name="Game"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod_dir, handle=pk, title=folder, app_id=app_id, game_name="Game"
    )
    return mod_dir, pk


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


def _backup_files(managed: Path) -> list[Path]:
    return BackupManager(managed).listed_backup_files()


def test_case1_deploy_fail_rollback_ok_cleans_state(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mid="95001")

    prior = mods / "TxnMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    from services.deploy_apply import ApplyResult

    def _fail_apply(file_plan: object, **kwargs: object) -> ApplyResult:
        return ApplyResult(success=False, error="simulated strategy failure")

    with patch("services.deploy_apply.apply_file_plan", _fail_apply):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert load_manifest(mod_dir) is None
    assert _backup_files(mod_dir) == []
    assert not transaction_path_for(mod_dir).exists()
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


def test_case2_deploy_fail_rollback_fail_keeps_recovery(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mid="95002", folder="KeepMod")

    prior = mods / "KeepMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    # First successful deploy establishes manifest + backups.
    first = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert first["success"] is True
    assert load_manifest(mod_dir) is not None
    backups_after_first = _backup_files(mod_dir)
    assert backups_after_first

    from services.deploy_apply import ApplyResult

    def _fail_apply(file_plan: object, **kwargs: object) -> ApplyResult:
        return ApplyResult(success=False, error="second deploy failed")

    with patch("services.deploy_apply.apply_file_plan", _fail_apply), patch.object(
        BackupManager,
        "restore_one",
        side_effect=BackupIntegrityError("simulated restore failure"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    # Recovery data must survive.
    assert load_manifest(mod_dir) is not None
    assert _backup_files(mod_dir), "backups must remain after rollback failure"
    txn_path = transaction_path_for(mod_dir)
    assert txn_path.is_file()
    txn = BackupManager(mod_dir).load_transaction()
    assert txn is not None
    assert str(txn.get("status") or "") == TXN_FAILED
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED


def test_case3_db_update_fail_still_success_with_warning(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mid="95003", folder="DbWarn")

    real_update = db.update_mod_deploy_status

    def _update_fail(mod_id, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("deploy_status") == DEPLOY_STATUS_DEPLOYED:
            raise RuntimeError("simulated db failure")
        return real_update(mod_id, **kwargs)

    with patch.object(db, "update_mod_deploy_status", side_effect=_update_fail):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is True
    assert out.get("warning") == "database_update_failed"
    assert (mods / "DbWarn" / "file1.txt").read_text(encoding="utf-8") == "NEW"
    assert load_manifest(mod_dir) is not None


def test_case4_prepare_overwrite_error_marks_txn_failed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mid="95004", folder="PrepFail")

    prior = mods / "PrepFail" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    with patch.object(
        BackupManager,
        "_backup_one",
        side_effect=BackupIntegrityError("backup write broken"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    txn = BackupManager(mod_dir).load_transaction()
    assert txn is not None
    status = str(txn.get("status") or "")
    assert status == TXN_FAILED
    assert status not in (TXN_PREPARED, TXN_BACKUP_DONE)
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED


def test_case5_redeploy_reuses_backup_undeploy_restores(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mid="95005", folder="ReuseMod")

    prior = mods / "ReuseMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    man1 = load_manifest(mod_dir)
    assert man1 is not None
    assert man1.files and man1.files[0].backup is not None
    backup_path_1 = man1.files[0].backup.path
    assert prior.read_text(encoding="utf-8") == "NEW"

    # Change source and deploy again (overwrite without undeploy).
    (mod_dir / "file1.txt").write_text("NEWER", encoding="utf-8")
    assert deployer.deploy_mod(pk)["success"] is True
    man2 = load_manifest(mod_dir)
    assert man2 is not None
    assert man2.files and man2.files[0].backup is not None
    # Must reuse original game-file backup, not overwrite with Mod payload.
    assert man2.files[0].backup.path == backup_path_1
    assert man2.files[0].backup.hash == man1.files[0].backup.hash
    assert prior.read_text(encoding="utf-8") == "NEWER"

    und = deployer.undeploy_mod(pk)
    assert und["success"] is True
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
