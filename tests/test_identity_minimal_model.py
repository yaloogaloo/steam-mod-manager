"""Identity Minimal Model — two concepts only: internal_id + workspace_id."""

from __future__ import annotations

import ast
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
from services.metadata_backup import backup_root, restore_info_sidecar_from_backup
from services.mod_identity import ensure_mod_identity, resolve_existing_mod_id

ROOT = Path(__file__).resolve().parents[1]
STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "minimal_id.db")
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


def _call_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_1_production_identity_path_avoids_find_mod_by_external_in_resolve() -> None:
    src = (ROOT / "services" / "mod_identity.py").read_text(encoding="utf-8")
    # resolve_existing_mod_id body must not call find_mod_by_external
    part = src.split("def resolve_existing_mod_id", 1)[1].split("def ensure_mod_identity", 1)[0]
    assert "find_mod_by_external" not in part
    assert "find_mod_by_workspace_id" not in part


def test_2_workspace_lookup_permanent_none(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert db.find_mod_by_workspace_id(created.workspace_id) is None
    assert db.find_mod_by_workspace_id(
        created.workspace_id, platform=PLATFORM_NEXUS, app_id=STARDEW
    ) is None
    assert db.find_mod_id_by_workspace_id(created.workspace_id) is None


def test_3_reconcile_cannot_create_entity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    _write_info(
        library / "Stardew" / "Orphan",
        {"title": "Orphan", "workspace_id": "99901", "platform": PLATFORM_NEXUS, "app_id": STARDEW},
    )
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before
    assert result.imported == 0
    src = (ROOT / "services" / "library_reconcile.py").read_text(encoding="utf-8")
    assert "create_mod_identity" not in _call_names(src)


def test_4_reconcile_cannot_workspace_bind(db: DatabaseManager, tmp_path: Path) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1401",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1401",
        title="Tractor",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    # Forged folder shares workspace only — different internal_id
    folder = _write_info(
        tmp_path / "mod" / "Stardew" / "Tractor",
        {
            "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "workspace_id": "1401",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Tractor",
        },
    )
    reconcile_library(tmp_path / "mod")
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() != folder.resolve()


def test_5_info_binds_only_via_internal_id(db: DatabaseManager, tmp_path: Path) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/777",
        title="Bind",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    iid = str(row.get("internal_id") or mid)
    folder = _write_info(
        tmp_path / "mod" / "Stardew" / "Bind",
        {
            "internal_id": iid,
            "workspace_id": "777",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Bind",
        },
    )
    bound, payload, _ = ensure_mod_identity(folder, db=db)
    assert bound == mid
    assert resolve_existing_mod_id({"workspace_id": "777", "app_id": STARDEW, "platform": PLATFORM_NEXUS}, db=db) == ""
    assert resolve_existing_mod_id({"internal_id": iid}, db=db) == mid


def test_6_backup_cannot_create_entity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)
    orphan = "9000000000999991"
    bak = backup_root(orphan)
    bak.mkdir(parents=True)
    (bak / "metadata.json").write_text(
        json.dumps({"internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "title": "No"}),
        encoding="utf-8",
    )
    folder = tmp_path / "mod" / "Stardew" / "NoCreate"
    folder.mkdir(parents=True)
    before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert restore_info_sidecar_from_backup(orphan, folder, db=db) is False
    after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after == before


def test_7_and_8_import_and_sync_are_create_entries(db: DatabaseManager) -> None:
    db.upsert_game(GameInfo(app_id=292030, name="W3", folder_name="W3"))
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1001",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1001",
        title="Imp",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_STEAM,
        external_id="2002",
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=2002",
        title="Syn",
        app_id=292030,
        game_name="W3",
        operation="sync",
        workshop_id="2002",
    )
    assert a.mod_id and b.mod_id and a.mod_id != b.mod_id
    with pytest.raises(Exception):
        create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id="1003",
            source_url="https://www.nexusmods.com/stardewvalley/mods/1003",
            title="Bad",
            app_id=STARDEW,
            game_name="Stardew",
            operation="reconcile",
        )


def test_9_same_workspace_different_app_id_two_entities(db: DatabaseManager) -> None:
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    assert a.mod_id != b.mod_id
    assert a.workspace_id == b.workspace_id == "1333"
    reg_a = db.find_mod_for_registration(PLATFORM_NEXUS, STARDEW, "1333")
    reg_b = db.find_mod_for_registration(PLATFORM_NEXUS, BG3, "1333")
    assert reg_a is not None and reg_b is not None
    assert str(reg_a.mod_id) == str(a.mod_id)
    assert str(reg_b.mod_id) == str(b.mod_id)
    assert db.find_mod_for_registration(PLATFORM_NEXUS, 0, "1333") is None


def test_10_rename_preserves_entity_ids(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="555",
        source_url="https://www.nexusmods.com/stardewvalley/mods/555",
        title="Rename",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    iid = str(row.get("internal_id") or mid)
    ws = str(row.get("workspace_id") or "555")
    old = _write_info(
        library / "Stardew" / "Old",
        {
            "internal_id": iid,
            "workspace_id": ws,
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Rename",
        },
    )
    db.update_mod_identity_fields(mid, last_known_path=str(old.resolve()), folder_present=True)
    new = library / "Stardew" / "New"
    shutil.move(str(old), str(new))
    reconcile_library(library)
    row2 = db.get_mod_backup_row(mid) or {}
    assert str(row2.get("internal_id") or "") == iid
    assert str(row2.get("workspace_id") or "") == ws
    assert Path(str(row2.get("last_known_path") or "")).resolve() == new.resolve()


def test_11_registration_api_replaces_external_identity_axis(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="8888",
        source_url="https://www.nexusmods.com/stardewvalley/mods/8888",
        title="Ext",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    # Deprecated alias must resolve via workspace_id registration, not a separate axis.
    via_reg = db.find_mod_for_registration(PLATFORM_NEXUS, STARDEW, "8888")
    via_ext = db.find_mod_by_external(PLATFORM_NEXUS, "8888", app_id=STARDEW)
    assert via_reg is not None and via_ext is not None
    assert str(via_reg.mod_id) == str(via_ext.mod_id) == str(created.mod_id)
