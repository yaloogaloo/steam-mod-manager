"""Phase 2-B repair preview is read-only and policy-correct."""

from __future__ import annotations

import json
from pathlib import Path

from tools.identity_repair_preview import (
    ACTION_IGNORE_ORPHAN,
    ACTION_MANUAL_REVIEW,
    ACTION_RESTORE_INFO,
    ACTION_REVIEW_BACKUP,
    ACTION_SPLIT_REVIEW,
    build_repair_preview,
)


def test_preview_actions_and_no_db_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db_path.write_bytes(b"sqlite-placeholder")
    backup = tmp_path / "mod_backup"
    mid = "9001"
    (backup / mid).mkdir(parents=True)
    (backup / mid / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "uuid-1",
                "platform": "nexus",
                "app_id": 413150,
                "external_id": "1333",
                "workspace_id": "1333",
                "title": "Carry",
            }
        ),
        encoding="utf-8",
    )
    # Mismatched backup for second entity
    (backup / "9002").mkdir(parents=True)
    (backup / "9002" / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "other",
                "platform": "steam",
                "app_id": 1,
                "external_id": "9",
            }
        ),
        encoding="utf-8",
    )
    orphan_bak = backup / "orphan"
    orphan_bak.mkdir()
    (orphan_bak / "metadata.json").write_text(
        json.dumps({"internal_id": "orphan-id", "title": "x"}), encoding="utf-8"
    )

    plan = {
        "generated_at": "t",
        "findings_planned": 5,
        "db_path": str(db_path),
        "backup_path": str(backup),
        "items": [
            {
                "plan_index": 0,
                "finding_type": "info_internal_id_not_in_db",
                "internal_id": "orphan-info",
                "category": "AUTO_SAFE",
                "risk": "low",
                "current_data": {
                    "db_record": {},
                    "info_path": "",
                    "extra": {"folder": str(tmp_path / "mod" / "g" / "o")},
                },
                "evidence": {},
                "needs_human_confirm_reason": "x",
            },
            {
                "plan_index": 1,
                "finding_type": "db_entity_without_valid_info",
                "internal_id": "uuid-1",
                "category": "MANUAL_REVIEW",
                "risk": "medium",
                "current_data": {
                    "db_record": {
                        "mod_id": mid,
                        "internal_id": "uuid-1",
                        "platform": "nexus",
                        "app_id": 413150,
                        "external_id": "1333",
                        "workspace_id": "1333",
                    },
                    "info_path": "",
                    "extra": {},
                },
                "evidence": {},
                "needs_human_confirm_reason": "x",
            },
            {
                "plan_index": 2,
                "finding_type": "db_entity_without_valid_info",
                "internal_id": "uuid-2",
                "category": "MANUAL_REVIEW",
                "risk": "medium",
                "current_data": {
                    "db_record": {
                        "mod_id": "9002",
                        "internal_id": "uuid-2",
                        "platform": "nexus",
                        "app_id": 413150,
                        "external_id": "1333",
                    },
                    "extra": {},
                },
                "evidence": {},
                "needs_human_confirm_reason": "x",
            },
            {
                "plan_index": 3,
                "finding_type": "backup_identity_source_anomaly",
                "internal_id": "orphan-id",
                "category": "MANUAL_REVIEW",
                "risk": "medium",
                "current_data": {
                    "db_record": {},
                    "extra": {"backup_dir": str(orphan_bak)},
                },
                "evidence": {},
                "needs_human_confirm_reason": "x",
            },
            {
                "plan_index": 4,
                "finding_type": "cross_game_same_external_id",
                "internal_id": "uuid-1",
                "category": "MANUAL_REVIEW",
                "risk": "high",
                "current_data": {
                    "db_record": {"mod_id": mid, "internal_id": "uuid-1"},
                    "extra": {"rows": [{"mod_id": mid}, {"mod_id": "9002"}]},
                },
                "evidence": {},
                "needs_human_confirm_reason": "x",
            },
        ],
    }

    before = db_path.read_bytes()
    preview = build_repair_preview(plan, backup_root=backup)
    after = db_path.read_bytes()
    assert before == after
    assert preview["production_mutation"] == "NONE"
    assert all(i["requires_manual_confirm"] is True for i in preview["items"])

    by_id = {i["finding_id"]: i for i in preview["items"]}
    assert by_id["info_internal_id_not_in_db:0"]["proposed_action"] == ACTION_IGNORE_ORPHAN
    assert by_id["db_entity_without_valid_info:1"]["proposed_action"] == ACTION_RESTORE_INFO
    assert by_id["db_entity_without_valid_info:2"]["proposed_action"] == ACTION_MANUAL_REVIEW
    assert by_id["backup_identity_source_anomaly:3"]["proposed_action"] == ACTION_REVIEW_BACKUP
    assert by_id["cross_game_same_external_id:4"]["proposed_action"] == ACTION_SPLIT_REVIEW

    restore = by_id["db_entity_without_valid_info:1"]
    assert restore["after_preview"]["diff"]["unchanged_db"] is True
    from services.mod_identity import read_entity_key

    assert read_entity_key(restore["after_preview"]["info_record"]) == "uuid-1"
    assert restore["after_preview"]["info_record"]["external_id"] == "1333"


def test_preview_tool_source_forbids_writes() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "tools" / "identity_repair_preview.py"
    ).read_text(encoding="utf-8")
    assert "create_mod_identity(" not in src
    assert "find_mod_by_workspace_id(" not in src
    assert "INSERT INTO mods" not in src
    assert "persist_unified_metadata_dict" not in src
