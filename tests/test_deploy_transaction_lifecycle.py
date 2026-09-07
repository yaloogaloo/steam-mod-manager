"""Deploy transaction lifecycle — commit order, recover error preservation."""

from __future__ import annotations

import json
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
    TXN_BACKUP_DONE,
    BackupManager,
    transaction_path_for,
)
from services.deploy import ModDeployer
from services.deploy_security import ManifestSecurityError
from services.deploy_txn import (
    compose_recover_deploy_error,
    is_active_deploy_transaction,
    register_active_deploy_transaction,
    unregister_active_deploy_transaction,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "txn_lifecycle.db")
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
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=folder.name,
            app_id=100,
            managed_path=str(folder),
        )
    )
    return folder


def test_compose_recover_preserves_original_error() -> None:
    original = "部署清单校验失败：path escape"
    out = compose_recover_deploy_error(original)
    assert original in out
    assert "rollback completed" in out
    assert out != "interrupted deploy rolled back from transaction"
    # idempotent append
    assert compose_recover_deploy_error(out) == out


def test_case1_manifest_failure_keeps_original_deploy_error(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Manifest stage failure must keep the concrete error (not interrupted recover)."""
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="97001")
    prior = mods / mod_dir.name / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    with patch(
        "services.deploy.validate_manifest_for_save",
        side_effect=ManifestSecurityError("path escape: simulated"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod("97001")

    assert out["success"] is False
    assert "部署清单校验失败" in str(out.get("error") or "")
    assert "path escape" in str(out.get("error") or "")
    assert "interrupted deploy" not in str(out.get("error") or "")

    info = db.get_mod_deploy_info("97001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert "path escape" in str(info.deploy_error or "")
    assert "interrupted deploy" not in str(info.deploy_error or "")
    assert not transaction_path_for(mod_dir).is_file()


def test_case2_residual_txn_recover_does_not_overwrite_error(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Stale txn rollback appends note only; original FAILED error stays."""
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="97002")
    original_error = "部署清单校验失败：path escape: simulated"

    prior = mods / mod_dir.name / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("PARTIAL", encoding="utf-8")

    db.update_mod_deploy_status(
        "97002",
        deploy_status=DEPLOY_STATUS_FAILED,
        deploy_path="",
        deploy_time="",
        deploy_error=original_error,
        app_id=100,
    )

    mgr = BackupManager(mod_dir)
    prep = mgr.prepare_overwrite([prior], mod_id="97002")
    # Leave txn as backup_done (simulate crash after abort failed to clear, or
    # mid-flight leftover) while DB already has the real failure reason.
    assert prep.by_target
    unregister_active_deploy_transaction(mod_dir)
    assert mgr.load_transaction() is not None
    assert mgr.load_transaction().get("status") == TXN_BACKUP_DONE

    reports = ModDeployer(
        library_root=library, db=db
    ).recover_stale_deploy_transactions()
    assert any(r.get("action") == "rolled_back" for r in reports)

    info = db.get_mod_deploy_info("97002")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    err = str(info.deploy_error or "")
    assert original_error in err
    assert "rollback completed" in err
    assert err.index(original_error) < err.index("rollback completed")
    # Must not replace with interrupted-only message
    assert err.strip() != "interrupted deploy rolled back from transaction"
    assert not transaction_path_for(mod_dir).is_file()


def test_case3_deployed_only_after_transaction_commit(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """DB DEPLOYED is written only after mark_deployed / txn cleared."""
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="97003")

    order: list[str] = []
    real_mark = BackupManager.mark_deployed

    def _mark(self: BackupManager, prep, *, mod_id: str = "") -> None:  # noqa: ANN001
        order.append("mark_deployed")
        # Transaction still present before commit helper finishes.
        assert self.load_transaction() is not None
        mid_info = db.get_mod_deploy_info("97003")
        assert mid_info is None or mid_info.deploy_status != DEPLOY_STATUS_DEPLOYED
        real_mark(self, prep, mod_id=mod_id)
        order.append("txn_cleared")
        assert self.load_transaction() is None

    real_update = db.update_mod_deploy_status

    def _update(mod_id, **kwargs):  # noqa: ANN001
        status = str(kwargs.get("deploy_status") or "")
        if status == DEPLOY_STATUS_DEPLOYED:
            order.append("db_deployed")
            assert "txn_cleared" in order
            assert not transaction_path_for(mod_dir).is_file()
        return real_update(mod_id, **kwargs)

    with (
        patch.object(BackupManager, "mark_deployed", _mark),
        patch.object(db, "update_mod_deploy_status", _update),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod("97003")

    assert out["success"] is True
    assert order.index("mark_deployed") < order.index("db_deployed")
    assert order.index("txn_cleared") < order.index("db_deployed")
    info = db.get_mod_deploy_info("97003")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert not transaction_path_for(mod_dir).is_file()


def test_active_txn_skipped_by_stale_recover(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Startup reconcile must not recover a currently active deploy txn."""
    library = tmp_path / "library"
    mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, db, mid="97004")
    prior = mods / mod_dir.name / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    mgr = BackupManager(mod_dir)
    mgr.prepare_overwrite([prior], mod_id="97004")
    assert is_active_deploy_transaction(mod_dir)

    reports = ModDeployer(
        library_root=library, db=db
    ).recover_stale_deploy_transactions()
    assert any(r.get("action") == "skipped_active" for r in reports)
    assert mgr.load_transaction() is not None
    assert prior.read_text(encoding="utf-8") == "GAME"

    unregister_active_deploy_transaction(mod_dir)
    # Cleanup leftovers so tmp does not leak active state across tests
    mgr.clear_transaction()
