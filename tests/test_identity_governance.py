"""P0/P1 identity governance regression tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, IdentityIntegrityError
from core.mod_platform import PLATFORM_MODIO, PLATFORM_NEXUS, is_internal_mod_id
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_identity_authority import (
    safe_workspace_id_for_deploy,
    sanitize_platform_external_id,
)
from services.mod_identity_repair import (
    apply_repair_plan,
    build_repair_plan,
    elect_canonical,
    repair_mod_library_identity,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "gov.db")
    yield manager
    DatabaseManager.reset_instance()


def test_allocate_mod_id_is_atomic(db: DatabaseManager) -> None:
    a = db.allocate_mod_id()
    b = db.allocate_mod_id()
    assert a != b
    assert b == a + 1


def test_internal_id_never_becomes_external_id() -> None:
    mid = "9000000000003410"
    assert sanitize_platform_external_id(PLATFORM_MODIO, mid, mod_id=mid) == ""
    assert sanitize_platform_external_id(PLATFORM_MODIO, "4503767", mod_id=mid) == "4503767"


def test_internal_id_never_becomes_workspace_id() -> None:
    mid = "9000000000003410"
    assert (
        safe_workspace_id_for_deploy(
            platform=PLATFORM_MODIO,
            workspace_id=mid,
            mod_id=mid,
        )
        == ""
    )
    assert (
        safe_workspace_id_for_deploy(
            platform=PLATFORM_MODIO,
            workspace_id="",
            mod_id=mid,
        )
        == ""
    )


def test_deploy_never_uses_internal_mod_id_as_workspace_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.game_info import GameInfo

    db.upsert_game(GameInfo(app_id=1086940, name="BG3", folder_name="BG3"))
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url="https://mod.io/g/baldursgate3/m/x",
        workspace_id="",
        app_id=1086940,
        last_known_path=str(tmp_path / "m"),
    )
    # Pollute workspace in raw SQL to simulate old bug state, then deploy resolve.
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET workspace_id=? WHERE mod_id=?",
            (mid, int(mid)),
        )
        db._conn.commit()
    ws = safe_workspace_id_for_deploy(
        platform=PLATFORM_MODIO,
        workspace_id=mid,
        mod_id=mid,
        source_url="https://mod.io/g/baldursgate3/m/x",
    )
    assert ws == ""
    assert not is_internal_mod_id(ws) if ws else True


def test_same_source_url_never_creates_duplicate_entity_after_repair(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    url = "https://www.nexusmods.com/stardewvalley/mods/44639"
    a = str(db.allocate_mod_id())
    b = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(a),
        platform=PLATFORM_NEXUS,
        external_id="JP title junk",
        source_url=url,
        folder_present=True,
    )
    db.update_mod_identity_fields(
        int(b),
        platform=PLATFORM_NEXUS,
        external_id="44639",
        source_url=url,
        folder_present=True,
    )
    plan = build_repair_plan(db, library)
    retire = [x for x in plan.actions if x.action == "retire_duplicate_entity"]
    assert retire
    applied = apply_repair_plan(db, library, plan, apply=True)
    assert applied.success
    remaining = []
    with db._lock:
        for mid in (a, b):
            row = db._conn.execute(
                "SELECT mod_id, external_id FROM mods WHERE mod_id=?",
                (int(mid),),
            ).fetchone()
            if row:
                remaining.append(dict(row))
    assert len(remaining) == 1
    assert remaining[0]["external_id"] == "44639"


def test_duplicate_identity_canonical_election() -> None:
    rows = [
        {
            "mod_id": "9000000000000001",
            "platform": "nexus",
            "external_id": "junk title",
            "source_url": "https://www.nexusmods.com/stardewvalley/mods/100",
            "app_id": 0,
            "folder_present": 1,
            "last_known_path": "",
        },
        {
            "mod_id": "9000000000000002",
            "platform": "nexus",
            "external_id": "100",
            "source_url": "https://www.nexusmods.com/stardewvalley/mods/100",
            "app_id": 413150,
            "folder_present": 1,
            "last_known_path": "",
        },
    ]
    canonical, dups = elect_canonical(rows)
    assert canonical.mod_id == "9000000000000002"
    assert dups[0].mod_id == "9000000000000001"


def test_repair_is_idempotent(db: DatabaseManager, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    library = tmp_path / "mod"
    mid = str(db.allocate_mod_id())
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        external_id=mid,
        source_url="https://mod.io/g/baldursgate3/m/super-skip-ship-sss",
    )
    first = repair_mod_library_identity(library, db=db, apply=True)
    assert first.success
    second = repair_mod_library_identity(library, db=db, apply=True)
    assert second.success
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.external_id != mid


def test_unique_constraint_failure_is_not_silenced(tmp_path: Path) -> None:
    from core.game_info import GameInfo

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "uniq.db")
    db.upsert_game(GameInfo(app_id=1, name="G", folder_name="G"))
    a = db.allocate_mod_id()
    b = db.allocate_mod_id()
    db.update_mod_platform_info(
        a, platform=PLATFORM_NEXUS, external_id="999", title="A", app_id=1
    )
    # Force duplicate triple: drop UNIQUE, then copy identity onto second row.
    with db._lock:
        db._conn.execute("DROP INDEX IF EXISTS uq_mods_platform_app_external")
        db._conn.execute(
            "UPDATE mods SET platform=?, app_id=?, external_id=? WHERE mod_id=?",
            (PLATFORM_NEXUS, 1, "999", b),
        )
        db._conn.commit()
    DatabaseManager.reset_instance()
    os.environ.pop("SMM_IDENTITY_RECOVERY", None)
    with pytest.raises(IdentityIntegrityError):
        DatabaseManager.instance(tmp_path / "uniq.db")
    DatabaseManager.reset_instance()
    os.environ["SMM_IDENTITY_RECOVERY"] = "1"
    try:
        db2 = DatabaseManager.instance(tmp_path / "uniq.db")
        assert db2 is not None
    finally:
        os.environ.pop("SMM_IDENTITY_RECOVERY", None)
        DatabaseManager.reset_instance()
