"""Tests for Stardew Valley workspace_id duplicate folder cleanup tool."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from tools.cleanup_stardew_workspace_duplicates import (
    STARDEW_APP_ID,
    STARDEW_GAME_DIR,
    apply_stardew_workspace_cleanup,
    scan_stardew_workspace_duplicates,
)

GAME = STARDEW_GAME_DIR


def _info(folder: Path, payload: dict) -> None:
    info = folder / ".info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    DatabaseManager.reset_instance()
    db_path = tmp_path / "stardew.db"
    db = DatabaseManager.instance(db_path)
    db.upsert_game(GameInfo(app_id=STARDEW_APP_ID, name="Stardew Valley"))
    library = tmp_path / "mod"
    (library / GAME).mkdir(parents=True)
    return db_path, library


def _insert(
    db: DatabaseManager,
    *,
    mod_id: int,
    path: Path,
    workspace_id: str,
    internal_id: str,
    external_id: str | None = None,
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
                STARDEW_APP_ID,
                "Tractor Mod",
                "Tractor Mod",
                workspace_id,
                external_id if external_id is not None else workspace_id,
                internal_id,
                str(path.resolve()),
            ),
        )
        db._conn.commit()


def test_same_workspace_two_dirs_detected(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    game = library / GAME
    a = game / "Tractor Mod"
    b = game / "Tractor Mod_9000000000003141"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _info(a, {"internal_id": "keep-iid", "workspace_id": "1401"})
    _info(b, {"internal_id": "poll-iid", "workspace_id": "1401"})
    DatabaseManager.reset_instance()

    plan = scan_stardew_workspace_duplicates(
        library_root=library, db_path=db_path, game_dir_name=GAME
    )
    assert plan["summary"]["duplicate_groups"] == 1
    g = plan["groups"][0]
    assert g["workspace_id"] == "1401"
    assert len(g["duplicate_paths"]) == 2
    assert len(g["each_internal_id"]) == 2
    assert "keep_candidate" in g and "delete_candidates" in g
    assert "db_binding" in g


def test_correct_keep_directory(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / GAME
    keep = game / "CJB Cheats Menu"
    poll = game / "CJB Cheats Menu_9000000000003117"
    iid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    # DB bound to pollution path → keep must still be the DB-bound directory.
    _insert(db, mod_id=9000000000003117, path=poll, workspace_id="4", internal_id=iid)
    _info(keep, {"internal_id": "other", "workspace_id": "4"})
    _info(poll, {"internal_id": iid, "workspace_id": "4"})
    DatabaseManager.reset_instance()

    plan = scan_stardew_workspace_duplicates(
        library_root=library, db_path=db_path, game_dir_name=GAME
    )
    g = plan["groups"][0]
    assert Path(g["keep_candidate"]["path"]).name == "CJB Cheats Menu_9000000000003117"
    assert g["keep_reason"] == "db_bound_path"

    # Without DB bind → prefer normal dirname.
    DatabaseManager.reset_instance()
    db2_path, library2 = _seed(tmp_path / "nobind")
    game2 = library2 / GAME
    keep2 = game2 / "Bigger Backpack"
    poll2 = game2 / "Bigger Backpack_9000000000003121"
    keep2.mkdir(parents=True)
    poll2.mkdir(parents=True)
    _info(keep2, {"internal_id": "k", "workspace_id": "1845"})
    _info(poll2, {"internal_id": "p", "workspace_id": "1845"})
    DatabaseManager.reset_instance()
    plan2 = scan_stardew_workspace_duplicates(
        library_root=library2, db_path=db2_path, game_dir_name=GAME
    )
    assert Path(plan2["groups"][0]["keep_candidate"]["path"]).name == "Bigger Backpack"
    assert plan2["groups"][0]["keep_reason"] == "normal_dirname"


def test_delete_duplicate_and_rebind_and_fix_info(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / GAME
    keep = game / "Destroyable Bushes"
    poll = game / "Destroyable Bushes_9000000000003116"
    db_iid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    # DB currently bound to pollution copy.
    _insert(
        db,
        internal_id=9000000000003116,
        path=poll,
        workspace_id="6304",
        internal_id=db_iid,
    )
    # But we want keep strategy: if we bind DB to poll, keep is poll.
    # For this test: bind DB to keep path so keep is normal + fix info.
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET last_known_path = ? WHERE mod_id = ?",
            (str(keep.resolve()), 9000000000003116),
        )
        db._conn.commit()
    keep.mkdir(parents=True)
    _info(
        keep,
        {
            "internal_id": "stale-orphan-iid",
            "workspace_id": "6304",
            "title": "Destroyable Bushes",
        },
    )
    _info(poll, {"internal_id": "poll-iid", "workspace_id": "6304"})
    DatabaseManager.reset_instance()

    plan = scan_stardew_workspace_duplicates(
        library_root=library, db_path=db_path, game_dir_name=GAME
    )
    g = plan["groups"][0]
    assert Path(g["keep_candidate"]["path"]).resolve() == keep.resolve()
    assert g["fix_info_internal_id"] is True
    assert len(g["delete_candidates"]) == 1

    result = apply_stardew_workspace_cleanup(
        plan,
        apply=True,
        confirm=True,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
        game_dir_name=GAME,
    )
    assert result["applied"] is True
    assert not poll.exists()
    assert keep.is_dir()

    con = sqlite3.connect(str(db_path))
    row = con.execute(
        "SELECT last_known_path, folder_present, internal_id, workspace_id "
        "FROM mods WHERE mod_id = ?",
        (9000000000003116,),
    ).fetchone()
    count = con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    con.close()
    assert count == 1
    assert Path(row[0]).resolve() == keep.resolve()
    assert int(row[1]) == 1
    assert row[2] == db_iid
    assert row[3] == "6304"

    meta = json.loads((keep / ".info" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["internal_id"] == db_iid
    assert meta["workspace_id"] == "6304"


def test_dry_run_no_mutation(tmp_path: Path) -> None:
    db_path, library = _seed(tmp_path)
    db = DatabaseManager.instance(db_path)
    game = library / GAME
    keep = game / "Wear More Rings"
    poll = game / "Wear More Rings_9000000000003145"
    iid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _insert(db, mod_id=9000000000003145, path=keep, workspace_id="3214", internal_id=iid)
    _info(keep, {"internal_id": iid, "workspace_id": "3214"})
    _info(poll, {"internal_id": "other", "workspace_id": "3214"})
    DatabaseManager.reset_instance()

    before = db_path.read_bytes()
    plan = scan_stardew_workspace_duplicates(
        library_root=library, db_path=db_path, game_dir_name=GAME
    )
    result = apply_stardew_workspace_cleanup(
        plan,
        apply=False,
        confirm=False,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
        game_dir_name=GAME,
    )
    assert result["applied"] is False
    assert db_path.read_bytes() == before
    assert keep.is_dir() and poll.is_dir()

    result2 = apply_stardew_workspace_cleanup(
        plan,
        apply=True,
        confirm=False,
        library_root=library,
        db_path=db_path,
        project_root=tmp_path,
        game_dir_name=GAME,
    )
    assert result2["applied"] is False
    assert poll.is_dir()
