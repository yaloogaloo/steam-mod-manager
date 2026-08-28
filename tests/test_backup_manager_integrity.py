"""Production integrity checks for Backup & Restore Deploy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.models import ModMetadata
from services.backup_manager import (
    BACKUPS_DIRNAME,
    TRANSACTION_FILENAME,
    TXN_FAILED,
    BackupIntegrityError,
    BackupManager,
    BackupRestoreError,
    transaction_path_for,
)
from services.conflict import ConflictDetector, ConflictType
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestBackupInfo,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "backup_integrity.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _meta(mod: Path, *, mid: str, title: str, app_id: int = 4242) -> None:
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "app_id": app_id,
                "game_name": "SomeGame",
            }
        ),
        encoding="utf-8",
    )


def _setup_folder_copy(db: DatabaseManager, tmp_path: Path, *, app_id: int = 4242) -> Path:
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        app_id,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    return mods_root


# ---------------------------------------------------------------------------
# Case 1 — backup uniqueness
# ---------------------------------------------------------------------------


def test_case1_backup_names_unique_across_targets_and_redeploy(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    game = tmp_path / "game"
    a = game / "a.dll"
    b = game / "b.dll"
    a.parent.mkdir(parents=True)
    a.write_text("A1", encoding="utf-8")
    b.write_text("B1", encoding="utf-8")

    mgr = BackupManager(managed)
    prep = mgr.prepare_overwrite([a, b])
    ba = prep.by_target[str(a.resolve())]
    bb = prep.by_target[str(b.resolve())]
    assert ba is not None and bb is not None
    assert ba.path != bb.path
    assert Path(managed / ba.path).is_file()
    assert Path(managed / bb.path).is_file()

    # Same target again — new unique file; old backup untouched
    a.write_text("A2", encoding="utf-8")
    # Clear prior manifest reuse by not saving a manifest
    prep2 = mgr.prepare_overwrite([a])
    ba2 = prep2.by_target[str(a.resolve())]
    assert ba2 is not None
    assert ba2.path != ba.path
    assert Path(managed / ba.path).read_text(encoding="utf-8") == "A1"
    assert Path(managed / ba2.path).read_text(encoding="utf-8") == "A2"


# ---------------------------------------------------------------------------
# Case 2 — hash verification
# ---------------------------------------------------------------------------


def test_case2_hash_mismatch_refuses_restore(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    target = tmp_path / "game" / "foo.dll"
    target.parent.mkdir(parents=True)
    target.write_text("ORIGINAL", encoding="utf-8")

    mgr = BackupManager(managed)
    prep = mgr.prepare_overwrite([target])
    info = prep.by_target[str(target.resolve())]
    assert info is not None

    # Tamper with backup bytes after hashing
    bak = Path(managed / info.path)
    bak.write_text("TAMPERED", encoding="utf-8")

    with pytest.raises(BackupIntegrityError, match="hash mismatch"):
        mgr.restore_one(info, target)


def test_case2_path_escape_refused(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    outside = tmp_path / "evil.bin"
    outside.write_text("x", encoding="utf-8")
    mgr = BackupManager(managed)
    evil = ManifestBackupInfo(path=str(outside.resolve()), hash="abc", created_at="t")
    with pytest.raises(BackupIntegrityError, match="escapes"):
        mgr.resolve_backup_file(evil)


# ---------------------------------------------------------------------------
# Case 3 — restore when target missing
# ---------------------------------------------------------------------------


def test_case3_restore_recreates_missing_target(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    target = tmp_path / "game" / "foo.dll"
    target.parent.mkdir(parents=True)
    target.write_text("ORIGINAL", encoding="utf-8")

    mgr = BackupManager(managed)
    prep = mgr.prepare_overwrite([target])
    info = prep.by_target[str(target.resolve())]
    assert info is not None

    target.unlink()
    assert not target.exists()

    mgr.restore_one(info, target)
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


# ---------------------------------------------------------------------------
# Case 4 — repeat deploy reuses first backup (no pollution)
# ---------------------------------------------------------------------------


def test_case4_repeat_deploy_reuses_original_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    mod = library / "SomeGame" / "Repeat"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("MOD-V1", encoding="utf-8")
    _meta(mod, mid="93001", title="Repeat")
    db.upsert_mod(ModMetadata(published_file_id="93001", title="Repeat", app_id=4242))

    prior = mods_root / "Repeat" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME-ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("93001")["success"] is True
    man1 = load_manifest(mod)
    assert man1 is not None
    b1 = man1.files[0].backup
    assert b1 is not None
    first_path = b1.path
    first_hash = b1.hash
    assert Path(mod / first_path).read_text(encoding="utf-8") == "GAME-ORIGINAL"

    # Second deploy without undeploy — payload changed, but backup must stay original
    (mod / "a.txt").write_text("MOD-V2", encoding="utf-8")
    assert deployer.deploy_mod("93001")["success"] is True
    man2 = load_manifest(mod)
    assert man2 is not None
    b2 = man2.files[0].backup
    assert b2 is not None
    assert b2.path == first_path
    assert b2.hash == first_hash
    assert Path(mod / first_path).read_text(encoding="utf-8") == "GAME-ORIGINAL"
    assert prior.read_text(encoding="utf-8") == "MOD-V2"

    assert deployer.undeploy_mod("93001")["success"] is True
    assert prior.read_text(encoding="utf-8") == "GAME-ORIGINAL"


# ---------------------------------------------------------------------------
# Case 5 — multi-mod same target chain
# ---------------------------------------------------------------------------


def test_case5_multi_mod_overwrite_chain(tmp_path: Path, db: DatabaseManager) -> None:
    """
    Mod A then Mod B overwrite the same absolute target via custom_deploy_path.
    Undeploy B → A's content; Undeploy A → game original.
    ConflictDetector still reports FILE_OVERWRITE while both deployed.
    """
    library = tmp_path / "mod"
    game = tmp_path / "game_install"
    game.mkdir()
    shared = game / "config.ini"
    shared.write_text("GAME", encoding="utf-8")

    # Dummy mod_path so folder_copy config exists; actual deploy uses custom path
    mods_root = tmp_path / "unused_mods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        4242,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )

    mods: dict[str, Path] = {}
    for mid, title, body in (
        ("93010", "ModA", "A"),
        ("93011", "ModB", "B"),
    ):
        folder = library / "SomeGame" / title
        folder.mkdir(parents=True)
        (folder / "config.ini").write_text(body, encoding="utf-8")
        _meta(folder, mid=mid, title=title)
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=4242))
        db.update_mod_user_metadata(
            mid,
            {
                "display_name": title,
                "custom_description": "",
                "user_notes": "",
                "favorite": False,
                "custom_deploy_path": str(game),
            },
        )
        mods[mid] = folder

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("93010")["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"
    man_a = load_manifest(mods["93010"])
    assert man_a is not None
    assert man_a.files[0].backup is not None
    assert Path(mods["93010"] / man_a.files[0].backup.path).read_text(
        encoding="utf-8"
    ) == "GAME"

    assert deployer.deploy_mod("93011")["success"] is True
    assert shared.read_text(encoding="utf-8") == "B"
    man_b = load_manifest(mods["93011"])
    assert man_b is not None
    assert man_b.files[0].backup is not None
    assert Path(mods["93011"] / man_b.files[0].backup.path).read_text(
        encoding="utf-8"
    ) == "A"

    reports = ConflictDetector(library, db=db).check_all_mods(persist=False)
    overwrite = [
        c
        for r in reports.values()
        for c in r.conflicts
        if c.conflict_type == ConflictType.FILE_OVERWRITE.value
    ]
    assert overwrite
    assert sorted(overwrite[0].mods) == ["93010", "93011"]

    assert deployer.undeploy_mod("93011")["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"

    assert deployer.undeploy_mod("93010")["success"] is True
    assert shared.read_text(encoding="utf-8") == "GAME"


# ---------------------------------------------------------------------------
# Case 6 — transaction + partial restore failure
# ---------------------------------------------------------------------------


def test_case6_partial_restore_leaves_failed_transaction(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    files = {
        "a.txt": "GA",
        "b.txt": "GB",
        "c.txt": "GC",
    }
    targets = []
    for name, text in files.items():
        p = game / name
        p.write_text(text, encoding="utf-8")
        targets.append(p)

    mgr = BackupManager(managed)
    prep = mgr.prepare_overwrite(targets)
    assert all(prep.by_target[str(t.resolve())] is not None for t in targets)

    # Build a fake post-deploy manifest
    entries = []
    for t in targets:
        info = prep.by_target[str(t.resolve())]
        assert info is not None
        entries.append(
            ManifestFileEntry(
                source=str(t),
                target=str(t.resolve()),
                backup=info,
            )
        )
    manifest = DeployManifest(
        mod_id="93020",
        deploy_time="t",
        deploy_type="folder_copy",
        files=entries,
    )
    save_manifest(managed, manifest)

    # Simulate deploy overwrite then delete targets (undeploy mid-state)
    for t in targets:
        t.write_text("MOD", encoding="utf-8")
        t.unlink()

    # Corrupt C's backup so restore fails after A/B succeed
    c_info = prep.by_target[str((game / "c.txt").resolve())]
    assert c_info is not None
    Path(managed / c_info.path).write_text("CORRUPT", encoding="utf-8")

    with pytest.raises(BackupRestoreError) as caught:
        mgr.restore_from_manifest(manifest)
    assert any("c.txt" in f for f in caught.value.failures)

    # A/B restored despite C failure
    assert (game / "a.txt").read_text(encoding="utf-8") == "GA"
    assert (game / "b.txt").read_text(encoding="utf-8") == "GB"
    assert not (game / "c.txt").exists() or (game / "c.txt").read_text(
        encoding="utf-8"
    ) != "GC"

    txn = mgr.load_transaction()
    assert txn is not None
    assert txn.get("status") == TXN_FAILED
    # Backups retained for diagnosis
    assert (managed / INFO_DIR_NAME / BACKUPS_DIRNAME).is_dir()
    assert list((managed / INFO_DIR_NAME / BACKUPS_DIRNAME).iterdir())


def test_case6_deploy_exception_clears_transaction_on_clean_rollback(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    mod = library / "SomeGame" / "Boom"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("MOD", encoding="utf-8")
    _meta(mod, mid="93021", title="Boom")
    db.upsert_mod(ModMetadata(published_file_id="93021", title="Boom", app_id=4242))

    prior = mods_root / "Boom" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("KEEP", encoding="utf-8")

    def boom(self, ctx):  # noqa: ANN001
        prior.write_text("PARTIAL", encoding="utf-8")
        raise RuntimeError("boom")

    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.deploy",
        boom,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            ModDeployer(library_root=library, db=db).deploy_mod("93021")

    assert prior.read_text(encoding="utf-8") == "KEEP"
    assert not transaction_path_for(mod).is_file()
    assert not (mod / INFO_DIR_NAME / BACKUPS_DIRNAME).exists()


def test_undeploy_survives_missing_target(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    mod = library / "SomeGame" / "Gone"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("MOD", encoding="utf-8")
    _meta(mod, mid="93022", title="Gone")
    db.upsert_mod(ModMetadata(published_file_id="93022", title="Gone", app_id=4242))

    prior = mods_root / "Gone" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("93022")["success"] is True
    # External deletion of deployed file
    prior.unlink()
    assert not prior.exists()

    assert deployer.undeploy_mod("93022")["success"] is True
    assert prior.read_text(encoding="utf-8") == "GAME"
