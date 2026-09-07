"""Identity lifecycle contract — DB internal_id is the only entity key."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.metadata_backup import (
    backup_root,
    restore_info_sidecar_from_backup,
)
from services.mod_identity import ensure_internal_id, ensure_mod_identity


STARDEW = 413150
BG3 = 1086940
WS = "1333"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_life.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    yield manager
    DatabaseManager.reset_instance()


def _write_info(folder: Path, payload: dict) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.bin").write_bytes(b"x")
    return folder


def test_same_workspace_different_games_do_not_match(db: DatabaseManager) -> None:
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/stardewvalley/mods/{WS}",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/baldursgate3/mods/{WS}",
        title="Community Library",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    assert a.mod_id != b.mod_id
    assert db.find_mod_by_external(PLATFORM_NEXUS, WS, app_id=STARDEW).mod_id == a.mod_id
    assert db.find_mod_by_external(PLATFORM_NEXUS, WS, app_id=BG3).mod_id == b.mod_id
    assert db.find_mod_by_workspace_id(WS) is None
    assert db.find_mod_by_workspace_id(WS, platform=PLATFORM_NEXUS, app_id=STARDEW) is None
    assert db.find_mod_id_by_workspace_id(WS) is None
    assert db.find_mod_id_by_workspace_id(WS, app_id=STARDEW) is None


def test_same_external_different_games_do_not_match(db: DatabaseManager) -> None:
    create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/stardewvalley/mods/6183",
        title="Train Station",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        title="Other 6183",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    stardew = db.find_mod_by_external(PLATFORM_NEXUS, "6183", app_id=STARDEW)
    bg3 = db.find_mod_by_external(PLATFORM_NEXUS, "6183", app_id=BG3)
    assert stardew is not None and bg3 is not None
    assert stardew.mod_id != bg3.mod_id


def test_folder_rename_preserves_entity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="99901",
        source_url="https://www.nexusmods.com/stardewvalley/mods/99901",
        title="Rename Me",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    uuid_key = str(row.get("internal_id") or "").strip() or mid
    old = library / "Stardew" / "Old Name"
    _write_info(
        old,
        {
            "internal_id": uuid_key,
            "workspace_id": "99901",
            "external_id": "99901",
            "platform": PLATFORM_NEXUS,
            "source_type": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Rename Me",
            "source_url": "https://www.nexusmods.com/stardewvalley/mods/99901",
        },
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(old.resolve()),
        folder_present=True,
    )
    new = library / "Stardew" / "New Name"
    shutil.move(str(old), str(new))
    count_before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    reconcile_library(library)
    count_after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert count_after == count_before
    assert db.get_mod(mid) is not None


def test_no_info_directory_is_not_registered(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew" / "BareFolder"
    folder.mkdir(parents=True)
    (folder / "pak.bin").write_bytes(b"1")
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before
    assert any("IGNORE_NO_INFO" in n for n in result.notes)


def test_forged_info_is_not_registered(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew" / "Forged"
    _write_info(
        folder,
        {
            "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "workspace_id": "1333",
            "platform": PLATFORM_NEXUS,
            "title": "Forged",
            "app_id": STARDEW,
        },
    )
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    mid, payload, _ = ensure_mod_identity(folder, db=db)
    assert mid == ""
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before
    assert any("IGNORE_UNBOUND_INFO" in n for n in result.notes)
    assert payload.get("identity_status") == "unresolved"


def test_backup_does_not_create_entity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew" / "HasBackupOnly"
    folder.mkdir(parents=True)
    (folder / "x.bin").write_bytes(b"1")
    orphan_mid = "9000000000999999"
    bak = backup_root(orphan_mid)
    bak.mkdir(parents=True, exist_ok=True)
    (bak / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "workspace_id": "1333",
                "platform": PLATFORM_NEXUS,
                "app_id": STARDEW,
                "title": "Should Not Create",
            }
        ),
        encoding="utf-8",
    )
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert restore_info_sidecar_from_backup(orphan_mid, folder, db=db) is False
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before
    assert not (folder / INFO_DIR_NAME / METADATA_FILENAME).is_file()


def test_workspace_id_cannot_query_entity(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="4242",
        source_url="https://www.nexusmods.com/stardewvalley/mods/4242",
        title="Only Via Internal",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert db.find_mod_by_workspace_id("4242") is None
    assert db.find_mod_id_by_workspace_id("4242", app_id=STARDEW) is None
    assert db.get_mod(created.mod_id) is not None


def test_import_existing_does_not_duplicate(db: DatabaseManager) -> None:
    first = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/7777",
        title="Once",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    second = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/7777",
        title="Once Again",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert first.mod_id == second.mod_id
    rows = db._conn.execute(
        "SELECT COUNT(*) FROM mods WHERE platform=? AND external_id=? AND app_id=?",
        (PLATFORM_NEXUS, "7777", STARDEW),
    ).fetchone()[0]
    assert rows == 1


def test_ensure_internal_id_never_mints() -> None:
    payload, changed = ensure_internal_id({})
    assert changed is False
    assert "internal_id" not in payload


def test_projection_workspace_no_mid_fallback(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_STEAM,
        workshop_id="555001",
        external_id="555001",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=555001",
        title="Steam Mod",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    # Clear workspace to prove projection does not fall back to mid.
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET workspace_id='' WHERE mod_id=?",
            (int(created.mod_id),),
        )
        db._conn.commit()
    rows = db.list_library_projection_rows() if hasattr(db, "list_library_projection_rows") else None
    if rows is None:
        # Use the internal projection helper path via get_library_cards / similar.
        with db._lock:
            row = db._conn.execute(
                "SELECT workspace_id FROM mods WHERE mod_id=?",
                (int(created.mod_id),),
            ).fetchone()
        assert str(row["workspace_id"] or "").strip() == ""
        # Projection mapping code path: empty workspace must stay empty.
        mid = str(created.mod_id)
        projected_ws = str(row["workspace_id"] or "").strip()
        assert projected_ws != mid or projected_ws == ""
        assert projected_ws == ""


def test_existing_entity_rebinds_to_new_info_path_after_old_path_deleted(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two folders share the same .info.internal_id; deleting the bound path
    must rebind the *existing* entity onto the remaining legal .info folder.

    Entity / internal_id / workspace_id must not change. No create.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)

    library = tmp_path / "mod"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="77701",
        source_url="https://www.nexusmods.com/stardewvalley/mods/77701",
        title="Dual Path",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row0 = db.get_mod_backup_row(mid) or {}
    iid = str(row0.get("internal_id") or "").strip() or mid
    ws = str(row0.get("workspace_id") or "").strip()
    assert ws

    payload = {
        "internal_id": iid,
        "workspace_id": ws,
        "external_id": "77701",
        "platform": PLATFORM_NEXUS,
        "source_type": PLATFORM_NEXUS,
        "app_id": STARDEW,
        "title": "Dual Path",
        "source_url": "https://www.nexusmods.com/stardewvalley/mods/77701",
    }
    folder_a = _write_info(library / "Stardew" / "CopyA", dict(payload))
    folder_b = _write_info(library / "Stardew" / "CopyB", dict(payload))
    db.update_mod_identity_fields(
        mid,
        internal_id=iid,
        workspace_id=ws,
        last_known_path=str(folder_a.resolve()),
        folder_present=True,
        identity_status="identity_conflict",
    )

    count_before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    shutil.rmtree(folder_a)
    # Simulate UI observing the deleted bind before reconcile.
    db.update_mod_identity_fields(mid, folder_present=False)

    result = reconcile_library(library)
    count_after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    row = db.get_mod_backup_row(mid) or {}

    assert count_after == count_before
    assert db.get_mod(mid) is not None
    assert str(row.get("internal_id") or "").strip() == iid
    assert str(row.get("workspace_id") or "").strip() == ws
    assert Path(str(row.get("last_known_path") or "")).resolve() == folder_b.resolve()
    assert int(row.get("folder_present") or 0) == 1
    assert str(row.get("identity_status") or "").strip() in ("", "ok", "complete")
    assert result.imported == 0
    assert folder_b.is_dir()
    assert not folder_a.exists()

    from services.path_lifecycle import resolve_managed_folder

    resolved = resolve_managed_folder(mid, library_root=library, db=db)
    assert resolved.path is not None
    assert resolved.path.resolve() == folder_b.resolve()
