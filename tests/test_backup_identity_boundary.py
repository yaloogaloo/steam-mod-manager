"""Phase 5A: Backup UUID write / dual-read. No directory migration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.backup_identity import (
    INVALID_FROZEN_INTERNAL_ID,
    BackupIdentityError,
    resolve_backup_storage_key,
)
from services.backup_manager import BackupManager
from services import metadata_backup as mb
from services.metadata_backup import (
    BACKUP_DIR_NAME,
    backup_root,
    load_backup,
    snapshot_from_mod_folder,
)
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    prove_managed_folder,
    write_info_sidecar,
)

APP_ID = 4242
SAMPLE_UUID = "36834fcf-3cbb-4ffe-8b78-be1921638bd4"
SAMPLE_PK = "296"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "phase5a_backup.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    DatabaseManager.reset_instance()


def _digit_backup_dirs() -> list[Path]:
    root = mb.data_dir() / BACKUP_DIR_NAME
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir() and p.name.isdigit()]


def _seed_sample(db: DatabaseManager, tmp_path: Path) -> tuple[Path, str, str]:
    created = create_steam_test_mod(
        db,
        external_id="3308841144",
        title="Sample",
        app_id=APP_ID,
        game_name="GameA",
        source_url="https://example.test/3308841144",
    )
    pk = str(created.mod_id)
    folder = tmp_path / "mod" / "GameA" / "Sample"
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    db.update_mod_identity_fields(pk, internal_id=SAMPLE_UUID)
    write_info_sidecar(
        folder,
        internal_id=SAMPLE_UUID,
        title="Sample",
        external_id="3308841144",
        workspace_id="3308841144",
        app_id=APP_ID,
        game_name="GameA",
        extra={
            "source_type": "steam",
            "source_url": "https://example.test/3308841144",
        },
    )
    bind_managed_path(db, pk, folder, game_name="GameA", title="Sample")
    prove_managed_folder(db, folder, handle=pk)
    return folder, pk, SAMPLE_UUID


def test_resolve_backup_storage_key_uuid() -> None:
    assert resolve_backup_storage_key(internal_id=SAMPLE_UUID) == SAMPLE_UUID


def test_pk_write_rejection() -> None:
    with pytest.raises(BackupIdentityError, match=INVALID_FROZEN_INTERNAL_ID):
        resolve_backup_storage_key(internal_id=SAMPLE_PK)
    with pytest.raises(BackupIdentityError, match=INVALID_FROZEN_INTERNAL_ID):
        backup_root(SAMPLE_PK)


def test_uuid_write(db: DatabaseManager, tmp_path: Path) -> None:
    folder, pk, frozen = _seed_sample(db, tmp_path)
    snap = snapshot_from_mod_folder(folder, owner_mod_id=frozen)
    assert snap is not None
    uuid_dir = mb.data_dir() / BACKUP_DIR_NAME / frozen
    pk_dir = mb.data_dir() / BACKUP_DIR_NAME / pk
    assert uuid_dir.is_dir()
    assert (uuid_dir / "metadata.json").is_file()
    assert not pk_dir.exists()
    payload = json.loads((uuid_dir / "metadata.json").read_text(encoding="utf-8"))
    assert payload.get("internal_id") == frozen


def test_uuid_read_from_uuid_dir(db: DatabaseManager, tmp_path: Path) -> None:
    folder, _pk, frozen = _seed_sample(db, tmp_path)
    assert snapshot_from_mod_folder(folder, owner_mod_id=frozen) is not None
    loaded = load_backup(frozen)
    assert loaded is not None
    assert loaded.metadata.get("internal_id") == frozen


def test_legacy_pk_fallback(db: DatabaseManager, tmp_path: Path) -> None:
    folder, pk, frozen = _seed_sample(db, tmp_path)
    pk_dir = mb.data_dir() / BACKUP_DIR_NAME / pk
    pk_dir.mkdir(parents=True, exist_ok=True)
    (pk_dir / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": frozen,
                "title": "Sample",
                "workspace_id": "3308841144",
                "source_type": "steam",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    uuid_dir = mb.data_dir() / BACKUP_DIR_NAME / frozen
    assert not uuid_dir.exists()
    loaded = load_backup(frozen)
    assert loaded is not None
    assert loaded.metadata.get("internal_id") == frozen
    assert not uuid_dir.exists()
    assert pk_dir.is_dir()


def test_write_does_not_create_or_move_pk_dir(db: DatabaseManager, tmp_path: Path) -> None:
    folder, pk, frozen = _seed_sample(db, tmp_path)
    pk_dir = mb.data_dir() / BACKUP_DIR_NAME / pk
    pk_dir.mkdir(parents=True, exist_ok=True)
    (pk_dir / "metadata.json").write_text(
        json.dumps({"internal_id": frozen, "title": "legacy"}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = {p.name for p in _digit_backup_dirs()}
    assert pk in before
    snap = snapshot_from_mod_folder(folder, owner_mod_id=frozen)
    assert snap is not None
    after = {p.name for p in _digit_backup_dirs()}
    assert before == after
    assert pk_dir.is_dir()
    assert (pk_dir / "metadata.json").is_file()
    uuid_dir = mb.data_dir() / BACKUP_DIR_NAME / frozen
    assert uuid_dir.is_dir()
    assert uuid_dir.name == frozen


def test_backup_manager_identity_is_uuid(db: DatabaseManager, tmp_path: Path) -> None:
    folder, pk, frozen = _seed_sample(db, tmp_path)
    mgr = BackupManager(folder, internal_id=frozen, mod_pk=pk)
    assert mgr.internal_id == frozen
    assert mgr.mod_pk == pk
    assert mgr.storage_key() == frozen
    assert mgr.backups_root().name == frozen
    # deploy.py still passes PK; constructor must not keep PK as storage identity.
    remapped = BackupManager(folder, internal_id=pk)
    assert remapped.internal_id == frozen
    assert remapped.mod_pk == pk
    assert remapped.storage_key() == frozen
    assert not remapped.storage_key().isdigit()


def test_deploy_backup_write_uuid_only(db: DatabaseManager, tmp_path: Path) -> None:
    folder, pk, frozen = _seed_sample(db, tmp_path)
    mgr = BackupManager(folder, internal_id=frozen, mod_pk=pk)
    assert mgr.storage_key() == frozen
    root = mgr.backups_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = b"overwrite-original"
    dest = root / "game.dat.aabb.original"
    dest.write_bytes(payload)
    store = mb.data_dir() / "deploy_backup"
    assert (store / frozen / dest.name).is_file()
    assert not (store / pk).exists()
    assert dest.read_bytes() == payload


def test_deploy_backup_legacy_pk_fallback(db: DatabaseManager, tmp_path: Path) -> None:
    from services.deploy_rules.manifest import ManifestBackupInfo

    folder, pk, frozen = _seed_sample(db, tmp_path)
    store = mb.data_dir() / "deploy_backup"
    pk_dir = store / pk
    pk_dir.mkdir(parents=True, exist_ok=True)
    fname = "a.txt.1fd5f5a768d3.original"
    body = b"legacy-overwrite"
    (pk_dir / fname).write_bytes(body)
    uuid_dir = store / frozen
    assert not uuid_dir.exists()

    mgr = BackupManager(folder, internal_id=frozen, mod_pk=pk)
    assert mgr.storage_key() == frozen
    assert mgr.backups_root().name == frozen
    listed = {p.name for p in mgr.listed_backup_files()}
    assert fname in listed
    digest = hashlib.sha256(body).hexdigest()
    resolved = mgr.resolve_backup_file(
        ManifestBackupInfo(path=f"deploy_backup/{pk}/{fname}", hash=digest)
    )
    assert resolved.is_file()
    assert resolved.read_bytes() == body
    assert resolved.parent.name == pk
    # New writes still go to the UUID tree.
    mgr.backups_root().mkdir(parents=True, exist_ok=True)
    (mgr.backups_root() / "newer.original").write_bytes(b"new")
    assert (store / frozen / "newer.original").is_file()
    assert (store / pk / fname).is_file()
