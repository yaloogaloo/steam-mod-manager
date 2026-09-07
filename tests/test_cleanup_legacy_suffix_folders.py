"""Tests for one-shot legacy ``*_900000000000xxxx`` folder cleanup."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from tools.cleanup_legacy_suffix_folders import (
    ACTION_DELETE,
    apply_legacy_suffix_cleanup,
    is_legacy_suffix_dirname,
    run_auto_clean,
    scan_legacy_suffix_folders,
)

STARDEW = 413150


def _info(folder: Path, payload: dict) -> None:
    info = folder / ".info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    DatabaseManager.reset_instance()
    db_path = tmp_path / "legacy.db"
    db = DatabaseManager.instance(db_path)
    db.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    library = tmp_path / "mod"
    library.mkdir()
    return db_path, library


def _insert(
    db: DatabaseManager,
    *,
    mod_id: int,
    path: Path,
    workspace_id: str,
    internal_id: str,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, display_name, platform, workspace_id,
                external_id, internal_id, last_known_path, folder_present,
                updated_at
            ) VALUES (?, ?, ?, ?, 'nexus', ?, ?, ?, ?, 1, datetime('now'))
            """,
            (
                mod_id,
                STARDEW,
                "Tractor Mod",
                "Tractor Mod",
                workspace_id,
                workspace_id,
                internal_id,
                str(path.resolve()),
            ),
        )
        db._conn.commit()


def test_suffix_folder_detected(tmp_path: Path) -> None:
    assert is_legacy_suffix_dirname("Tractor Mod_9000000000003141")
    assert is_legacy_suffix_dirname("Foo_900000000000")
    assert not is_legacy_suffix_dirname("Tractor Mod")
    assert not is_legacy_suffix_dirname("Mod_9000")

    db_path, library = _seed(tmp_path)
    game = library / "Stardew Valley"
    poll = game / "Tractor Mod_9000000000003141"
    poll.mkdir(parents=True)
    _info(poll, {"internal_id": "iid-poll", "workspace_id": "1401"})
    DatabaseManager.reset_instance()

    plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)
    assert plan["summary"]["matched"] == 1
    item = plan["items"][0]
    assert item["game"] == "Stardew Valley"
    assert item["folder"] == "Tractor Mod_9000000000003141"
    assert item["metadata"]["internal_id"] == "iid-poll"
    assert item["metadata"]["workspace_id"] == "1401"
    for key in (
        "game",
        "folder",
        "path",
        "metadata",
        "db_match",
        "replacement_folder",
        "action",
    ):
        assert key in item


def test_suffix_folder_always_delete_candidate(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / "Stardew Valley"
    poll = game / "CJB Cheats Menu_9000000000003117"
    # Even with DB bind + .info ids present → still delete candidate.
    _insert(
        db,
        internal_id=9000000000003117,
        path=poll,
        workspace_id="4",
        internal_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    _info(
        poll,
        {
            "internal_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "workspace_id": "4",
        },
    )
    DatabaseManager.reset_instance()

    plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)
    assert len(plan["items"]) == 1
    assert plan["items"][0]["action"] == ACTION_DELETE
    assert plan["items"][0]["confirmed"] is True
    assert plan["items"][0]["db_match"] is not None


def test_db_pointing_suffix_folder_gets_rebound_if_normal_exists(
    tmp_path: Path,
) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / "Stardew Valley"
    normal = game / "Destroyable Bushes"
    poll = game / "Destroyable Bushes_9000000000003116"
    keep_iid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    _insert(
        db,
        internal_id=9000000000003116,
        path=poll,
        workspace_id="6304",
        internal_id=keep_iid,
    )
    _info(poll, {"internal_id": keep_iid, "workspace_id": "6304"})
    normal.mkdir(parents=True)
    _info(
        normal,
        {
            "internal_id": "orphan-or-other",
            "workspace_id": "6304",
            "title": "Destroyable Bushes",
        },
    )
    DatabaseManager.reset_instance()

    plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)
    assert plan["items"][0]["replacement_folder"]
    assert Path(plan["items"][0]["replacement_folder"]).name == "Destroyable Bushes"
    assert plan["items"][0]["path_rebind"] == "rebind_to_normal"

    result = apply_legacy_suffix_cleanup(
        plan,
        apply=True,
        confirm=True,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
    )
    assert result["applied"] is True
    assert not poll.exists()
    assert normal.is_dir()

    con = sqlite3.connect(str(db_path))
    row = con.execute(
        "SELECT last_known_path, folder_present, workspace_id, internal_id "
        "FROM mods WHERE mod_id = ?",
        (9000000000003116,),
    ).fetchone()
    con.close()
    assert row is not None
    assert Path(row[0]).resolve() == normal.resolve()
    assert int(row[1]) == 1
    assert row[2] == "6304"
    assert row[3] == keep_iid


def test_dry_run_no_mutation(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / "Stardew Valley"
    poll = game / "Bigger Backpack_9000000000003121"
    _insert(
        db,
        internal_id=9000000000003121,
        path=poll,
        workspace_id="1845",
        internal_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    _info(poll, {"internal_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "workspace_id": "1845"})
    DatabaseManager.reset_instance()

    before_db = db_path.read_bytes()
    plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)
    result = apply_legacy_suffix_cleanup(
        plan,
        apply=False,
        confirm=False,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
    )
    assert result["applied"] is False
    assert db_path.read_bytes() == before_db
    assert poll.is_dir()


def test_apply_requires_confirm(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / "Stardew Valley"
    poll = game / "Wear More Rings_9000000000003145"
    _insert(
        db,
        internal_id=9000000000003145,
        path=poll,
        workspace_id="3214",
        internal_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
    )
    _info(poll, {"internal_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "workspace_id": "3214"})
    DatabaseManager.reset_instance()

    before_db = db_path.read_bytes()
    plan = scan_legacy_suffix_folders(library_root=library, db_path=db_path)
    result = apply_legacy_suffix_cleanup(
        plan,
        apply=True,
        confirm=False,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
    )
    assert result["applied"] is False
    assert db_path.read_bytes() == before_db
    assert poll.is_dir()

    result2 = apply_legacy_suffix_cleanup(
        plan,
        apply=True,
        confirm=True,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
    )
    assert result2["applied"] is True
    assert not poll.exists()

    con = sqlite3.connect(str(db_path))
    row = con.execute(
        "SELECT folder_present, last_known_path FROM mods WHERE mod_id = ?",
        (9000000000003145,),
    ).fetchone()
    count = con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    con.close()
    assert count == 1  # entity retained
    assert int(row[0]) == 0  # marked missing (no normal replacement)
    assert not row[1]  # path cleared — must not point at deleted folder


def test_auto_clean_writes_before_after_and_verifies(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / "Stardew Valley"
    normal = game / "Tractor Mod"
    poll = game / "Tractor Mod_9000000000003141"
    iid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

    _insert(db, mod_id=9000000000003141, path=poll, workspace_id="1401", internal_id=iid)
    _info(poll, {"internal_id": iid, "workspace_id": "1401"})
    normal.mkdir(parents=True)
    _info(normal, {"internal_id": "other", "workspace_id": "1401"})
    DatabaseManager.reset_instance()

    before_path = tmp_path / "legacy_suffix_cleanup_before.json"
    after_path = tmp_path / "legacy_suffix_cleanup_after.json"
    result = run_auto_clean(
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
        before_path=before_path,
        after_path=after_path,
        run_contract_tests=False,
    )
    assert result["ok"] is True
    assert before_path.is_file()
    assert after_path.is_file()
    assert not poll.exists()
    assert normal.is_dir()

    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    assert before["entity_count"] == after["entity_count"] == 1
    assert before["entities"][0]["internal_id"] == after["entities"][0]["internal_id"]
    assert before["entities"][0]["workspace_id"] == after["entities"][0]["workspace_id"]
    assert after["scan"]["summary"]["matched"] == 0
    assert after["verification"]["ok"] is True

    con = sqlite3.connect(str(db_path))
    row = con.execute(
        "SELECT last_known_path, folder_present FROM mods WHERE mod_id = ?",
        (9000000000003141,),
    ).fetchone()
    con.close()
    assert Path(row[0]).resolve() == normal.resolve()
    assert int(row[1]) == 1
