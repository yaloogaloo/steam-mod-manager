"""Deploy backup storage lifecycle — reuse, bytes, cleanup (not metadata_backup)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from services.backup_manager import (
    BACKUPS_DIRNAME,
    BackupIntegrityError,
    BackupManager,
)
from services.deploy import ModDeployer
from services.deploy_apply import ApplyResult
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

SIZE_10 = 10 * 1024
SIZE_20 = 20 * 1024
SIZE_30 = 30 * 1024


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "deploy_backup_lifecycle.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _setup_folder_copy(db: DatabaseManager, tmp_path: Path) -> Path:
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
    files: dict[str, bytes | str] | None = None,
) -> tuple[Path, str]:
    mod = library / "SomeGame" / title
    mod.mkdir(parents=True)
    payload = files or {"a.txt": "MOD-A"}
    for rel, body in payload.items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    created = create_steam_test_mod(db, external_id=mid, title=title, app_id=4242)
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod, handle=pk, title=title, app_id=4242, game_name="SomeGame"
    )
    return mod, pk


def _backup_bytes(mgr: BackupManager) -> int:
    return sum(p.stat().st_size for p in mgr.listed_backup_files())


def _info_backups(mod: Path) -> Path:
    return mod / INFO_DIR_NAME / BACKUPS_DIRNAME


def test_external_overwrite_creates_one_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98201", title="Once")
    prior = mods_root / "Once" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True
    man = load_manifest(source)
    assert man is not None
    backed = [f for f in man.files if f.backup is not None]
    assert len(backed) == 1
    mgr = BackupManager(source, internal_id=pk)
    files = mgr.listed_backup_files()
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "ORIGINAL"
    assert _backup_bytes(mgr) == len("ORIGINAL".encode("utf-8"))
    assert not _info_backups(source).exists()


def test_repeat_deploy_does_not_duplicate_backup_bytes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98202", title="Repeat")
    prior = mods_root / "Repeat" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"O" * SIZE_10)

    deployer = ModDeployer(library_root=library, db=db)
    sizes: list[int] = []
    paths: list[str] = []
    for i in range(4):
        (source / "a.txt").write_text(f"V{i}", encoding="utf-8")
        assert deployer.deploy_mod(pk)["success"] is True
        mgr = BackupManager(source, internal_id=pk)
        sizes.append(_backup_bytes(mgr))
        man = load_manifest(source)
        assert man and man.files[0].backup
        paths.append(man.files[0].backup.path)
        assert len(mgr.listed_backup_files()) == 1
        assert not _info_backups(source).exists()

    assert sizes[0] == SIZE_10
    assert sizes[0] == sizes[1] == sizes[2] == sizes[3]
    assert paths[0] == paths[1] == paths[2] == paths[3]


def test_backup_bytes_only_overwritten_external_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(
        library,
        db,
        mid="98203",
        title="Partial",
        files={"a.bin": b"A" * SIZE_10, "c.bin": b"C" * SIZE_30},
    )
    dest = mods_root / "Partial"
    dest.mkdir(parents=True)
    (dest / "a.bin").write_bytes(b"1" * SIZE_10)
    (dest / "b.bin").write_bytes(b"2" * SIZE_20)
    (dest / "c.bin").write_bytes(b"3" * SIZE_30)

    assert ModDeployer(library_root=library, db=db).deploy_mod(pk)["success"] is True
    man = load_manifest(source)
    assert man is not None
    backed = [f for f in man.files if f.backup is not None]
    assert len(backed) == 2
    mgr = BackupManager(source, internal_id=pk)
    files = mgr.listed_backup_files()
    assert len(files) == 2
    total = _backup_bytes(mgr)
    assert total == SIZE_10 + SIZE_30
    assert total != SIZE_10 + SIZE_20 + SIZE_30
    resolved = {
        mgr.resolve_backup_file(f.backup).resolve() for f in backed
    }  # type: ignore[union-attr]
    assert resolved == {p.resolve() for p in files}
    assert (dest / "b.bin").read_bytes() == b"2" * SIZE_20
    assert not _info_backups(source).exists()


def test_failed_apply_cleans_transaction_only_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98204", title="FailApply")
    prior = mods_root / "FailApply" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"X" * SIZE_10)

    with patch(
        "services.deploy_apply.apply_file_plan",
        return_value=ApplyResult(success=False, error="simulated apply failure"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert prior.read_bytes() == b"X" * SIZE_10
    mgr = BackupManager(source, internal_id=pk)
    assert mgr.listed_backup_files() == []
    assert not mgr.backups_root().exists()
    assert load_manifest(source) is None
    assert not _info_backups(source).exists()


def test_failed_prepare_prunes_partial_new_backups(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(
        library,
        db,
        mid="98205",
        title="FailPrep",
        files={"a.txt": "MA", "b.txt": "MB"},
    )
    for name, text in (("a.txt", "GA"), ("b.txt", "GB")):
        p = mods_root / "FailPrep" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    real = BackupManager._backup_one
    calls = {"n": 0}

    def _once(self: BackupManager, target: Path, backup_root: Path):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise BackupIntegrityError("second backup broken")
        return real(self, target, backup_root)

    with patch.object(BackupManager, "_backup_one", _once):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    mgr = BackupManager(source, internal_id=pk)
    assert mgr.listed_backup_files() == []
    assert not mgr.backups_root().exists()
    assert (mods_root / "FailPrep" / "a.txt").read_text(encoding="utf-8") == "GA"
    assert (mods_root / "FailPrep" / "b.txt").read_text(encoding="utf-8") == "GB"


def test_failed_prepare_keeps_prior_referenced_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(
        library,
        db,
        mid="98206",
        title="KeepPrior",
        files={"a.txt": "A1"},
    )
    prior_a = mods_root / "KeepPrior" / "a.txt"
    prior_a.parent.mkdir(parents=True)
    prior_a.write_text("ORIGINAL-A", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    man1 = load_manifest(source)
    assert man1 is not None
    first = next(f.backup for f in man1.files if f.backup is not None)
    assert first is not None
    mgr = BackupManager(source, internal_id=pk)
    before = _backup_bytes(mgr)
    assert before > 0

    (source / "extra.txt").write_text("E1", encoding="utf-8")
    extra_game = mods_root / "KeepPrior" / "extra.txt"
    extra_game.write_text("ORIGINAL-E", encoding="utf-8")
    (source / "a.txt").write_text("A2", encoding="utf-8")

    real = BackupManager._backup_one

    def _fail_new(self: BackupManager, target: Path, backup_root: Path):
        if target.name == "extra.txt":
            raise BackupIntegrityError("new target backup broken")
        return real(self, target, backup_root)

    with patch.object(BackupManager, "_backup_one", _fail_new):
        out = deployer.deploy_mod(pk)

    assert out["success"] is False
    man_still = load_manifest(source)
    assert man_still is not None
    assert any(
        f.backup is not None and f.backup.path == first.path for f in man_still.files
    )
    listed = mgr.listed_backup_files()
    assert len(listed) == 1
    assert listed[0].read_text(encoding="utf-8") == "ORIGINAL-A"
    assert _backup_bytes(mgr) == before
    assert prior_a.read_text(encoding="utf-8") == "A1"


def test_successful_undeploy_removes_backup_dir(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98207", title="Gone")
    prior = mods_root / "Gone" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"Z" * SIZE_10)

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    mgr = BackupManager(source, internal_id=pk)
    assert _backup_bytes(mgr) == SIZE_10

    assert deployer.undeploy_mod(pk)["success"] is True
    assert prior.read_bytes() == b"Z" * SIZE_10
    assert mgr.listed_backup_files() == []
    assert not mgr.backups_root().exists()
    assert not _info_backups(source).exists()


def _setup_shared_pair(db: DatabaseManager, tmp_path: Path, *, a_id: str, b_id: str):
    library = tmp_path / "mod"
    game = tmp_path / "game_install"
    game.mkdir()
    unused = tmp_path / "unused_mods"
    unused.mkdir()
    db.update_game_deploy_config(
        4242,
        name="SomeGame",
        mod_path=str(unused),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    folders: dict[str, Path] = {}
    pks: dict[str, str] = {}
    for mid, title, body in ((a_id, "ModA", "A"), (b_id, "ModB", "B")):
        folder = library / "SomeGame" / title
        folder.mkdir(parents=True)
        (folder / "shared" / "file.cfg").parent.mkdir(parents=True)
        (folder / "shared" / "file.cfg").write_text(body, encoding="utf-8")
        created = create_steam_test_mod(db, external_id=mid, title=title, app_id=4242)
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
        folders[mid] = folder
        pks[mid] = pk
    shared = game / "shared" / "file.cfg"
    return library, shared, folders, pks


def test_lifo_restore_and_cleanup(tmp_path: Path, db: DatabaseManager) -> None:
    library, shared, folders, pks = _setup_shared_pair(
        db, tmp_path, a_id="98208", b_id="98209"
    )
    shared.parent.mkdir(parents=True)
    shared.write_text("ORIGINAL", encoding="utf-8")
    dep = ModDeployer(library_root=library, db=db)
    pk_a, pk_b = pks["98208"], pks["98209"]
    assert dep.deploy_mod(pk_a)["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"
    mgr_a = BackupManager(folders["98208"], internal_id=pk_a)
    assert _backup_bytes(mgr_a) == len("ORIGINAL".encode("utf-8"))

    assert dep.deploy_mod(pk_b)["success"] is True
    assert shared.read_text(encoding="utf-8") == "B"
    mgr_b = BackupManager(folders["98209"], internal_id=pk_b)
    assert mgr_b.listed_backup_files()[0].read_text(encoding="utf-8") == "A"
    assert mgr_a.listed_backup_files()[0].read_text(encoding="utf-8") == "ORIGINAL"

    assert dep.undeploy_mod(pk_b)["success"] is True
    assert shared.read_text(encoding="utf-8") == "A"
    assert mgr_b.listed_backup_files() == []
    assert not mgr_b.backups_root().exists()
    assert mgr_a.listed_backup_files()[0].read_text(encoding="utf-8") == "ORIGINAL"
    assert not _info_backups(folders["98208"]).exists()
    assert not _info_backups(folders["98209"]).exists()

    assert dep.undeploy_mod(pk_a)["success"] is True
    assert shared.read_text(encoding="utf-8") == "ORIGINAL"
    assert mgr_a.listed_backup_files() == []
    assert not mgr_a.backups_root().exists()


def test_info_never_gains_backups_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98210", title="CleanInfo")
    prior = mods_root / "CleanInfo" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME", encoding="utf-8")
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    assert deployer.deploy_mod(pk)["success"] is True
    assert deployer.undeploy_mod(pk)["success"] is True
    assert not _info_backups(source).exists()
    assert list(source.rglob("*.original")) == []


def test_redeploy_api_does_not_accumulate_bytes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_folder_copy(db, tmp_path)
    source, pk = _add_mod(library, db, mid="98211", title="ReApi")
    prior = mods_root / "ReApi" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"R" * SIZE_10)

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(pk)["success"] is True
    mgr = BackupManager(source, internal_id=pk)
    first = _backup_bytes(mgr)
    assert first == SIZE_10
    assert deployer.redeploy_mod(pk)["success"] is True
    second = _backup_bytes(mgr)
    assert second == first
    assert len(mgr.listed_backup_files()) == 1
    assert not _info_backups(source).exists()
