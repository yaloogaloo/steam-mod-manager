"""Tests for one-shot workspace duplicate cleanup tool (no lifecycle mutations)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from tools.cleanup_duplicate_workspace_mods import (
    ACTION_DELETE,
    ACTION_FIX_INFO,
    ACTION_KEEP,
    ACTION_MANUAL,
    apply_cleanup_plan,
    build_cleanup_plan,
)

STARDEW = 413150
BG3 = 1086940


def _info(folder: Path, payload: dict) -> None:
    info = folder / ".info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_db(tmp_path: Path) -> tuple[Path, Path]:
    DatabaseManager.reset_instance()
    db_path = tmp_path / "cleanup.db"
    db = DatabaseManager.instance(db_path)
    db.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    db.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3"))
    library = tmp_path / "mod"
    library.mkdir()
    return db_path, library


def _insert_mod(
    db: DatabaseManager,
    *,
    mod_id: int,
    app_id: int,
    workspace_id: str,
    internal_id: str,
    path: Path,
    platform: str = "nexus",
    title: str = "Mod",
    external_id: str | None = None,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    # external_id has a UNIQUE(platform, app_id, external_id) index — pollution
    # duplicates share workspace_id but must not collide on external_id.
    ext = workspace_id if external_id is None else external_id
    with db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, display_name, platform, workspace_id,
                external_id, internal_id, last_known_path, folder_present,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
            """,
            (
                mod_id,
                app_id,
                title,
                title,
                platform,
                workspace_id,
                ext,
                internal_id,
                str(path),
            ),
        )
        db._conn.commit()


def test_dry_run_does_not_modify_db(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)

    keep_dir = library / "Stardew Valley" / "Tractor Mod"
    poll_dir = library / "Stardew Valley" / "Tractor Mod_9000000000003141"
    _insert_mod(
        db,
        mod_id=9000000000001001,
        app_id=STARDEW,
        workspace_id="1401",
        internal_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        path=keep_dir,
        title="Tractor Mod",
    )
    _insert_mod(
        db,
        mod_id=9000000000003141,
        app_id=STARDEW,
        workspace_id="1401",
        internal_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        path=poll_dir,
        title="Tractor Mod",
        external_id="pollution-1401",
    )
    _info(
        keep_dir,
        {
            "internal_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "workspace_id": "1401",
            "title": "Tractor Mod",
        },
    )
    _info(
        poll_dir,
        {
            "internal_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "workspace_id": "1401",
            "title": "Tractor Mod",
        },
    )
    DatabaseManager.reset_instance()

    before = db_path.read_bytes()
    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    apply_cleanup_plan(
        plan,
        apply=False,
        confirm=False,
        db_path=db_path,
        project_root=tmp_path,
        data_root=tmp_path / "data",
    )
    assert db_path.read_bytes() == before
    assert keep_dir.is_dir() and poll_dir.is_dir()

    deletes = [a for a in plan["actions"] if a["action"] == ACTION_DELETE]
    assert len(deletes) == 1
    assert deletes[0]["confirmed"] is True
    assert deletes[0]["deleted_internal_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert deletes[0]["kept_internal_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    for key in (
        "before",
        "action",
        "after",
        "deleted_path",
        "deleted_internal_id",
        "kept_internal_id",
        "reason",
    ):
        assert key in deletes[0]


def test_apply_only_confirmed_items(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)

    keep_dir = library / "Stardew Valley" / "CJB Cheats Menu"
    poll_dir = library / "Stardew Valley" / "CJB Cheats Menu_9000000000003117"
    _insert_mod(
        db,
        mod_id=9000000000002001,
        app_id=STARDEW,
        workspace_id="4",
        internal_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        path=keep_dir,
        title="CJB Cheats Menu",
    )
    _insert_mod(
        db,
        mod_id=9000000000003117,
        app_id=STARDEW,
        workspace_id="4",
        internal_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        path=poll_dir,
        title="CJB Cheats Menu",
        external_id="pollution-4",
    )
    _info(
        keep_dir,
        {
            "internal_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "workspace_id": "4",
        },
    )
    _info(
        poll_dir,
        {
            "internal_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "workspace_id": "4",
        },
    )

    backup = tmp_path / "data" / "mod_backup" / "9000000000003117"
    backup.mkdir(parents=True)
    (backup / "metadata.json").write_text("{}", encoding="utf-8")
    DatabaseManager.reset_instance()

    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    # Force one delete item unconfirmed — must be skipped.
    for a in plan["actions"]:
        if a["action"] == ACTION_DELETE:
            a["confirmed"] = False

    result = apply_cleanup_plan(
        plan,
        apply=True,
        confirm=True,
        db_path=db_path,
        project_root=tmp_path,
        data_root=tmp_path / "data",
    )
    assert result["applied"] is True
    assert poll_dir.is_dir()
    assert backup.is_dir()

    con = sqlite3.connect(str(db_path))
    count = con.execute(
        "SELECT COUNT(*) FROM mods WHERE mod_id = ?",
        (9000000000003117,),
    ).fetchone()[0]
    con.close()
    assert count == 1

    # Restore confirmed and apply for real.
    for a in plan["actions"]:
        if a["action"] == ACTION_DELETE:
            a["confirmed"] = True

    result2 = apply_cleanup_plan(
        plan,
        apply=True,
        confirm=True,
        db_path=db_path,
        project_root=tmp_path,
        data_root=tmp_path / "data",
    )
    assert any(r.get("action") == ACTION_DELETE and r.get("ok") for r in result2["results"])
    assert not poll_dir.exists()
    assert not backup.exists()
    assert keep_dir.is_dir()

    con = sqlite3.connect(str(db_path))
    assert (
        con.execute(
            "SELECT COUNT(*) FROM mods WHERE mod_id = ?",
            (9000000000003117,),
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM mods WHERE mod_id = ?",
            (9000000000002001,),
        ).fetchone()[0]
        == 1
    )
    con.close()


def test_same_workspace_across_games_allowed(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)

    a = library / "Stardew Valley" / "Community"
    b = library / "Baldurs Gate 3" / "Community"
    _insert_mod(
        db,
        mod_id=9000000000003001,
        app_id=STARDEW,
        workspace_id="1333",
        internal_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        path=a,
        title="Carry Chest",
    )
    _insert_mod(
        db,
        mod_id=9000000000003002,
        app_id=BG3,
        workspace_id="1333",
        internal_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        path=b,
        title="Community Library",
    )
    _info(a, {"internal_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "workspace_id": "1333"})
    _info(b, {"internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "workspace_id": "1333"})
    DatabaseManager.reset_instance()

    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    assert plan["summary"]["duplicate_groups"] == 0
    assert plan["actions"] == []


def test_same_game_duplicate_must_be_found(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)

    keep_dir = library / "Stardew Valley" / "Bigger Backpack"
    poll_dir = library / "Stardew Valley" / "Bigger Backpack_9000000000003121"
    _insert_mod(
        db,
        mod_id=9000000000004001,
        app_id=STARDEW,
        workspace_id="1845",
        internal_id="11111111-1111-1111-1111-111111111111",
        path=keep_dir,
    )
    _insert_mod(
        db,
        mod_id=9000000000003121,
        app_id=STARDEW,
        workspace_id="1845",
        internal_id="22222222-2222-2222-2222-222222222222",
        path=poll_dir,
        external_id="pollution-1845",
    )
    _info(
        keep_dir,
        {
            "internal_id": "11111111-1111-1111-1111-111111111111",
            "workspace_id": "1845",
        },
    )
    _info(
        poll_dir,
        {
            "internal_id": "22222222-2222-2222-2222-222222222222",
            "workspace_id": "1845",
        },
    )
    DatabaseManager.reset_instance()

    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    assert plan["summary"]["duplicate_groups"] == 1
    assert plan["summary"]["delete_pollution"] == 1
    assert any(a["action"] == ACTION_KEEP for a in plan["actions"])
    assert any(a["action"] == ACTION_DELETE for a in plan["actions"])


def test_info_internal_id_fix_correct(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)

    keep_dir = library / "Stardew Valley" / "Destroyable Bushes"
    poll_dir = library / "Stardew Valley" / "Destroyable Bushes_9000000000003116"
    keep_iid = "33333333-3333-3333-3333-333333333333"
    _insert_mod(
        db,
        mod_id=9000000000005001,
        app_id=STARDEW,
        workspace_id="6304",
        internal_id=keep_iid,
        path=keep_dir,
        title="Destroyable Bushes",
    )
    _insert_mod(
        db,
        mod_id=9000000000003116,
        app_id=STARDEW,
        workspace_id="6304",
        internal_id="44444444-4444-4444-4444-444444444444",
        path=poll_dir,
        title="Destroyable Bushes",
        external_id="pollution-6304",
    )
    # Keeper path: workspace matches DB, but .info/entity_key (or legacy) is stale/wrong.
    _info(
        keep_dir,
        {
            "internal_id": "stale-orphan-internal-id",
            "workspace_id": "6304",
            "title": "Destroyable Bushes",
        },
    )
    _info(
        poll_dir,
        {
            "internal_id": "44444444-4444-4444-4444-444444444444",
            "workspace_id": "6304",
        },
    )
    DatabaseManager.reset_instance()

    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    fixes = [a for a in plan["actions"] if a["action"] == ACTION_FIX_INFO]
    assert len(fixes) == 1
    assert fixes[0]["confirmed"] is True
    assert fixes[0]["kept_internal_id"] == keep_iid
    assert fixes[0]["after"]["info"]["internal_id"] == keep_iid

    result = apply_cleanup_plan(
        plan,
        apply=True,
        confirm=True,
        db_path=db_path,
        project_root=tmp_path,
        data_root=tmp_path / "data",
    )
    assert result["applied"] is True
    meta = json.loads((keep_dir / ".info" / "metadata.json").read_text(encoding="utf-8"))
    from services.mod_identity import read_entity_key

    assert read_entity_key(meta) == keep_iid
    assert meta.get("internal_id") == keep_iid
    assert "entity_key" not in meta
    assert meta["workspace_id"] == "6304"
    assert not poll_dir.exists()


def test_apply_without_confirm_is_noop(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)
    keep_dir = library / "Stardew Valley" / "Wear More Rings"
    poll_dir = library / "Stardew Valley" / "Wear More Rings_9000000000003145"
    _insert_mod(
        db,
        mod_id=9000000000006001,
        app_id=STARDEW,
        workspace_id="3214",
        internal_id="55555555-5555-5555-5555-555555555555",
        path=keep_dir,
    )
    _insert_mod(
        db,
        mod_id=9000000000003145,
        app_id=STARDEW,
        workspace_id="3214",
        internal_id="66666666-6666-6666-6666-666666666666",
        path=poll_dir,
        external_id="pollution-3214",
    )
    _info(keep_dir, {"internal_id": "55555555-5555-5555-5555-555555555555", "workspace_id": "3214"})
    _info(poll_dir, {"internal_id": "66666666-6666-6666-6666-666666666666", "workspace_id": "3214"})
    DatabaseManager.reset_instance()

    before = db_path.read_bytes()
    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    result = apply_cleanup_plan(
        plan,
        apply=True,
        confirm=False,
        db_path=db_path,
        project_root=tmp_path,
        data_root=tmp_path / "data",
    )
    assert result["applied"] is False
    assert db_path.read_bytes() == before
    assert poll_dir.is_dir()


def test_ambiguous_duplicates_go_to_manual_review(tmp_path: Path) -> None:
    db_path, library = _seed_db(tmp_path)
    db = DatabaseManager.instance(db_path)
    a = library / "Stardew Valley" / "ModA"
    b = library / "Stardew Valley" / "ModB"
    _insert_mod(
        db,
        mod_id=9000000000007001,
        app_id=STARDEW,
        workspace_id="9999",
        internal_id="77777777-7777-7777-7777-777777777777",
        path=a,
        title="ModA",
    )
    _insert_mod(
        db,
        mod_id=9000000000007002,
        app_id=STARDEW,
        workspace_id="9999",
        internal_id="88888888-8888-8888-8888-888888888888",
        path=b,
        title="ModB",
        external_id="pollution-9999",
    )
    _info(a, {"internal_id": "77777777-7777-7777-7777-777777777777", "workspace_id": "9999"})
    _info(b, {"internal_id": "88888888-8888-8888-8888-888888888888", "workspace_id": "9999"})
    DatabaseManager.reset_instance()

    plan = build_cleanup_plan(
        db_path=db_path, library_root=library, project_root=tmp_path
    )
    # Both normal names + both match info + both bound → ambiguous.
    assert all(a["action"] == ACTION_MANUAL for a in plan["actions"])
    assert all(a["confirmed"] is False for a in plan["actions"])
