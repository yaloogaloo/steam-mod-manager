"""IdentityRepairService contract — repair cannot mint or rewrite Internal ID."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import PLATFORM_NEXUS, PLATFORM_STEAM, DatabaseManager
from core.game_info import GameInfo
from services.identity_repair_service import (
    FieldRepairAction,
    IdentityRepairDetection,
    get_identity_repair_service,
)
from services.identity_service import create_mod_identity, identity_create_scope
from services.file_ops import persist_unified_metadata_dict, read_info_metadata_dict

STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "repair_svc.db")
    manager.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew Valley")
    )
    manager.upsert_game(
        GameInfo(app_id=BG3, name="Baldurs Gate 3", folder_name="Baldurs Gate 3")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_repair_does_not_create_identity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    svc = get_identity_repair_service()
    result = svc.repair(db, library, apply=True)
    assert result.success
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    src = Path(__file__).resolve().parents[1] / "services" / "identity_repair_service.py"
    text = src.read_text(encoding="utf-8")
    assert "create_mod_identity(" not in text


def test_repair_does_not_change_surviving_mod_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    with identity_create_scope():
        mod = create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id="9001",
            source_url="https://www.nexusmods.com/stardewvalley/mods/9001",
            title="Keep Me",
            app_id=STARDEW,
            game_name="Stardew Valley",
        )
    mid = str(mod.mod_id)
    # Pollute workspace to give repair something to do without retiring this row.
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET workspace_id=? WHERE mod_id=?",
            (mid, int(mid)),
        )
        db._conn.commit()
    frozen = str((db.get_mod_backup_row(mid) or {}).get("internal_id") or "")
    svc = get_identity_repair_service()
    result = svc.repair(
        db,
        library,
        apply=True,
        include_entity=False,
        include_pollution=False,
        include_field_scrubs=True,
    )
    assert result.success
    row = db.get_mod_display_info(mid)
    assert row is not None
    assert str(row.mod_id) == mid
    after = str((db.get_mod_backup_row(mid) or {}).get("internal_id") or "")
    assert frozen
    assert after == frozen
    assert after != mid


def test_workshop_id_never_enters_mod_id_via_repair(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "SteamMod"
    folder.mkdir(parents=True)
    with identity_create_scope():
        mod = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id="555666777",
            workshop_id="555666777",
            title="Steam Mod",
            app_id=BG3,
        )
    mid = str(mod.mod_id)
    db.update_mod_identity_fields(int(mid), last_known_path=str(folder), folder_present=True)
    persist_unified_metadata_dict(
        folder,
        {"published_file_id": "555666777", "title": "Steam Mod"},
        sync_backup=False,
        sync_reason="test",
    )
    # Attempt a forbidden legacy plan; validate must reject.
    bad = IdentityRepairDetection(
        field_actions=[
            FieldRepairAction(
                action="retire_duplicate_entity",
                canonical_mod_id=mid,
                duplicate_mod_id="999",
                details={
                    "published_file_id": mid,
                    "filesystem_policy": "retain_folders_bind_to_canonical",
                },
            )
        ]
    )
    svc = get_identity_repair_service()
    validation = svc.validate(bad)
    assert validation.ok is False
    result = svc.repair(db, library, apply=True, detection=bad)
    assert result.success is False
    meta = read_info_metadata_dict(folder) or {}
    assert str(meta.get("published_file_id") or "") == "555666777"
    assert str(meta.get("published_file_id") or "") != mid or mid == "555666777"
    # Even when digits coincide historically, repair must not rewrite PFI to Internal.
    src = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "identity_repair_service.py"
    ).read_text(encoding="utf-8")
    assert 'meta["published_file_id"] = canonical_mod_id' not in src
    assert "published_file_id\"] = canonical" not in src


def test_workspace_id_repair_allowed(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    a = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        title="Sit This One Out 2",
        app_id=BG3,
        game_name="Baldurs Gate 3",
    )
    with identity_create_scope(), db._lock:
        dirty = int(db.allocate_mod_id())
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=?, title=?, external_id=?,
                   source_url=?, workspace_id=?, display_name=?
            WHERE mod_id=?
            """,
            (
                PLATFORM_NEXUS,
                STARDEW,
                "Train Station",
                "6183",
                "https://www.nexusmods.com/stardewvalley/mods/6183",
                str(a.workspace_id or "6183"),
                "Train Station",
                dirty,
            ),
        )
        db._conn.commit()
    svc = get_identity_repair_service()
    result = svc.repair(
        db,
        library,
        apply=True,
        include_entity=False,
        include_field_scrubs=False,
        include_pollution=True,
    )
    assert result.success
    again = svc.detect(db, library)
    assert len(again.pollution.cross_game_workspace) == 0  # type: ignore[union-attr]


def test_detect_has_no_side_effects(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    with identity_create_scope():
        create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id="42",
            source_url="https://www.nexusmods.com/stardewvalley/mods/42",
            title="X",
            app_id=STARDEW,
            game_name="Stardew Valley",
        )
    snap = db._conn.execute(
        "SELECT mod_id, workspace_id, external_id, platform, app_id FROM mods "
        "ORDER BY mod_id"
    ).fetchall()
    before = [dict(r) for r in snap]
    svc = get_identity_repair_service()
    svc.detect(db, library)
    after = [
        dict(r)
        for r in db._conn.execute(
            "SELECT mod_id, workspace_id, external_id, platform, app_id FROM mods "
            "ORDER BY mod_id"
        ).fetchall()
    ]
    assert after == before


def test_repair_is_idempotent(db: DatabaseManager, tmp_path: Path) -> None:
    from core.mod_platform import PLATFORM_MODIO

    library = tmp_path / "mod"
    library.mkdir()
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        external_id=mid,
        source_url="https://mod.io/g/baldursgate3/m/super-skip-ship-sss",
        app_id=BG3,
    )
    svc = get_identity_repair_service()
    first = svc.repair(
        db,
        library,
        apply=True,
        include_entity=False,
        include_pollution=False,
        include_field_scrubs=True,
    )
    assert first.success
    info1 = db.get_mod_display_info(mid)
    second = svc.repair(
        db,
        library,
        apply=True,
        include_entity=False,
        include_pollution=False,
        include_field_scrubs=True,
    )
    assert second.success
    info2 = db.get_mod_display_info(mid)
    assert info1 is not None and info2 is not None
    assert str(info1.external_id) == str(info2.external_id)
    assert str(info1.mod_id) == str(info2.mod_id) == mid
