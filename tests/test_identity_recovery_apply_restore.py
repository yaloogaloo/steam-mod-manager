"""Phase 2-C restore apply — only RESTORE_INFO, rollback on failure, no DB writes."""

from __future__ import annotations

import json
from pathlib import Path

from tools.identity_recovery_apply_restore import (
    ALLOWED_ACTION,
    _build_restored_payload,
    _identity_match,
    _validate_info_against_db,
    apply_restore_items,
    assert_frozen_unchanged,
    snapshot_mods,
)


def _make_sqlite(tmp_path: Path) -> Path:
    import sqlite3

    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
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
            source_url TEXT,
            last_known_path TEXT,
            folder_present INTEGER,
            deploy_status TEXT,
            deploy_path TEXT,
            content_status TEXT,
            conflict_status TEXT
        )
        """
    )
    folder = tmp_path / "mod" / "Game" / "ModA"
    folder.mkdir(parents=True)
    (folder / "content.bin").write_bytes(b"x")
    con.execute(
        """
        INSERT INTO mods VALUES (
            9001, 413150, 'nexus', '1333', '1333', 'uuid-1',
            'Carry', '', ?, 1, 'not_deployed', '', 'healthy', 'none'
        )
        """,
        (str(folder),),
    )
    con.commit()
    con.close()
    return db


def test_apply_restore_writes_info_and_keeps_db(tmp_path: Path) -> None:
    db = _make_sqlite(tmp_path)
    folder = tmp_path / "mod" / "Game" / "ModA"
    backup = tmp_path / "mod_backup" / "9001"
    backup.mkdir(parents=True)
    (backup / "metadata.json").write_text(
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

    item = {
        "finding_id": "db_entity_without_valid_info:0",
        "proposed_action": ALLOWED_ACTION,
        "before": {
            "db_record": {
                "mod_id": "9001",
                "internal_id": "uuid-1",
                "platform": "nexus",
                "app_id": 413150,
                "external_id": "1333",
                "workspace_id": "1333",
                "last_known_path": str(folder),
            }
        },
    }
    before_bytes = db.read_bytes()
    before_snap = snapshot_mods(db)
    result = apply_restore_items(
        [item],
        backup_root=tmp_path / "mod_backup",
        rollback_root=tmp_path / "rollback",
    )
    after_bytes = db.read_bytes()
    after_snap = snapshot_mods(db)
    assert before_bytes == after_bytes
    assert assert_frozen_unchanged(before_snap, after_snap) == []
    assert result["restored_count"] == 1
    info = json.loads((folder / ".info" / "metadata.json").read_text(encoding="utf-8"))
    from services.mod_identity import read_entity_key

    assert read_entity_key(info) == "uuid-1"
    assert info.get("internal_id") == "uuid-1"
    assert "entity_key" not in info
    assert info["external_id"] == "1333"
    assert info["app_id"] == 413150


def test_apply_restore_rolls_back_on_mismatch(tmp_path: Path) -> None:
    db = _make_sqlite(tmp_path)
    folder = tmp_path / "mod" / "Game" / "ModA"
    backup = tmp_path / "mod_backup" / "9001"
    backup.mkdir(parents=True)
    # Wrong external_id — must refuse before write via _identity_match in apply
    (backup / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "uuid-1",
                "platform": "nexus",
                "app_id": 413150,
                "external_id": "9999",
                "workspace_id": "1333",
            }
        ),
        encoding="utf-8",
    )
    item = {
        "finding_id": "db_entity_without_valid_info:1",
        "proposed_action": ALLOWED_ACTION,
        "before": {
            "db_record": {
                "mod_id": "9001",
                "internal_id": "uuid-1",
                "platform": "nexus",
                "app_id": 413150,
                "external_id": "1333",
                "workspace_id": "1333",
                "last_known_path": str(folder),
            }
        },
    }
    try:
        apply_restore_items(
            [item],
            backup_root=tmp_path / "mod_backup",
            rollback_root=tmp_path / "rollback",
        )
        assert False, "expected failure"
    except RuntimeError as exc:
        assert "mismatch" in str(exc).lower() or "failed" in str(exc).lower()
    assert not (folder / ".info" / "metadata.json").is_file()


def test_identity_match_and_payload_stamp() -> None:
    db = {
        "mod_id": "1",
        "internal_id": "u",
        "platform": "nexus",
        "app_id": 1,
        "external_id": "9",
        "workspace_id": "9",
    }
    bak = {
        "internal_id": "u",
        "platform": "nexus",
        "app_id": 1,
        "external_id": "9",
        "title": "t",
    }
    ok, fails = _identity_match(bak, db)
    assert ok and fails == []
    payload = _build_restored_payload(bak, db)
    from services.mod_identity import read_entity_key

    assert read_entity_key(payload) == "u"
    assert payload.get("internal_id") == "u"
    assert "entity_key" not in payload
    assert payload["workspace_id"] == "9"
    ok2, _ = _validate_info_against_db(payload, db)
    assert ok2


def test_apply_tool_source_guards() -> None:
    src = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "identity_recovery_apply_restore.py"
    ).read_text(encoding="utf-8")
    assert "create_mod_identity(" not in src
    assert "find_mod_by_workspace_id(" not in src
    assert "reconcile_library(" not in src
    assert "INSERT INTO mods" not in src
