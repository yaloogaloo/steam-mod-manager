"""Guards: Identity Recovery Phase 2-D manual review CSV is read-only."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from tools.identity_recovery_manual_review import (
    CSV_COLUMNS,
    assert_tool_is_read_only,
    build_manual_review_rows,
    generate_manual_review,
)

TOOL = Path(__file__).resolve().parents[1] / "tools" / "identity_recovery_manual_review.py"


def test_manual_review_tool_source_is_read_only() -> None:
    src = TOOL.read_text(encoding="utf-8")
    assert_tool_is_read_only(src)
    assert "phase\": \"2-D\"" in src or 'phase": "2-D"' in src
    assert "production_mutation" in src
    assert "manual_decision" in src


def test_build_rows_covers_all_preview_items_and_leaves_decision_empty() -> None:
    report = {
        "findings": [
            {
                "code": "info_internal_id_not_in_db",
                "internal_id": "orphan-1",
                "app_id": 0,
                "platform": "nexus",
                "external_id": "",
                "conflict_reason": "UUID not in DB",
                "recommended_action": "IGNORE_ORPHAN_INFO",
            }
        ]
    }
    plan = {
        "items": [
            {
                "category": "AUTO_SAFE",
                "risk": "low",
                "finding_type": "info_internal_id_not_in_db",
                "current_state": "orphan info",
                "needs_human_confirm_reason": "policy confirm",
                "recommended_action": "IGNORE_ORPHAN_INFO",
            }
        ]
    }
    preview = {
        "items": [
            {
                "finding_id": "info_internal_id_not_in_db:0",
                "plan_index": 0,
                "plan_category": "AUTO_SAFE",
                "risk": "low",
                "proposed_action": "IGNORE_ORPHAN_INFO",
                "before": {
                    "db_record": None,
                    "info_record": {
                        "internal_id": "orphan-1",
                        "platform": "nexus",
                        "app_id": 0,
                        "workspace_id": "1333",
                    },
                    "backup_record": None,
                },
                "evidence": {"conflict_reason": "UUID not in DB"},
            }
        ]
    }
    apply_log = {"status": "NO_SAFE_CANDIDATES", "skipped": []}
    rows = build_manual_review_rows(
        report=report, plan=plan, preview=preview, apply_log=apply_log
    )
    assert len(rows) == 1
    row = rows[0]
    assert set(CSV_COLUMNS) == set(row.keys())
    assert row["finding_id"] == "info_internal_id_not_in_db:0"
    assert row["category"] == "AUTO_SAFE"
    assert row["internal_id"] == "orphan-1"
    assert row["workspace_id"] == "1333"
    assert row["recommended_action"] == "IGNORE_ORPHAN_INFO"
    assert row["manual_decision"] == ""
    assert "UUID not in DB" in row["problem_description"]
    assert row["info_record"]


def test_generate_csv_does_not_mutate_db(tmp_path: Path) -> None:
    db_path = tmp_path / "mod_manager.db"
    db_bytes = b"identity-recovery-phase-2d-db"
    db_path.write_bytes(db_bytes)

    report = {
        "db_path": str(db_path),
        "findings": [
            {
                "code": "db_entity_without_valid_info",
                "internal_id": "uuid-a",
                "app_id": 292030,
                "platform": "steam",
                "external_id": "111",
                "conflict_reason": "missing info",
                "recommended_action": "RESTORE_INFO_FROM_BACKUP",
            }
        ],
    }
    plan = {
        "items": [
            {
                "category": "MANUAL_REVIEW",
                "risk": "medium",
                "current_state": "no .info",
                "needs_human_confirm_reason": "needs confirm",
                "recommended_action": "RESTORE after confirm",
            }
        ]
    }
    preview = {
        "items": [
            {
                "finding_id": "db_entity_without_valid_info:0",
                "plan_index": 0,
                "plan_category": "MANUAL_REVIEW",
                "risk": "medium",
                "proposed_action": "RESTORE_INFO_FROM_BACKUP",
                "before": {
                    "db_record": {
                        "mod_id": "111",
                        "internal_id": "uuid-a",
                        "platform": "steam",
                        "app_id": 292030,
                        "external_id": "111",
                        "workspace_id": "111",
                    },
                    "info_record": None,
                    "backup_record": {"internal_id": "uuid-a", "published_file_id": "111"},
                },
                "evidence": {"conflict_reason": "missing info"},
            }
        ]
    }
    apply_log = {
        "status": "NO_SAFE_CANDIDATES",
        "skipped": [
            {
                "finding_id": "db_entity_without_valid_info:0",
                "reason": "external_id mismatch",
            }
        ],
    }

    report_path = tmp_path / "report.json"
    plan_path = tmp_path / "plan.json"
    preview_path = tmp_path / "preview.json"
    apply_path = tmp_path / "apply.json"
    out_path = tmp_path / "manual.csv"
    for path, payload in (
        (report_path, report),
        (plan_path, plan),
        (preview_path, preview),
        (apply_path, apply_log),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")

    # Touch sentinel files that must not change
    info_sentinel = tmp_path / "folder" / ".info" / "metadata.json"
    info_sentinel.parent.mkdir(parents=True)
    info_sentinel.write_text('{"keep":true}', encoding="utf-8")
    backup_sentinel = tmp_path / "mod_backup" / "111" / "metadata.json"
    backup_sentinel.parent.mkdir(parents=True)
    backup_sentinel.write_text('{"keep":true}', encoding="utf-8")
    info_before = info_sentinel.read_bytes()
    backup_before = backup_sentinel.read_bytes()

    summary = generate_manual_review(
        report_path=report_path,
        plan_path=plan_path,
        preview_path=preview_path,
        apply_log_path=apply_path,
        out_path=out_path,
        db_path=db_path,
    )

    assert summary["rows_count"] == 1
    assert summary["db_bytes_unchanged"] is True
    assert db_path.read_bytes() == db_bytes
    assert info_sentinel.read_bytes() == info_before
    assert backup_sentinel.read_bytes() == backup_before
    assert out_path.is_file()

    with out_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_COLUMNS
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["manual_decision"] == ""
    assert rows[0]["recommended_action"] == "RESTORE_INFO_FROM_BACKUP"
    assert "phase_2c_skip=external_id mismatch" in rows[0]["problem_description"]
