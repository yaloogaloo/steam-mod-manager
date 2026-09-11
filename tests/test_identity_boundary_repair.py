"""P2-2 Identity Boundary Repair — targeted Frozen-contract acceptance."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services import collection as coll
from services.deploy_paths import resolve_deploy_identity
from services.file_ops import persist_unified_metadata_dict, read_info_metadata_dict
from services.info_sidecar import apply_sidecar_to_db, ensure_registration_info_proof
from services.identity_service import (
    create_mod_identity,
    persist_identity,
    resolve_mod_pk,
)
from services.library_reconcile import reconcile_library
from services.mod_identity import read_internal_id, resolve_existing_mod_id
from services.mod_refresh import refresh_mod

STARDEW = 413150


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_boundary_repair.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    yield manager
    DatabaseManager.reset_instance()


def _create(db: DatabaseManager, *, ext: str, title: str):
    return create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=ext,
        source_url=f"https://www.nexusmods.com/stardewvalley/mods/{ext}",
        title=title,
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )


def test_new_mod_internal_id_is_not_str_mod_id(db: DatabaseManager) -> None:
    created = _create(db, ext="9101", title="Mint")
    pk = str(created.mod_id)
    proof = str(created.internal_id or "").strip()
    row = db.get_mod_backup_row(pk) or {}
    stored = str(row.get("internal_id") or "").strip()
    assert proof
    assert stored == proof
    assert stored != pk
    uuid.UUID(stored)


def test_existing_uuid_survives_persist(db: DatabaseManager) -> None:
    created = _create(db, ext="9102", title="Keep")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    persist_identity(db, pk, title="Keep", source="test", reason="no_rewrite")
    persist_identity(
        db, pk, internal_id=pk, source="test", reason="refuse_collapse"
    )
    after = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    assert after == proof
    assert after != pk


def test_internal_id_resolves_to_mod_id(db: DatabaseManager) -> None:
    created = _create(db, ext="9103", title="Resolve")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    assert resolve_mod_pk(proof, db=db) == pk
    assert db.find_mod_by_internal_id(proof) == pk


def test_collection_accepts_uuid_and_writes_pk_fk(db: DatabaseManager) -> None:
    created = _create(db, ext="9104", title="Pack")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    rec = coll.create_collection(STARDEW, "Pack", db=db)
    inserted = coll.add_mods_to_collection(rec.collection_id, [proof], db=db)
    assert inserted == 1
    members = coll.list_collection_member_ids(rec.collection_id, db=db)
    assert members == [pk]
    row = db._conn.execute(
        "SELECT mod_id FROM collection_mods WHERE collection_id = ?",
        (rec.collection_id,),
    ).fetchone()
    assert str(row["mod_id"]) == pk
    with pytest.raises(ValueError, match="invalid mod_id"):
        db.add_mods_to_collection(rec.collection_id, [proof])


def test_deploy_resolves_uuid_to_current_pk(db: DatabaseManager) -> None:
    created = _create(db, ext="9105", title="Deploy")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    assert resolve_deploy_identity(proof, db=db) == pk
    assert resolve_deploy_identity(pk, db=db) == pk


def test_pk_differing_from_uuid_still_resolves(db: DatabaseManager) -> None:
    proof = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    with db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                platform, source_url, external_id, workspace_id, internal_id,
                mod_files, updated_at
            )
            VALUES (987, ?, 'Rebuild', '', '', '', '', '', 0, ?, ?, ?, ?, ?, '{}',
                    datetime('now'))
            """,
            (
                STARDEW,
                PLATFORM_NEXUS,
                "https://www.nexusmods.com/stardewvalley/mods/9106",
                "9106",
                "9106",
                proof,
            ),
        )
        db._conn.commit()
    assert proof != "987"
    assert resolve_mod_pk(proof, db=db) == "987"
    assert resolve_deploy_identity(proof, db=db) == "987"
    rec = coll.create_collection(STARDEW, "Rebuild", db=db)
    assert coll.add_mod_to_collection(rec.collection_id, proof, db=db)
    assert coll.list_collection_member_ids(rec.collection_id, db=db) == ["987"]


def test_workspace_id_cannot_become_entity_key(db: DatabaseManager) -> None:
    created = _create(db, ext="9107", title="Workspace")
    pk = str(created.mod_id)
    ws = str(created.workspace_id)
    assert ws == "9107"
    assert db.find_mod_by_workspace_id(ws) is None
    assert db.find_mod_by_workspace_id(ws, platform=PLATFORM_NEXUS, app_id=STARDEW) is None
    assert resolve_existing_mod_id(
        {"workspace_id": ws, "app_id": STARDEW, "platform": PLATFORM_NEXUS},
        db=db,
    ) == ""
    assert resolve_mod_pk(ws, db=db) == ""
    assert resolve_mod_pk(pk, db=db) == pk


def test_existing_uuid_is_not_rewritten(db: DatabaseManager) -> None:
    created = _create(db, ext="9108", title="Stable")
    pk = str(created.mod_id)
    original = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    other = "ffffffff-ffff-4ccc-8ddd-aaaaaaaaaaaa"
    persist_identity(
        db, pk, internal_id=other, source="test", reason="refuse_rewrite"
    )
    after = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    assert after == original
    assert after != other
    assert after != pk


def test_registration_info_proof_matches_frozen_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    created = _create(db, ext="9109", title="Proof")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    folder = tmp_path / "mod" / "Stardew" / "Proof"
    folder.mkdir(parents=True)
    (folder / "content.bin").write_bytes(b"mod")
    ensure_registration_info_proof(folder, pk, db=db)
    disk = read_internal_id(read_info_metadata_dict(folder) or {})
    assert proof
    assert disk == proof
    assert disk != pk
    uuid.UUID(disk)


def test_apply_sidecar_cannot_overwrite_frozen_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    created = _create(db, ext="9110", title="Sidecar")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    folder = tmp_path / "mod" / "Stardew" / "Sidecar"
    folder.mkdir(parents=True)
    (folder / "content.bin").write_bytes(b"mod")
    persist_unified_metadata_dict(
        folder,
        {
            "internal_id": "ffffffff-ffff-4ccc-8ddd-bbbbbbbbbbbb",
            "workspace_id": "9110",
            "platform": PLATFORM_NEXUS,
            "title": "Sidecar Forged",
            "url": "https://www.nexusmods.com/stardewvalley/mods/9110",
        },
        sync_backup=False,
        sync_reason="test",
    )
    apply_sidecar_to_db(folder, mod_id=pk, db=db)
    after = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    assert after == proof
    assert after != "ffffffff-ffff-4ccc-8ddd-bbbbbbbbbbbb"
    assert after != pk


def test_refresh_offline_reconcile_preserve_frozen_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    created = _create(db, ext="9111", title="Lifecycle")
    pk = str(created.mod_id)
    proof = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    library = tmp_path / "mod"
    folder = library / "Stardew" / "Lifecycle"
    folder.mkdir(parents=True)
    (folder / "content.bin").write_bytes(b"mod")
    persist_unified_metadata_dict(
        folder,
        {
            "internal_id": proof,
            "workspace_id": "9111",
            "platform": PLATFORM_NEXUS,
            "title": "Lifecycle",
            "app_id": STARDEW,
        },
        sync_backup=False,
        sync_reason="test",
    )
    db.update_mod_identity_fields(
        pk, last_known_path=str(folder.resolve()), folder_present=True
    )
    result = refresh_mod(
        pk, folder, platform=PLATFORM_NEXUS, library_root=library, db=db
    )
    assert result.success
    db.update_mod_offline_status(pk, status="archived", provider="nexus_archive")
    reconcile_library(library)
    after = str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "")
    row = db.get_mod_backup_row(pk) or {}
    assert after == proof
    assert after != pk
    assert str(row.get("workspace_id") or "") == "9111"
    assert str(row.get("mod_id") or pk) == pk
