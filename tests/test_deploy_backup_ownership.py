"""Deploy overwrite-backup ownership and storage (not metadata_backup)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from services.backup_manager import (
    BACKUPS_DIRNAME,
    DEPLOY_BACKUP_DIR_NAME,
    BackupManager,
)
from services.deploy import ModDeployer
from services.deploy_apply import ApplyResult
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestBackupInfo,
    ManifestFileEntry,
    load_manifest,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "deploy_backup_ownership.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _setup_game(db: DatabaseManager, tmp_path: Path) -> Path:
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        4242,
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
) -> tuple[Path, str]:
    mod = library / "SomeGame" / title
    mod.mkdir(parents=True)
    for rel, text in (files or {"a.txt": "MOD-A"}).items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    created = create_steam_test_mod(db, external_id=mid, title=title, app_id=4242)
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod, handle=pk, title=title, app_id=4242, game_name="SomeGame"
    )
    return mod, pk


def _info_backups(mod: Path) -> Path:
    return mod / INFO_DIR_NAME / BACKUPS_DIRNAME


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_first_deploy_missing_target_creates_no_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98001", title="Fresh")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True
    target = mods_root / "Fresh" / "a.txt"
    assert target.read_text(encoding="utf-8") == "MOD-A"

    man = load_manifest(source)
    assert man is not None
    assert all(f.backup is None for f in man.files)
    mgr = BackupManager(source, internal_id=pk)
    assert mgr.listed_backup_files() == []
    assert not _info_backups(source).exists()
    assert not mgr.backups_root().exists()


def test_redeploy_does_not_backup_own_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Stand-in for the 5443-file Rockefeller self-backup: owned targets skip copy."""
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    payload = {f"f{i:02d}.txt": f"V1-{i}" for i in range(12)}
    source, pk = _add_mod(library, db, mid="98002", title="Many", files=payload)

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    mgr = BackupManager(source, internal_id=pk)
    assert mgr.listed_backup_files() == []

    for rel in payload:
        (source / rel).write_text("V2", encoding="utf-8")
    assert deployer.deploy_mod(pk)["success"] is True

    man = load_manifest(source)
    assert man is not None
    assert all(f.backup is None for f in man.files)
    assert mgr.listed_backup_files() == []
    assert not _info_backups(source).exists()
    for rel in payload:
        assert (mods_root / "Many" / rel).read_text(encoding="utf-8") == "V2"


def test_external_game_file_is_backed_up(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98003", title="Ext")
    prior = mods_root / "Ext" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME-ORIGINAL", encoding="utf-8")

    assert ModDeployer(library_root=library, db=db).deploy_mod(pk)["success"] is True
    man = load_manifest(source)
    assert man is not None
    backed = [f for f in man.files if f.backup is not None]
    assert len(backed) == 1
    mgr = BackupManager(source, internal_id=pk)
    bak = mgr.resolve_backup_file(backed[0].backup)  # type: ignore[arg-type]
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == "GAME-ORIGINAL"
    assert DEPLOY_BACKUP_DIR_NAME in bak.as_posix()
    assert not _info_backups(source).exists()
    assert prior.read_text(encoding="utf-8") == "MOD-A"


def test_rollback_restores_external_original(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98004", title="Roll")
    prior = mods_root / "Roll" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("KEEP-ME", encoding="utf-8")

    def _fail_apply(file_plan: object, **kwargs: object) -> ApplyResult:
        return ApplyResult(success=False, error="simulated apply failure")

    with patch("services.deploy_apply.apply_file_plan", _fail_apply):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert prior.read_text(encoding="utf-8") == "KEEP-ME"
    assert load_manifest(source) is None
    mgr = BackupManager(source, internal_id=pk)
    assert mgr.listed_backup_files() == []
    assert not _info_backups(source).exists()


def test_owned_target_produces_no_original_copy(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    target = tmp_path / "game" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("OURS", encoding="utf-8")
    save_manifest(
        managed,
        DeployManifest(
            mod_id="98005",
            deploy_time="t",
            deploy_type=DEPLOY_TYPE_FOLDER_COPY,
            files=[
                ManifestFileEntry(
                    source=str(managed / "a.txt"),
                    target=str(target.resolve()),
                    backup=None,
                )
            ],
        ),
    )
    mgr = BackupManager(managed, internal_id="98005")
    prep = mgr.prepare_overwrite([target], mod_id="98005")
    assert prep.by_target[str(target.resolve())] is None
    assert mgr.listed_backup_files() == []
    assert not _info_backups(managed).exists()


def test_backup_not_in_library_payload_or_info(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98006", title="Store")
    prior = mods_root / "Store" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("EXT", encoding="utf-8")

    assert ModDeployer(library_root=library, db=db).deploy_mod(pk)["success"] is True
    originals = list(source.rglob("*.original"))
    assert originals == []
    assert not _info_backups(source).exists()
    mgr = BackupManager(source, internal_id=pk)
    files = mgr.listed_backup_files()
    assert len(files) == 1
    assert files[0].resolve().is_relative_to(mgr.backups_root().resolve())
    try:
        files[0].relative_to(source)
        raise AssertionError("overwrite backup must not live under the Library Mod")
    except ValueError:
        pass


def test_manifest_backup_matches_on_disk_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(
        library, db, mid="98007", title="HashMe", files={"a.txt": "A", "b.txt": "B"}
    )
    for name, text in (("a.txt", "GA"), ("b.txt", "GB")):
        p = mods_root / "HashMe" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    assert ModDeployer(library_root=library, db=db).deploy_mod(pk)["success"] is True
    man = load_manifest(source)
    assert man is not None
    mgr = BackupManager(source, internal_id=pk)
    on_disk = {p.resolve() for p in mgr.listed_backup_files()}
    resolved: set[Path] = set()
    for entry in man.files:
        assert entry.backup is not None
        bak = mgr.resolve_backup_file(entry.backup)
        assert bak.is_file()
        assert _sha256(bak) == entry.backup.hash
        resolved.add(bak.resolve())
    assert on_disk == resolved


def test_undeploy_does_not_restore_own_payload_as_original(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98008", title="Self")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    target = mods_root / "Self" / "a.txt"
    assert target.read_text(encoding="utf-8") == "MOD-A"
    man = load_manifest(source)
    assert man is not None
    assert all(f.backup is None for f in man.files)

    assert deployer.undeploy_mod(pk)["success"] is True
    assert not target.exists()
    assert BackupManager(source, internal_id=pk).listed_backup_files() == []


def test_owned_external_backup_reused_on_redeploy_then_undeploy_restores(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98009", title="ReuseExt")
    prior = mods_root / "ReuseExt" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME-ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    man1 = load_manifest(source)
    assert man1 and man1.files[0].backup
    path1 = man1.files[0].backup.path
    (source / "a.txt").write_text("V2", encoding="utf-8")
    assert deployer.deploy_mod(pk)["success"] is True
    man2 = load_manifest(source)
    assert man2 and man2.files[0].backup
    assert man2.files[0].backup.path == path1
    mgr = BackupManager(source, internal_id=pk)
    assert len(mgr.listed_backup_files()) == 1
    assert deployer.undeploy_mod(pk)["success"] is True
    assert prior.read_text(encoding="utf-8") == "GAME-ORIGINAL"


def test_deploy_does_not_delete_library_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    _setup_game(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98010", title="KeepLib")
    extra = source / "[Gameplay] KeepMe" / "loose.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text("LIBRARY-PAYLOAD", encoding="utf-8")
    leftover = source / "notes.txt"
    leftover.write_text("do-not-delete", encoding="utf-8")

    assert ModDeployer(library_root=library, db=db).deploy_mod(pk)["success"] is True
    assert extra.is_file()
    assert extra.read_text(encoding="utf-8") == "LIBRARY-PAYLOAD"
    assert leftover.is_file()
    assert leftover.read_text(encoding="utf-8") == "do-not-delete"
    assert (source / "a.txt").read_text(encoding="utf-8") == "MOD-A"
    assert not _info_backups(source).exists()


def test_legacy_info_backup_path_still_resolves(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    legacy = managed / INFO_DIR_NAME / BACKUPS_DIRNAME
    legacy.mkdir(parents=True)
    dest = legacy / "keep.original"
    dest.write_text("OLD", encoding="utf-8")
    mgr = BackupManager(managed, internal_id="98011")
    resolved = mgr.resolve_backup_file(
        ManifestBackupInfo(path=".info/backups/keep.original", hash="x")
    )
    assert resolved == dest.resolve()
