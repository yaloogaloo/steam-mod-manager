"""Deploy lifecycle integrity after Backup & Restore Deploy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    DatabaseManager,
)
from services.backup_manager import (
    BACKUPS_DIRNAME,
    TXN_BACKUP_DONE,
    TXN_FAILED,
    BackupManager,
    transaction_path_for,
)
from services.conflict import ConflictDetector, ConflictType
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest
from services.deploy_rules.manifest import MANIFEST_FILENAME
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lifecycle_integrity.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _setup(db: DatabaseManager, tmp_path: Path, *, app_id: int = 4242) -> Path:
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        app_id,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    return mods_root


def _add_mod(
    library: Path,
    db: DatabaseManager,
    *,
    mid: str,
    title: str,
    files: dict[str, str] | None = None,
    app_id: int = 4242,
) -> tuple[Path, str]:
    mod = library / "SomeGame" / title
    mod.mkdir(parents=True)
    for rel, text in (files or {"a.txt": "MOD"}).items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=str(mid), title=title, app_id=app_id, game_name="SomeGame"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod, handle=pk, title=title, app_id=app_id, game_name="SomeGame"
    )
    return mod, pk


# ---------------------------------------------------------------------------
# Case 1 — deploy status consistent with manifest
# ---------------------------------------------------------------------------


def test_case1_deploy_status_matches_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94001", title="StatusMod")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True

    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_path
    assert info.deploy_time

    man = load_manifest(source)
    assert man is not None
    assert man.mod_id == pk
    assert (source / INFO_DIR_NAME / MANIFEST_FILENAME).is_file()
    assert (mods_root / "StatusMod" / "a.txt").is_file()


# ---------------------------------------------------------------------------
# Case 2 — manifest lifecycle
# ---------------------------------------------------------------------------


def test_case2_manifest_absent_after_failed_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94002", title="FailMan")
    prior = mods_root / "FailMan" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    from services.deploy_apply import ApplyResult

    def boom(file_plan: object, **kwargs: object) -> ApplyResult:
        prior.write_text("PARTIAL", encoding="utf-8")
        return ApplyResult(success=False, error="copy failed")

    with patch("services.deploy_apply.apply_file_plan", boom):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert load_manifest(source) is None
    assert not (source / INFO_DIR_NAME / MANIFEST_FILENAME).exists()
    assert prior.read_text(encoding="utf-8") == "GAME"
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


def test_case2_manifest_removed_on_undeploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94003", title="UndeployMan")
    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(pk)["success"] is True
    assert load_manifest(source) is not None
    assert dep.undeploy_mod(pk)["success"] is True
    assert load_manifest(source) is None


# ---------------------------------------------------------------------------
# Case 3 — repeat deploy reuses backup
# ---------------------------------------------------------------------------


def test_case3_repeat_deploy_reuses_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94004", title="Repeat", files={"a.txt": "V1"})
    prior = mods_root / "Repeat" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(pk)["success"] is True
    man1 = load_manifest(source)
    assert man1 and man1.files[0].backup
    path1 = man1.files[0].backup.path
    hash1 = man1.files[0].backup.hash

    (source / "a.txt").write_text("V2", encoding="utf-8")
    assert dep.deploy_mod(pk)["success"] is True
    man2 = load_manifest(source)
    assert man2 and man2.files[0].backup
    assert man2.files[0].backup.path == path1
    assert man2.files[0].backup.hash == hash1
    # Only one referenced backup file remains (orphans pruned)
    listed = BackupManager(source, internal_id=pk).listed_backup_files()
    assert len(listed) == 1
    assert not (source / INFO_DIR_NAME / BACKUPS_DIRNAME).exists()


# ---------------------------------------------------------------------------
# Case 4 — partial deploy failure restores earlier files
# ---------------------------------------------------------------------------


def test_case4_partial_deploy_failure_restores(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(
        library,
        db,
        mid="94005",
        title="Partial",
        files={"a.txt": "MA", "b.txt": "MB", "c.txt": "MC"},
    )
    for name, text in (("a.txt", "GA"), ("b.txt", "GB"), ("c.txt", "GC")):
        p = mods_root / "Partial" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    from services.deploy_apply import ApplyResult

    def flaky(file_plan: object, **kwargs: object) -> ApplyResult:
        # Copy only a.txt then fail (simulate mid-deploy crash)
        src = source / "a.txt"
        dst = mods_root / "Partial" / "a.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return ApplyResult(success=False, error="B failed")

    with patch("services.deploy_apply.apply_file_plan", flaky):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert (mods_root / "Partial" / "a.txt").read_text(encoding="utf-8") == "GA"
    assert (mods_root / "Partial" / "b.txt").read_text(encoding="utf-8") == "GB"
    assert (mods_root / "Partial" / "c.txt").read_text(encoding="utf-8") == "GC"
    assert load_manifest(source) is None
    assert not transaction_path_for(source).is_file()
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


# ---------------------------------------------------------------------------
# Case 5 — undeploy boundaries
# ---------------------------------------------------------------------------


def test_case5_undeploy_missing_target_restores(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    _source, pk = _add_mod(library, db, mid="94006", title="GoneT")
    prior = mods_root / "GoneT" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(pk)["success"] is True
    prior.unlink()
    assert dep.undeploy_mod(pk)["success"] is True
    assert prior.read_text(encoding="utf-8") == "GAME"


def test_case5_undeploy_missing_backup_errors(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94007", title="NoBak")
    prior = mods_root / "NoBak" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(pk)["success"] is True
    man = load_manifest(source)
    assert man and man.files[0].backup
    bak = BackupManager(source, internal_id=pk).resolve_backup_file(
        man.files[0].backup
    )
    bak.unlink()

    out = dep.undeploy_mod(pk)
    assert out["success"] is False
    err = str(out.get("error") or "")
    assert "备份" in err or "backup" in err.lower()
    # Targets must NOT have been deleted when preflight fails
    assert prior.is_file()
    assert prior.read_text(encoding="utf-8") == "MOD"
    # Manifest retained for diagnosis (no silent delete)
    assert load_manifest(source) is not None


# ---------------------------------------------------------------------------
# Case 6 — multi-mod backup chain
# ---------------------------------------------------------------------------


def test_case6_backup_chain_a_then_b(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    game = tmp_path / "install"
    game.mkdir()
    shared = game / "original.ini"
    shared.write_text("ORIGINAL", encoding="utf-8")

    unused = tmp_path / "unused"
    unused.mkdir()
    db.update_game_deploy_config(
        4242, name="SomeGame", mod_path=str(unused), deploy_type=DEPLOY_TYPE_FOLDER_COPY
    )

    pks: dict[str, str] = {}
    for mid, title, body in (("94010", "ModA", "A"), ("94011", "ModB", "B")):
        folder = library / "SomeGame" / title
        folder.mkdir(parents=True)
        (folder / "original.ini").write_text(body, encoding="utf-8")
        created = create_steam_test_mod(
            db, external_id=str(mid), title=title, app_id=4242, game_name="SomeGame"
        )
        pk = str(created.mod_id)
        prove_managed_folder(
            db, folder, handle=pk, title=title, app_id=4242, game_name="SomeGame"
        )
        db.update_mod_user_metadata(
            pk,
            {
                "display_name": title,
                "custom_description": "",
                "user_notes": "",
                "favorite": False,
                "custom_deploy_path": str(game),
            },
        )
        pks[mid] = pk

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod(pks["94010"])["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"
    assert dep.deploy_mod(pks["94011"])["success"] is True
    assert shared.read_text(encoding="utf-8") == "B"

    reports = ConflictDetector(library, db=db).check_all_mods(persist=False)
    overwrite = [
        c
        for r in reports.values()
        for c in r.conflicts
        if c.conflict_type == ConflictType.FILE_OVERWRITE.value
    ]
    assert overwrite

    assert dep.undeploy_mod(pks["94011"])["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"
    assert dep.undeploy_mod(pks["94010"])["success"] is True
    assert shared.read_text(encoding="utf-8") == "ORIGINAL"


# ---------------------------------------------------------------------------
# Case 7 — transaction recovery after crash at backup_done
# ---------------------------------------------------------------------------


def test_case7_transaction_recovery_from_backup_done(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup(db, tmp_path)
    source, pk = _add_mod(library, db, mid="94020", title="Crash")
    prior = mods_root / "Crash" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    mgr = BackupManager(source, internal_id=pk)
    prep = mgr.prepare_overwrite([prior], mod_id=pk)
    assert prep.by_target[str(prior.resolve())] is not None
    # Simulate crash after backup_done: target already overwritten, txn left.
    # Process restart clears the in-memory active-txn registry.
    from services.deploy_txn import unregister_active_deploy_transaction

    prior.write_text("PARTIAL-MOD", encoding="utf-8")
    unregister_active_deploy_transaction(source)
    txn = mgr.load_transaction()
    assert txn is not None
    assert txn.get("status") == TXN_BACKUP_DONE

    reports = ModDeployer(
        library_root=library, db=db
    ).recover_stale_deploy_transactions()
    assert any(r.get("action") == "rolled_back" for r in reports)
    assert prior.read_text(encoding="utf-8") == "GAME"
    assert not transaction_path_for(source).is_file()
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    err = str(info.deploy_error or "")
    assert "rollback completed" in err
    assert "interrupted deploy" in err


def test_case7_failed_transaction_needs_attention(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    mgr = BackupManager(managed)
    mgr.write_transaction(
        status=TXN_FAILED,
        targets=["x"],
        backups=[],
        mod_id="1",
    )
    result = mgr.recover_interrupted_transaction(auto_rollback=True)
    assert result["action"] == "needs_attention"
    assert mgr.load_transaction() is not None
