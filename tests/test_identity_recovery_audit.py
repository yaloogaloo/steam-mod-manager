"""Guards: Identity Recovery Phase-1 tool is read-only and lifecycle-safe."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "identity_recovery_audit.py"


def test_recovery_tool_source_forbids_create_and_workspace_lookup() -> None:
    src = TOOL.read_text(encoding="utf-8")
    from tools.identity_recovery_audit import assert_tool_is_read_only

    assert_tool_is_read_only(src)
    assert "ModDeployer" not in src
    assert 'phase": 1' in src or "phase\": 1" in src
    assert "production_mutation" in src
    assert "NONE" in src


def test_recovery_tool_scan_is_read_only(tmp_path: Path) -> None:
    import json
    import sqlite3

    from tools.identity_recovery_audit import scan_identity_recovery

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
            identity_status TEXT,
            source_type TEXT
        )
        """
    )
    # Cross-game same external_id 1333
    con.execute(
        "INSERT INTO mods VALUES (1,413150,'nexus','1333','1333','aaa', 'Carry',"
        "'','https://www.nexusmods.com/stardewvalley/mods/1333','',1,'','nexus')"
    )
    con.execute(
        "INSERT INTO mods VALUES (2,1086940,'nexus','1333','1333','bbb', 'Lib',"
        "'','https://www.nexusmods.com/baldursgate3/mods/1333','',1,'','nexus')"
    )
    con.commit()
    con.close()

    library = tmp_path / "mod"
    orphan = library / "BG3" / "OrphanInfo"
    orphan.mkdir(parents=True)
    info = orphan / ".info"
    info.mkdir()
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "workspace_id": "1333",
                "platform": "nexus",
                "title": "Orphan",
            }
        ),
        encoding="utf-8",
    )
    backup = tmp_path / "mod_backup"
    (backup / "999").mkdir(parents=True)
    (backup / "999" / "metadata.json").write_text(
        json.dumps({"internal_id": "orphan-bak", "title": "Bak"}),
        encoding="utf-8",
    )

    before = db_path.read_bytes()
    report = scan_identity_recovery(
        db_path=db_path, library=library, backup_root=backup
    )
    after = db_path.read_bytes()
    assert before == after
    assert report["production_mutation"] == "NONE"
    assert report["guards"]["calls_create_mod_identity"] is False
    assert report["guards"]["calls_workspace_reverse_lookup"] is False
    codes = {f["code"] for f in report["findings"]}
    assert "cross_game_same_external_id" in codes
    assert "cross_game_same_workspace_id" in codes
    assert "info_internal_id_not_in_db" in codes
    assert "backup_identity_source_anomaly" in codes
    for f in report["findings"]:
        assert "conflict_reason" in f
        assert "recommended_action" in f
        assert "db_record" in f
