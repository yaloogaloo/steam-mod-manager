"""Phase 4-A migration review — human package only; never mutates data."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from core.paths import project_root

ROOT = project_root()
TOOL = ROOT / "tools" / "identity_data_migration_review.py"


@pytest.fixture()
def sample_audit(tmp_path: Path) -> Path:
    audit = {
        "generated_at": "2026-01-01T00:00:00Z",
        "db_path": str(tmp_path / "hygiene.db"),
        "workspace_external_mismatch": {
            "A_empty_workspace_has_external": [
                {
                    "mod_id": "9001",
                    "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "platform": "nexus",
                    "app_id": 413150,
                    "workspace_id": "",
                    "external_id": "1333",
                    "source_url": "https://www.nexusmods.com/stardewvalley/mods/1333",
                    "info_path": str(tmp_path / "mod" / "A" / ".info" / "metadata.json"),
                    "last_known_path": str(tmp_path / "mod" / "A"),
                }
            ],
            "B_workspace_ne_external": [
                {
                    "mod_id": "9002",
                    "internal_id": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "platform": "nexus",
                    "app_id": 413150,
                    "workspace_id": "17863417826512345",
                    "external_id": "1401",
                    "source_url": "https://www.nexusmods.com/stardewvalley/mods/1401",
                    "info_path": str(tmp_path / "mod" / "B" / ".info" / "metadata.json"),
                    "last_known_path": str(tmp_path / "mod" / "B"),
                }
            ],
            "C_empty_external": [],
        },
        "cross_game_workspace_report": {
            "cross_game_allowed": [],
            "same_app_conflict": [
                {
                    "platform": "nexus",
                    "workspace_id": "999",
                    "app_id": 413150,
                    "entities": [
                        {"mod_id": "9003", "internal_id": "c1", "app_id": 413150},
                        {"mod_id": "9004", "internal_id": "c2", "app_id": 413150},
                    ],
                    "status": "SAME_APP_CONFLICT",
                }
            ],
        },
        "internal_id_integrity": {
            "db_internal_id_empty": [
                {
                    "mod_id": "9005",
                    "last_known_path": "",
                    "info_path": "",
                    "issue": "DB_INTERNAL_ID_EMPTY",
                }
            ],
            "db_present_info_missing": [],
            "info_present_db_missing": [],
            "duplicate_internal_id": [],
        },
    }
    path = tmp_path / "identity_data_hygiene_report.json"
    path.write_text(json.dumps(audit), encoding="utf-8")
    # Tiny RO DB for title enrichment
    db_path = tmp_path / "hygiene.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE mods (mod_id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO mods(mod_id, title) VALUES (9001, 'Carry')")
    con.execute("INSERT INTO mods(mod_id, title) VALUES (9002, 'Tractor')")
    con.commit()
    con.close()
    return path


def test_tool_source_has_no_update_create_delete() -> None:
    from tools.identity_data_migration_review import assert_tool_source_is_read_only

    src = TOOL.read_text(encoding="utf-8")
    assert_tool_source_is_read_only(src)
    # Extra explicit guards for reviewers
    assert re.search(r"\bUPDATE\s+\w+", src, re.I) is None
    assert re.search(r"\bINSERT\s+INTO\b", src, re.I) is None
    assert re.search(r"\bDELETE\s+FROM\b", src, re.I) is None
    assert "create_mod_identity(" not in src
    assert "reconcile_library(" not in src
    assert "allocate_internal_id(" not in src


def test_review_emits_required_categories_and_actions(
    sample_audit: Path, tmp_path: Path
) -> None:
    from tools.identity_data_migration_review import run_review

    out_json = tmp_path / "identity_migration_review.json"
    out_csv = tmp_path / "identity_migration_review.csv"
    db_path = tmp_path / "hygiene.db"
    before = db_path.read_bytes()
    sha_before = hashlib.sha256(before).hexdigest()

    review = run_review(
        audit_path=sample_audit,
        out_json=out_json,
        out_csv=out_csv,
        db_path=db_path,
    )

    after = db_path.read_bytes()
    assert after == before
    assert hashlib.sha256(after).hexdigest() == sha_before
    assert review["production_mutation"] == "NONE"
    assert review["auto_apply"] is False
    assert review["db_bytes_unchanged"] is True

    cats = {c["problem"]["category"] for c in review["candidates"]}
    assert "EMPTY_WORKSPACE" in cats
    assert "WORKSPACE_EXTERNAL_MISMATCH" in cats
    assert "EMPTY_INTERNAL_ID" in cats
    assert "SAME_APP_WORKSPACE_CONFLICT" in cats

    actions = {c["recommended_action"] for c in review["candidates"]}
    assert actions <= {"KEEP", "UPDATE_WORKSPACE_ONLY", "DELETE_POLLUTION", "MANUAL_REVIEW"}

    empty_ws = next(
        c for c in review["candidates"] if c["problem"]["category"] == "EMPTY_WORKSPACE"
    )
    assert empty_ws["identity"]["title"] == "Carry"
    assert empty_ws["identity"]["workspace_id"] == ""
    assert empty_ws["identity"]["external_id"] == "1333"
    assert empty_ws["recommended_action"] == "UPDATE_WORKSPACE_ONLY"
    assert empty_ws["requires_manual_confirm"] is True
    assert "info_path" in empty_ws["evidence"]
    assert "backup_reference" in empty_ws["evidence"]

    conflict = [
        c
        for c in review["candidates"]
        if c["problem"]["category"] == "SAME_APP_WORKSPACE_CONFLICT"
    ]
    assert len(conflict) == 2
    assert conflict[0]["recommended_action"] == "MANUAL_REVIEW"
    assert conflict[0]["evidence"]["related_mods"]

    assert out_json.is_file() and out_csv.is_file()
    csv_text = out_csv.read_text(encoding="utf-8")
    assert "EMPTY_WORKSPACE" in csv_text
    assert "requires_manual_confirm" in csv_text


def test_review_does_not_call_lifecycle_services(sample_audit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import identity_data_migration_review as mod

    def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("lifecycle must not be called")

    monkeypatch.setattr(
        "services.identity_service.create_mod_identity", boom, raising=False
    )
    # Module must not import these at runtime during review
    review = mod.run_review(
        audit_path=sample_audit,
        out_json=tmp_path / "r.json",
        out_csv=tmp_path / "r.csv",
        db_path=tmp_path / "hygiene.db",
    )
    assert review["counts"]["candidates"] >= 4
