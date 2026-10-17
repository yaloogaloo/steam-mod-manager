"""Phase 2-A recovery plan builder is read-only and policy-correct."""

from __future__ import annotations

import json
from pathlib import Path

from tools.identity_recovery_plan import (
    CATEGORY_AUTO_SAFE,
    CATEGORY_FORBIDDEN_AUTO_FIX,
    CATEGORY_MANUAL_REVIEW,
    build_recovery_plan,
)


def test_plan_builder_classifies_and_does_not_mutate_db(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "t.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE mods (
            mod_id INTEGER PRIMARY KEY,
            app_id INTEGER,
            platform TEXT,
            external_id TEXT,
            workspace_id TEXT,
            internal_id TEXT,
            title TEXT,
            display_name TEXT,
            source_url TEXT,
            last_known_path TEXT,
            folder_present INTEGER,
            identity_status TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE deployment_record_items (
            record_id INTEGER,
            mod_id INTEGER
        )
        """
    )
    con.execute(
        "INSERT INTO mods(mod_id, app_id, platform, external_id, workspace_id, internal_id) "
        "VALUES (1, 413150, 'nexus', '1333', '1333', 'keep-me')"
    )
    con.execute("INSERT INTO deployment_record_items VALUES (10, 1)")
    con.commit()
    con.close()

    backup = tmp_path / "mod_backup"
    (backup / "orphan-bak").mkdir(parents=True)
    (backup / "orphan-bak" / "metadata.json").write_text(
        json.dumps({"internal_id": "bak-linked"}), encoding="utf-8"
    )

    report = {
        "generated_at": "t",
        "findings_count": 5,
        "db_path": str(db_path),
        "library_path": str(tmp_path / "mod"),
        "backup_path": str(backup),
        "findings": [
            {
                "code": "info_internal_id_not_in_db",
                "internal_id": "orphan-clean",
                "app_id": 0,
                "platform": "nexus",
                "external_id": "",
                "info_path": "/x/.info/metadata.json",
                "db_record": {},
                "conflict_reason": "not in db",
                "recommended_action": "IGNORE_ORPHAN_INFO",
                "extra": {"folder": "/x"},
            },
            {
                "code": "info_internal_id_not_in_db",
                "internal_id": "bak-linked",
                "app_id": 0,
                "platform": "nexus",
                "external_id": "",
                "info_path": "/y/.info/metadata.json",
                "db_record": {},
                "conflict_reason": "not in db but bak",
                "recommended_action": "IGNORE_ORPHAN_INFO",
                "extra": {},
            },
            {
                "code": "cross_game_same_external_id",
                "internal_id": "keep-me",
                "app_id": 413150,
                "platform": "nexus",
                "external_id": "1333",
                "info_path": "",
                "db_record": {"mod_id": "1", "internal_id": "keep-me"},
                "conflict_reason": "cross game",
                "recommended_action": "SPLIT",
                "extra": {},
            },
            {
                "code": "db_entity_without_valid_info",
                "internal_id": "keep-me",
                "app_id": 413150,
                "platform": "nexus",
                "external_id": "1333",
                "info_path": "",
                "db_record": {"mod_id": "1"},
                "conflict_reason": "no info",
                "recommended_action": "RESTORE",
                "extra": {},
            },
            {
                "code": "duplicate_internal_id",
                "internal_id": "dup",
                "app_id": 0,
                "platform": "nexus",
                "external_id": "9",
                "info_path": "",
                "db_record": {},
                "conflict_reason": "dup",
                "recommended_action": "DEDUP",
                "extra": {},
            },
        ],
    }

    before = db_path.read_bytes()
    plan = build_recovery_plan(report, db_path=db_path, backup_root=backup)
    after = db_path.read_bytes()
    assert before == after
    assert plan["production_mutation"] == "NONE"
    assert plan["guards"]["modifies_database"] is False
    assert plan["guards"]["calls_identity_service"] is False

    by_id = {i["internal_id"]: i for i in plan["items"]}
    assert by_id["orphan-clean"]["category"] == CATEGORY_AUTO_SAFE
    assert by_id["orphan-clean"]["auto_fix_allowed"] is True
    assert by_id["bak-linked"]["category"] == CATEGORY_MANUAL_REVIEW
    assert by_id["bak-linked"]["auto_fix_allowed"] is False
    assert by_id["keep-me"]  # last overwrite — check by finding_type instead

    cats = {(i["finding_type"], i["category"], i["auto_fix_allowed"]) for i in plan["items"]}
    assert ("info_internal_id_not_in_db", CATEGORY_AUTO_SAFE, True) in cats
    assert ("info_internal_id_not_in_db", CATEGORY_MANUAL_REVIEW, False) in cats
    assert ("cross_game_same_external_id", CATEGORY_MANUAL_REVIEW, False) in cats
    assert ("db_entity_without_valid_info", CATEGORY_MANUAL_REVIEW, False) in cats
    assert ("duplicate_internal_id", CATEGORY_FORBIDDEN_AUTO_FIX, False) in cats

    for item in plan["items"]:
        assert "risk" in item
        assert "current_data" in item
        assert "evidence" in item
        assert "recommended_action" in item
        assert "auto_fix_allowed" in item
        assert "needs_human_confirm_reason" in item


def test_plan_tool_source_forbids_identity_service_calls() -> None:
    src = (Path(__file__).resolve().parents[1] / "tools" / "identity_recovery_plan.py").read_text(
        encoding="utf-8"
    )
    assert "create_mod_identity(" not in src
    assert "find_mod_by_workspace_id(" not in src
    assert "IdentityService" not in src or "Never call IdentityService" in src
    assert "INSERT INTO mods" not in src
