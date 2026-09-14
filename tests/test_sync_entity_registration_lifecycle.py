"""Sync lifecycle: FS-first materialize, then Identity Service bind.

CONTRACT (intentional, not an Import clone)
-------------------------------------------
Sync discovers Steam Workshop folders that already exist on disk, copies them
into the managed library, then binds/creates Internal entities::

    scan source FS
        → copy / skip_existing (managed folder)
        → sidecar may hold Steam workspace / external ids (not Internal PK)
        → Identity Service create/bind
        → Database + ``.info/entity_key`` proof
        → Library

Import is Identity-first because the user is creating a new entity, then
materializing under that PK. Sync must not mint a Library row for a Workshop
folder that never landed on disk.

Allowed without Internal identity: discovered source, copied/skipped managed
folder, Steam platform sidecar fields. Forbidden: forging ``mods.mod_id``
from a folder name or Workshop ID inside Sync itself.

Failure / retry: copy failure → no entity. Identity failure → folder may
remain; next Sync rematches ``(platform, app_id, workspace_id)`` and must
not create a second row. Reconcile does not create.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.mod_identity import extract_workspace_id, resolve_existing_mod_id
from services.mod_metadata_resolver import list_visible_mods
from services.sync import ModSyncService, SyncOptions


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "sync_entity.db")
    yield manager
    DatabaseManager.reset_instance()


def _workshop_mod(root: Path, wid: str, *, with_file: bool = True) -> Path:
    folder = root / wid
    folder.mkdir(parents=True)
    if with_file:
        (folder / "mod.pak").write_bytes(b"x")
    return folder


def _managed_without_entity(
    library: Path, game: str, wid: str, *, title: str = ""
) -> Path:
    name = title or f"Unknown_Mod_{wid}"
    folder = library / game / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "payload.pak").write_bytes(b"x")
    # Intentionally incomplete sidecar (historical sync): workshop id only.
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{wid}","title":"{name}","app_id":289070}}',
        encoding="utf-8",
    )
    return folder


def _client() -> MagicMock:
    client = MagicMock()
    client.get_details_batch.return_value = []
    client.refresh_details.return_value = []
    client.resolve_game_names.return_value = None
    return client


def test_extract_workspace_id_from_unknown_mod_name() -> None:
    # Directory names never participate in identity.
    assert extract_workspace_id(folder_name="Unknown_Mod_2314657561") == ""
    assert extract_workspace_id(title="Unknown Mod 2314657561") == "2314657561"
    assert extract_workspace_id(legacy_token="2314657561") == "2314657561"
    assert (
        extract_workspace_id(
            source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=2314657561"
        )
        == "2314657561"
    )


def test_skip_existing_still_registers_missing_entities(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """325 folders / 267 entities regression: skip path must register entities."""
    from core.game_info import GameInfo
    from services.identity_service import create_mod_identity

    workshop = tmp_path / "workshop" / "289070"
    library = tmp_path / "mod"
    game = "Civilization VI"
    db.upsert_game(GameInfo(app_id=289070, name=game, folder_name=game))
    # Scale mirrors Civ6 case: 325 source / 58 historically missing entities.
    source_n = 325
    pre_registered = 267
    ids = [str(10_000_000 + i) for i in range(source_n)]
    for wid in ids:
        _workshop_mod(workshop, wid)
        _managed_without_entity(library, game, wid)

    for wid in ids[:pre_registered]:
        create_mod_identity(
            db,
            platform="steam",
            workshop_id=wid,
            external_id=wid,
            source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
            title=f"M{wid}",
            app_id=289070,
            game_name=game,
            operation="import",
        )
        db.update_mod_identity_fields(
            wid,
            folder_present=True,
            last_known_path=str(library / game / f"Unknown_Mod_{wid}"),
        )

    svc = ModSyncService(
        workshop, library, client=_client(), archiver=MagicMock()
    )
    result = svc.sync(
        SyncOptions(
            skip_existing=True,
            download_covers=False,
            archive_pages=False,
            recursive_scan=False,
        )
    )

    assert result.source_count == source_n
    assert result.materialized_count == source_n
    assert result.identity_success_count == source_n
    assert result.entity_created_count == source_n - pre_registered
    assert result.library_count == source_n
    assert not result.registration_failed

    visible = list_visible_mods(library, game)
    assert len(visible) == source_n


def test_unknown_mod_folder_registers_via_workspace_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Unknown_Mod_<id> is not 'no identity' — Workspace ID binds the entity."""
    from core.game_info import GameInfo

    workshop = tmp_path / "workshop" / "289070"
    library = tmp_path / "mod"
    game = "Civilization VI"
    db.upsert_game(GameInfo(app_id=289070, name=game, folder_name=game))
    wid = "2314657561"
    _workshop_mod(workshop, wid)
    folder = _managed_without_entity(library, game, wid)

    svc = ModSyncService(
        workshop, library, client=_client(), archiver=MagicMock()
    )
    result = svc.sync(
        SyncOptions(skip_existing=True, download_covers=False, recursive_scan=False)
    )
    assert result.source_count == 1
    assert result.materialized_count == 1
    assert result.identity_success_count == 1
    assert result.entity_created_count == 1
    assert result.library_count == 1
    assert not result.registration_failed

    hit = db.find_mod_for_registration("steam", 289070, wid)
    assert hit is not None
    assert str(hit.workspace_id) == wid
    sidecar = {
        "internal_id": str(hit.mod_id),
    }
    assert resolve_existing_mod_id(sidecar, db=db) == str(hit.mod_id)
    assert len(list_visible_mods(library, game)) == 1


def test_sync_pipeline_source_has_entity_registration_phase() -> None:
    src = inspect.getsource(ModSyncService._sync_body)
    assert "_register_synced_entities" in src
    # Even when nothing needs copy, registration still runs.
    assert "Files up to date — registering Mod entities" in src
    # Must not treat sync as "copy only".
    assert "IDENTITY" in inspect.getsource(ModSyncService._register_synced_entities).upper() or (
        "identity" in inspect.getsource(ModSyncService._register_synced_entities)
    )


def test_sync_stamps_steam_identity_on_metadata(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.game_info import GameInfo

    workshop = tmp_path / "workshop" / "289070"
    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=289070, name="Civ6", folder_name="Civ6"))
    wid = "200001"
    _workshop_mod(workshop, wid)
    folder = _managed_without_entity(library, "Civ6", wid)

    svc = ModSyncService(workshop, library, client=_client(), archiver=MagicMock())
    result = svc.sync(
        SyncOptions(skip_existing=True, download_covers=False, recursive_scan=False)
    )
    assert result.library_count == 1
    meta = ModFileManager(library).load_metadata(folder)
    assert meta is not None
    assert meta.source_type == "steam"
    assert "id=200001" in (meta.url or "")


def test_create_mod_identity_unknown_title_with_embedded_workspace(
    db: DatabaseManager,
) -> None:
    from core.game_info import GameInfo
    from services.identity_service import create_mod_identity

    db.upsert_game(GameInfo(app_id=289070, name="Civ6", folder_name="Civ6"))
    out = create_mod_identity(
        db,
        platform="steam",
        title="Unknown_Mod_9876543210",
        app_id=289070,
        game_name="Civ6",
        operation="sync",
    )
    assert str(out.mod_id).isdigit()
    info = db.get_mod_display_info(out.mod_id)
    assert info is not None
    assert str(info.workspace_id) == "9876543210"


def _count_mods(db: DatabaseManager) -> int:
    return int(db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"])


def test_copy_failure_does_not_mint_entity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy never landed → no Internal entity (FS-first: do not mint first)."""
    from core.game_info import GameInfo
    from services.file_ops import ModFileManager

    workshop = tmp_path / "workshop" / "289070"
    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=289070, name="Civ6", folder_name="Civ6"))
    wid = "310001"
    src = _workshop_mod(workshop, wid)
    meta = ModMetadata(
        published_file_id=wid,
        title="CopyFail",
        app_id=289070,
        game_name="Civ6",
        source_path=str(src),
    )
    client = _client()
    client.get_details_batch.return_value = [meta]
    client.resolve_game_names.return_value = None

    def _boom(self, metadata, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(ModFileManager, "copy_mod", _boom)
    svc = ModSyncService(workshop, library, client=client, archiver=MagicMock())
    result = svc.sync(
        SyncOptions(skip_existing=True, download_covers=False, recursive_scan=False)
    )
    assert result.failed
    assert _count_mods(db) == 0
    assert result.library_count == 0
    assert db.get_mod_display_info(wid) is None


def test_identity_failure_leaves_folder_retry_binds_once(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity fail keeps managed FS; retry rematches one entity, no duplicate."""
    import json

    from core.game_info import GameInfo
    from services.identity_service import create_mod_identity as real_create
    from services.mod_identity import read_internal_id

    workshop = tmp_path / "workshop" / "289070"
    library = tmp_path / "mod"
    game = "Civilization VI"
    db.upsert_game(GameInfo(app_id=289070, name=game, folder_name=game))
    wid = "310002"
    _workshop_mod(workshop, wid)
    folder = _managed_without_entity(library, game, wid)

    boom = {"n": 0}

    def _once(*args, **kwargs):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("identity create failed")
        return real_create(*args, **kwargs)

    monkeypatch.setattr("services.identity_service.create_mod_identity", _once)
    svc = ModSyncService(
        workshop, library, client=_client(), archiver=MagicMock()
    )
    first = svc.sync(
        SyncOptions(skip_existing=True, download_covers=False, recursive_scan=False)
    )
    assert folder.is_dir()
    assert _count_mods(db) == 0
    assert first.library_count == 0
    assert first.registration_failed
    raw = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert str(raw.get("workspace_id") or raw.get("published_file_id") or "") == wid
    assert not read_internal_id(raw)

    monkeypatch.setattr(
        "services.identity_service.create_mod_identity", real_create
    )
    second = svc.sync(
        SyncOptions(skip_existing=True, download_covers=False, recursive_scan=False)
    )
    assert second.library_count == 1
    assert second.entity_created_count == 1
    assert not second.registration_failed
    assert _count_mods(db) == 1
    info = db.get_mod_display_info(
        db.find_mod_for_registration("steam", 289070, wid).mod_id
    )
    assert info is not None
    assert str(info.workspace_id) == wid
    assert str(info.mod_id).isdigit()
    proof = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    # entity_key value == Entity.internal_id (not mods.mod_id PK).
    row = db.get_mod_backup_row(str(info.mod_id)) or {}
    frozen = str(row.get("internal_id") or "")
    assert frozen
    assert read_internal_id(proof) == frozen
    assert proof.get("internal_id") == frozen
    assert "entity_key" not in proof
    assert str(info.workspace_id) == wid
    assert str(info.mod_id).isdigit()
    assert frozen != str(info.mod_id)
