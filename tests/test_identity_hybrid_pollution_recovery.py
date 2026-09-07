"""Hybrid pollution recovery + cross-game merge regression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.identity_service import create_mod_identity
from services.importers.duplicate_check import find_duplicate_mod
from tools.identity_hybrid_pollution_recovery import (
    ACTION_SPLIT,
    apply_plan,
    build_plan,
    build_report,
)

STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "hybrid.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    manager.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3"))
    yield manager
    DatabaseManager.reset_instance()


def _info(folder: Path, payload: dict) -> None:
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")


def test_hybrid_recovery_split_restores_two_entities(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    stardew_folder = library / "Stardew Valley" / "Carry Chest"
    bg3_folder = library / "Baldurs Gate 3" / "Community Library"
    stardew_folder.mkdir(parents=True)
    bg3_folder.mkdir(parents=True)
    (stardew_folder / "pak").write_bytes(b"s")
    (bg3_folder / "pak").write_bytes(b"b")

    hybrid = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Community Library",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    mid = str(hybrid.mod_id)
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(stardew_folder.resolve()),
        folder_present=True,
        internal_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    _info(
        stardew_folder,
        {
            "internal_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "workspace_id": "1333",
            "title": "Carry Chest",
            "display_name": "Community Library",
            "url": "https://www.nexusmods.com/stardewvalley/mods/1333",
        },
    )
    _info(
        bg3_folder,
        {
            "internal_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "workspace_id": "1333",
            "title": "Community Library",
            "display_name": "Community Library",
            "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
        },
    )

    db_path = Path(db._db_path) if hasattr(db, "_db_path") else tmp_path / "hybrid.db"
    # DatabaseManager stores path on instance — resolve via sqlite file
    with db._lock:
        db_file = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])

    report = build_report(db_path=db_file, library=library)
    hybrids = [f for f in report["findings"] if f["code"] == "hybrid_entity"]
    assert hybrids, report["counts"]
    assert hybrids[0]["merged_two_real_entities"] is True

    plan = build_plan(report)
    splits = [i for i in plan["items"] if i["action"] == ACTION_SPLIT]
    assert len(splits) == 1
    splits[0]["approved"] = True

    result = apply_plan(
        plan,
        db_path=db_file,
        library=library,
        apply=True,
        confirm=True,
        rollback_dir=tmp_path / "rollback",
    )
    assert result["applied"] is True
    applied = [r for r in result["results"] if r.get("applied")]
    assert applied
    after = applied[0]["after"]
    kept = after["kept_entity"]
    created = after["created_entities"]
    assert kept["mod_id"] == mid
    assert "Carry Chest" in kept["title"]
    assert int(kept["app_id"]) == STARDEW
    assert created and not created[0].get("error")
    new_mid = created[0]["mod_id"]
    assert new_mid != mid
    assert int(created[0]["app_id"]) == BG3

    # Re-open via same manager after apply reset instance
    DatabaseManager.reset_instance()
    db2 = DatabaseManager.instance(db_file)
    stardew = db2.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=STARDEW)
    bg3 = db2.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=BG3)
    assert stardew is not None and bg3 is not None
    assert str(stardew.mod_id) != str(bg3.mod_id)
    assert str(stardew.workspace_id) == str(bg3.workspace_id) == "1333"
    assert str(stardew.external_id) == str(bg3.external_id) == "1333"
    assert "Carry" in (stardew.display_name or stardew.steam_name or "")
    assert "Community" in (bg3.display_name or bg3.steam_name or "")
    DatabaseManager.reset_instance()


def test_cross_game_same_external_id_never_merges_again(db: DatabaseManager) -> None:
    """Regression: future imports must not collapse BG3+Stardew 1333 into one row."""
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    assert str(a.mod_id) != str(b.mod_id)
    assert (
        find_duplicate_mod(
            db,
            platform=PLATFORM_NEXUS,
            external_id="1333",
            source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
            app_id=STARDEW,
        ).mod_id
        == b.mod_id
    )
    assert (
        find_duplicate_mod(
            db,
            platform=PLATFORM_NEXUS,
            external_id="1333",
            source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
            app_id=BG3,
        ).mod_id
        == a.mod_id
    )
    assert (
        find_duplicate_mod(
            db,
            platform=PLATFORM_NEXUS,
            external_id="1333",
            source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
            app_id=BG3,
        )
        is None
    )
