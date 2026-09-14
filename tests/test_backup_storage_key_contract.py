"""Backup storage key is mods.mod_id — never workspace_id / published_file_id."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, persist_unified_metadata_dict
from services.identity_service import identity_create_scope, lifecycle_scope
from services.legacy_workspace_backup import (
    backup_writer_locked_to_mod_id,
    classify_legacy_workspace_buckets,
    delete_safe_legacy_workspace_buckets,
    repair_invalid_current_from_legacy,
)
from services.metadata_backup import (
    BACKUP_DIR_NAME,
    backup_root,
    mark_missing,
    prove_backup_storage_key,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import rebuild_missing_metadata_backup, sync_after_metadata_change
from services.mod_presence import attempt_recovery
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242
WORKSHOP = "3691316854"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "storage_key.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    DatabaseManager.reset_instance()


def _valid_payload(*, frozen: str, workshop: str, title: str) -> dict:
    return {
        "internal_id": frozen,
        "title": title,
        "display_name": title,
        "source_type": "steam",
        "platform": "steam",
        "source_url": f"https://example.test/{workshop}",
        "workspace_id": workshop,
        "external_id": workshop,
        "published_file_id": workshop,
    }


def _write_current_backup(pk: str, payload: dict, *, cover: bytes = b"LIVECOVER") -> Path:
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (dest / "cover.jpg").write_bytes(cover)
    return dest


def _write_legacy_bucket(
    root: Path,
    name: str,
    payload: dict,
    *,
    cover: bytes = b"LIVECOVER",
    index: str | None = None,
) -> Path:
    bucket = root / name
    bucket.mkdir(parents=True, exist_ok=True)
    (bucket / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    if cover is not None:
        (bucket / "cover.jpg").write_bytes(cover)
    if index is not None:
        offline = bucket / "offline"
        offline.mkdir(exist_ok=True)
        (offline / "index.html").write_text(index, encoding="utf-8")
    return bucket


def _seed_reminted(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    workshop_id: str = WORKSHOP,
    title: str = "Reminted",
) -> tuple[Path, str, str]:
    with lifecycle_scope("import"):
        with identity_create_scope():
            pk = str(db.allocate_mod_id())
    frozen = str(uuid.uuid4())
    folder = tmp_path / "mod" / "GameA" / title
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=workshop_id,
        workspace_id=workshop_id,
        app_id=APP_ID,
        game_name="GameA",
        extra=_valid_payload(frozen=frozen, workshop=workshop_id, title=title),
    )
    db.update_mod_identity_fields(
        pk,
        internal_id=frozen,
        workspace_id=workshop_id,
        platform=PLATFORM_STEAM,
        source_type="steam",
        external_id=workshop_id,
        source_url=f"https://example.test/{workshop_id}",
        last_known_path=str(folder),
        folder_present=True,
        title=title,
        app_id=APP_ID,
    )
    bind_managed_path(db, pk, folder, game_name="GameA", title=title)
    return folder, pk, frozen


def test_entity_internal_id_does_not_fallback_to_published_file_id() -> None:
    meta = ModMetadata(published_file_id=WORKSHOP, title="Stub")
    assert meta.entity_internal_id() == ""


def test_prove_rejects_workspace_id_that_is_not_a_pk(db: DatabaseManager) -> None:
    assert prove_backup_storage_key(WORKSHOP) == ""
    assert db.get_mod(WORKSHOP) is None


def test_writer_lock_rejects_published_file_id_fallback(db: DatabaseManager) -> None:
    gate = backup_writer_locked_to_mod_id()
    assert gate["locked"] is True
    assert gate["runtime_reference_risk"] is False


def test_snapshot_workshop_hint_writes_mod_id_not_workspace(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed_reminted(db, tmp_path)
    snap = snapshot_from_mod_folder(folder, owner_mod_id=WORKSHOP)
    assert snap is not None
    assert snap.mod_id == pk
    assert (backup_root(pk) / "metadata.json").is_file()
    assert not (backup_root(WORKSHOP)).exists()
    assert backup_root(pk).name == pk
    assert pk != WORKSHOP


def test_unresolved_entity_does_not_write_backup(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "GameA" / "Orphan"
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id="",
        title="Orphan",
        external_id=WORKSHOP,
        workspace_id=WORKSHOP,
        app_id=APP_ID,
        extra=_valid_payload(frozen="", workshop=WORKSHOP, title="Orphan"),
    )
    snap = snapshot_from_mod_folder(folder, owner_mod_id=WORKSHOP)
    assert snap is None
    assert not backup_root(WORKSHOP).exists()
    sync_after_metadata_change(WORKSHOP, folder, "edit", wait=True)
    assert not backup_root(WORKSHOP).exists()


def test_sync_empty_dto_internal_id_uses_frozen_not_workshop(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed_reminted(db, tmp_path)
    persist_unified_metadata_dict(
        folder,
        {
            **_valid_payload(frozen=frozen, workshop=WORKSHOP, title="Edited"),
            "title": "Edited",
        },
        sync_backup=True,
        sync_reason="edit",
    )
    sync_after_metadata_change("", folder, "edit", wait=True)
    assert (backup_root(pk) / "metadata.json").is_file()
    assert not backup_root(WORKSHOP).exists()


def test_workspace_equals_mod_id_is_not_legacy(db: DatabaseManager, tmp_path: Path) -> None:
    """Current backup key is mods.mod_id — never treat that bucket as legacy."""
    from core.paths import data_dir

    steam_ws = "3752077777"
    with lifecycle_scope("import"):
        with identity_create_scope():
            from services.identity_service import create_mod_identity

            created = create_mod_identity(
                db,
                platform=PLATFORM_STEAM,
                external_id=steam_ws,
                workshop_id=steam_ws,
                title="SameDigits",
                app_id=APP_ID,
                game_name="GameA",
            )
    pk = str(created.mod_id)
    assert pk.isdigit() and pk != steam_ws
    frozen = str(created.internal_id or "")
    payload = _valid_payload(frozen=frozen, workshop=steam_ws, title="SameDigits")
    _write_current_backup(pk, payload)
    root = data_dir() / BACKUP_DIR_NAME
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    ids = {row["legacy_bucket"] for row in classified["safe_to_delete"]}
    blocked = {row["legacy_bucket"] for row in classified["blocked"]}
    assert pk not in ids
    assert pk not in blocked


def test_safe_legacy_workspace_bucket_is_deleted(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir

    folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    _write_current_backup(pk, payload)
    root = data_dir() / BACKUP_DIR_NAME
    stale = dict(payload)
    stale["internal_id"] = str(uuid.uuid4())
    legacy = _write_legacy_bucket(root, WORKSHOP, stale)
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["writer"]["locked"] is True
    assert {row["legacy_bucket"] for row in classified["safe_to_delete"]} == {WORKSHOP}
    assert classified["safe_to_delete"][0]["current_mod_id"] == pk
    result = delete_safe_legacy_workspace_buckets(classified, dry_run=False)
    assert result["deleted"] == 1
    assert not legacy.exists()
    assert (backup_root(pk) / "metadata.json").is_file()
    assert db.get_mod(pk) is not None
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_unique_legacy_cover_is_retained(db: DatabaseManager, tmp_path: Path) -> None:
    from core.paths import data_dir

    folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    _write_current_backup(pk, payload, cover=b"CURRENT")
    root = data_dir() / BACKUP_DIR_NAME
    _write_legacy_bucket(root, WORKSHOP, payload, cover=b"UNIQUE-COVER-BYTES")
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["safe_to_delete"] == []
    assert classified["unique_data"][0]["legacy_bucket"] == WORKSHOP


def test_no_entity_orphan_is_not_deleted(db: DatabaseManager, tmp_path: Path) -> None:
    from core.paths import data_dir

    root = data_dir() / BACKUP_DIR_NAME
    payload = _valid_payload(
        frozen=str(uuid.uuid4()), workshop="1112223334", title="Ghost"
    )
    bucket = _write_legacy_bucket(root, "1112223334", payload)
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["safe_to_delete"] == []
    assert classified["no_current_entity"][0]["category"] in {
        "recovery_valuable",
        "unknown",
    }
    result = delete_safe_legacy_workspace_buckets(classified, dry_run=False)
    assert result["deleted"] == 0
    assert bucket.exists()


def test_multiple_entities_same_workspace_retained(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir

    folder, pk, frozen = _seed_reminted(db, tmp_path)
    with lifecycle_scope("import"):
        with identity_create_scope():
            pk2 = str(db.allocate_mod_id())
    frozen2 = str(uuid.uuid4())
    db.upsert_game(GameInfo(app_id=9999, name="GameB", folder_name="GameB"))
    db.update_mod_identity_fields(
        pk2,
        internal_id=frozen2,
        workspace_id=WORKSHOP,
        platform=PLATFORM_STEAM,
        source_type="steam",
        external_id=WORKSHOP,
        title="OtherGame",
        app_id=9999,
    )
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    _write_current_backup(pk, payload)
    root = data_dir() / BACKUP_DIR_NAME
    _write_legacy_bucket(root, WORKSHOP, payload)
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["safe_to_delete"] == []
    assert classified["multiple_entity_candidates"][0]["legacy_bucket"] == WORKSHOP


def test_lifecycle_never_creates_workspace_id_bucket(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed_reminted(db, tmp_path)
    info = folder / INFO_DIR_NAME
    (info / "cover.jpg").write_bytes(b"\xff\xd8cover")
    offline = info / "offline"
    offline.mkdir(exist_ok=True)
    (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")

    persist_unified_metadata_dict(
        folder,
        {**_valid_payload(frozen=frozen, workshop=WORKSHOP, title="Edited"), "title": "Edited"},
        sync_reason="edit",
    )
    sync_after_metadata_change(WORKSHOP, folder, "cover_change", wait=True)
    sync_after_metadata_change(pk, folder, "offline_change", wait=True)
    sync_after_metadata_change(pk, folder, "sync", wait=True)
    rebuild_missing_metadata_backup(library_root=tmp_path / "mod")
    assert (backup_root(pk) / "metadata.json").is_file()
    assert list(backup_root(pk).glob("cover.*"))
    assert (backup_root(pk) / "offline" / "index.html").is_file()
    assert not backup_root(WORKSHOP).exists()

    mark_missing(pk)
    attempt_recovery(pk, library_root=tmp_path / "mod")
    sync_after_metadata_change(pk, folder, "restore", wait=True)
    assert not backup_root(WORKSHOP).exists()
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen
    assert db.get_mod(pk) is not None


def test_old_pk_bucket_pairs_via_metadata_workspace(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir

    _folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    _write_current_backup(pk, payload)
    root = data_dir() / BACKUP_DIR_NAME
    leftover = _write_legacy_bucket(root, "777001", payload)
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    safe = {row["legacy_bucket"] for row in classified["safe_to_delete"]}
    assert leftover.name in safe
    row = next(r for r in classified["safe_to_delete"] if r["legacy_bucket"] == leftover.name)
    assert row["current_mod_id"] == pk
    assert row.get("pair_via") in {"metadata_workspace_id", "metadata_internal_id"}


def test_repair_invalid_current_cover_from_legacy(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir

    _folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    payload["cover_path"] = "cover.jpg"
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    root = data_dir() / BACKUP_DIR_NAME
    _write_legacy_bucket(root, WORKSHOP, payload, cover=b"FIXEDCOVER")
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["current_backup_invalid"]
    assert classified["current_backup_invalid"][0]["current_mod_id"] == pk
    repair = repair_invalid_current_from_legacy(classified, dry_run=False)
    assert repair["failed_count"] == 0
    assert repair["repaired_count"] == 1
    assert list(dest.glob("cover.*"))
    after = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert after["counts"]["current_backup_invalid"] == 0
    assert {row["legacy_bucket"] for row in after["safe_to_delete"]} == {WORKSHOP}


def test_other_and_nexus_backup_may_omit_source_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir
    from services.metadata_backup_validator import status_from_validation, validate_backup

    folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = {
        "internal_id": frozen,
        "title": "LocalMod",
        "display_name": "LocalMod",
        "source_type": "other",
        "workspace_id": "17863417826512025",
        "url": "",
    }
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    (dest / "cover.jpg").write_bytes(b"LOCALCOVER")
    result = validate_backup(pk)
    assert "missing source_url" not in result["issues"]
    assert result["metadata_ok"] is True
    assert status_from_validation(result) in {"complete", "partial"}
    del folder, data_dir


def test_steam_backup_still_requires_source_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.metadata_backup_validator import validate_backup

    _folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Steam")
    payload.pop("source_url", None)
    payload.pop("url", None)
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    result = validate_backup(pk)
    assert "missing source_url" in result["issues"]
    assert result["metadata_ok"] is False


def test_other_empty_url_current_backup_pairs_legacy_safe(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir
    from services.legacy_backup_finalize import finalize_leftover_legacy_buckets
    from services.metadata_backup_validator import status_from_validation, validate_backup

    _folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = {
        "internal_id": frozen,
        "title": "LocalMod",
        "display_name": "LocalMod",
        "source_type": "other",
        "platform": "other",
        "workspace_id": WORKSHOP,
        "external_id": WORKSHOP,
        "url": "",
    }
    dest = _write_current_backup(pk, payload)
    root = data_dir() / BACKUP_DIR_NAME
    leftover = _write_legacy_bucket(root, WORKSHOP, payload)
    result = validate_backup(pk)
    assert "missing source_url" not in result["issues"]
    assert result["metadata_ok"] is True
    assert status_from_validation(result) == "complete"
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["counts"]["current_backup_invalid"] == 0
    safe = {row["legacy_bucket"] for row in classified["safe_to_delete"]}
    assert leftover.name in safe
    out = finalize_leftover_legacy_buckets(
        db=db, dry_run=False, backup_root_path=root
    )
    assert out["counts"]["after_invalid"] == 0
    assert not leftover.exists()
    assert dest.is_dir()


def test_unique_legacy_cover_migrates_then_deletes(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.paths import data_dir
    from services.legacy_backup_finalize import finalize_leftover_legacy_buckets

    _folder, pk, frozen = _seed_reminted(db, tmp_path)
    payload = _valid_payload(frozen=frozen, workshop=WORKSHOP, title="Reminted")
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    root = data_dir() / BACKUP_DIR_NAME
    leftover = _write_legacy_bucket(root, WORKSHOP, payload, cover=b"ONLYCOVER")
    classified = classify_legacy_workspace_buckets(backup_root_path=root, db=db)
    assert classified["unique_data"]
    out = finalize_leftover_legacy_buckets(
        db=db, dry_run=False, backup_root_path=root
    )
    assert list(dest.glob("cover.*"))
    assert not leftover.exists()
    assert out["counts"]["after_invalid"] == 0
