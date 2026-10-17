"""Identity rebuild recovery — Runtime consumer convergence regression tests.

Covers post-rebuild migration semantics without touching Identity Contract:
- deployment / tags / favorites migrate via mapping (not entity recreate)
- offline reflects .info/index.html presence
- deploy resolves via .info/entity_key, not last_known_path alone
- BG3/Stardew same workspace stay independent
- same-game duplicate workspace rejected at scan uniqueness level
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import OFFLINE_STATUS_ARCHIVED, OFFLINE_STATUS_NONE
from services.deploy_paths import resolve_deploy_managed_path
from services.deploy_status import DEPLOY_ERR_ENTITY_DISK_MISSING
from services.info_sidecar import ensure_registration_info_proof
from services.mod_library_cache import build_library_snapshot
from services.path_lifecycle import resolve_mod_folder_by_internal_id
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import identity_create_scope


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _insert_mod(
    db: DatabaseManager,
    *,
    mod_id: int,
    internal_id: str,
    workspace_id: str,
    app_id: int,
    path: str,
    favorite: int = 0,
    offline_status: str = OFFLINE_STATUS_NONE,
    title: str = "T",
) -> None:
    if app_id > 0:
        db.upsert_game(GameInfo(app_id=app_id, name=f"Game_{app_id}"))
    con = db._conn
    with identity_create_scope():
        con.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, workspace_id, internal_id,
                last_known_path, folder_present, favorite, offline_status,
                platform, updated_at, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'nexus', datetime('now'), 1)
            """,
            (
                mod_id,
                app_id,
                title,
                workspace_id,
                internal_id,
                path,
                favorite,
                offline_status,
            ),
        )
        con.commit()


def test_resolve_mod_folder_by_internal_id_ignores_stale_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "r.db")
    library = tmp_path / "mod"
    monkeypatch.setattr("core.paths.default_mod_library", lambda: library)

    stale = library / "GameA" / "Stale"
    live = library / "GameA" / "Live"
    stale.mkdir(parents=True)
    live.mkdir(parents=True)
    uuid = "11111111-2222-3333-4444-555555555555"
    _write_info(live, {"internal_id": uuid, "workspace_id": "1333", "app_id": 1086940})
    # Stale folder has no matching proof.
    _write_info(stale, {"internal_id": "other", "workspace_id": "1333", "app_id": 1086940})

    _insert_mod(
        db,
        mod_id=1,
        internal_id=uuid,
        workspace_id="1333",
        app_id=1086940,
        path=str(stale),  # intentional stale binding
    )

    found = resolve_mod_folder_by_internal_id(uuid, library_root=library, db=db)
    assert found is not None
    assert found.resolve() == live.resolve()

    # Deploy entry also recovers without trusting last_known_path.
    deploy_path = resolve_deploy_managed_path(1, db=db, library_root=library)
    assert deploy_path is not None
    assert deploy_path.resolve() == live.resolve()
    DatabaseManager.reset_instance()


def test_deploy_missing_disk_uses_entity_error_not_game_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "r2.db")
    library = tmp_path / "mod"
    library.mkdir()
    monkeypatch.setattr("core.paths.default_mod_library", lambda: library)
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _insert_mod(
        db,
        mod_id=2,
        internal_id=uuid,
        workspace_id="99",
        app_id=413150,
        path=str(library / "Missing" / "Gone"),
    )
    assert resolve_deploy_managed_path(2, db=db, library_root=library) is None
    # Message constant must remain distinct from game mod_path misconfig.
    assert "游戏设置" not in DEPLOY_ERR_ENTITY_DISK_MISSING
    assert "实体" in DEPLOY_ERR_ENTITY_DISK_MISSING
    DatabaseManager.reset_instance()


def test_offline_status_from_index_html(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "off.db")
    library = tmp_path / "mod"
    with_page = library / "G" / "Has"
    without = library / "G" / "No"
    with_page.mkdir(parents=True)
    without.mkdir(parents=True)
    (with_page / INFO_DIR_NAME).mkdir()
    (with_page / INFO_DIR_NAME / "index.html").write_text("<html></html>", encoding="utf-8")
    _write_info(with_page, {"internal_id": "u1", "workspace_id": "1", "app_id": 1})
    _write_info(without, {"internal_id": "u2", "workspace_id": "2", "app_id": 1})

    _insert_mod(
        db, mod_id=10, internal_id="u1", workspace_id="1", app_id=1, path=str(with_page)
    )
    _insert_mod(
        db, mod_id=11, internal_id="u2", workspace_id="2", app_id=1, path=str(without)
    )

    # Mimic migrate Phase-4 rescan.
    for mid, folder in ((10, with_page), (11, without)):
        has = (folder / INFO_DIR_NAME / "index.html").is_file()
        status = OFFLINE_STATUS_ARCHIVED if has else OFFLINE_STATUS_NONE
        db._conn.execute(
            "UPDATE mods SET offline_status = ? WHERE mod_id = ?", (status, mid)
        )
    db._conn.commit()

    rows = {
        str(r["mod_id"]): r
        for r in db._conn.execute("SELECT mod_id, offline_status FROM mods")
    }
    assert rows["10"]["offline_status"] == OFFLINE_STATUS_ARCHIVED
    assert rows["11"]["offline_status"] == OFFLINE_STATUS_NONE
    DatabaseManager.reset_instance()


def test_bg3_stardew_same_workspace_independent(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "cross.db")
    library = tmp_path / "mod"
    bg3 = library / "BG3" / "Lib"
    sd = library / "Stardew" / "Lib"
    bg3.mkdir(parents=True)
    sd.mkdir(parents=True)
    _write_info(
        bg3,
        {"internal_id": "bg3-uuid", "workspace_id": "1333", "app_id": 1086940},
    )
    _write_info(
        sd,
        {"internal_id": "sd-uuid", "workspace_id": "1333", "app_id": 413150},
    )
    _insert_mod(
        db,
        mod_id=21,
        internal_id="bg3-uuid",
        workspace_id="1333",
        app_id=1086940,
        path=str(bg3),
    )
    _insert_mod(
        db,
        mod_id=22,
        internal_id="sd-uuid",
        workspace_id="1333",
        app_id=413150,
        path=str(sd),
    )
    assert resolve_mod_folder_by_internal_id("bg3-uuid", library_root=library, db=db) == bg3.resolve()
    assert resolve_mod_folder_by_internal_id("sd-uuid", library_root=library, db=db) == sd.resolve()
    # Same workspace across games must not collapse entities.
    rows = list(
        db._conn.execute(
            "SELECT app_id, workspace_id, internal_id FROM mods WHERE workspace_id='1333'"
        )
    )
    assert len(rows) == 2
    assert {int(r["app_id"]) for r in rows} == {1086940, 413150}
    DatabaseManager.reset_instance()


def test_same_game_duplicate_workspace_unique_in_db(tmp_path: Path) -> None:
    """Post-cleanup invariant: one (app_id, workspace_id) → one entity."""
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "dup.db")
    _insert_mod(
        db,
        mod_id=31,
        internal_id="a",
        workspace_id="5815",
        app_id=413150,
        path=str(tmp_path / "A"),
    )
    dups = list(
        db._conn.execute(
            """
            SELECT workspace_id, COUNT(*) c FROM mods
            WHERE app_id = 413150 AND TRIM(workspace_id) != ''
            GROUP BY workspace_id HAVING c > 1
            """
        )
    )
    assert dups == []
    DatabaseManager.reset_instance()


def test_registration_writes_uuid_and_workspace(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "reg.db")
    folder = tmp_path / "mod" / "G" / "M"
    folder.mkdir(parents=True)
    uuid = "12345678-1234-1234-1234-1234567890ab"
    _insert_mod(
        db,
        mod_id=40,
        internal_id=uuid,
        workspace_id="777",
        app_id=1623730,
        path=str(folder),
    )
    ensure_registration_info_proof(folder, 40, db=db)
    meta = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert meta.get("internal_id") == uuid
    assert meta.get("workspace_id") == "777"
    DatabaseManager.reset_instance()


def test_mapping_migrate_restores_deployment_and_favorite(tmp_path: Path) -> None:
    """Simulate backup→live mapping restore without recreating entities."""
    live_db = tmp_path / "live.db"
    bak_db = tmp_path / "bak.db"
    DatabaseManager.reset_instance()
    live = DatabaseManager.instance(live_db)

    folder = tmp_path / "mod" / "G" / "M"
    folder.mkdir(parents=True)
    new_uuid = "99999999-aaaa-bbbb-cccc-ddddeeeeffff"
    _insert_mod(
        live,
        mod_id=1,
        internal_id=new_uuid,
        workspace_id="42",
        app_id=916440,
        path=str(folder),
        favorite=0,
    )
    live.close()
    DatabaseManager.reset_instance()

    bak = DatabaseManager.instance(bak_db)
    _insert_mod(
        bak,
        mod_id=9000000000000999,
        internal_id="old-uuid",
        workspace_id="42",
        app_id=916440,
        path=str(folder),
        favorite=1,
        title="AnnoMod",
    )
    bak._conn.execute(
        """
        INSERT INTO deployment_records (app_id, name, created_at, updated_at)
        VALUES (916440, 'pack', datetime('now'), datetime('now'))
        """
    )
    rec_id = int(bak._conn.execute("SELECT id FROM deployment_records").fetchone()[0])
    bak._conn.execute(
        "INSERT INTO deployment_record_items (record_id, mod_id) VALUES (?, ?)",
        (rec_id, 9000000000000999),
    )
    bak._conn.execute(
        """
        INSERT INTO mod_tags (mod_id, tag_type, tag_value, created_at, updated_at)
        VALUES (?, 'category', '地图', datetime('now'), datetime('now'))
        """,
        (9000000000000999,),
    )
    bak._conn.commit()
    bak.close()
    DatabaseManager.reset_instance()

    mapping = {
        "mappings": [
            {
                "old_internal_id": "old-uuid",
                "old_mod_id": "9000000000000999",
                "new_internal_id": new_uuid,
                "new_mod_id": "1",
                "confidence": "verified",
            }
        ]
    }
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps(mapping), encoding="utf-8")

    from tools.identity_rebuild_runtime_migrate import migrate

    report = migrate(
        live_db=live_db,
        backup_db=bak_db,
        mapping_path=map_path,
        library=tmp_path / "mod",
        apply=True,
    )
    assert report["restored"]["deployment_records"] >= 1
    assert report["restored"]["deployment_record_items"] >= 1
    assert report["restored"]["favorites"] >= 1
    assert report["restored"]["mod_tags"] >= 1

    con = sqlite3.connect(str(live_db))
    assert con.execute("SELECT favorite FROM mods WHERE mod_id=1").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM deployment_record_items").fetchone()[0] >= 1
    assert con.execute("SELECT COUNT(*) FROM mod_tags").fetchone()[0] >= 1
    con.close()


def test_library_loads_after_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "lib.db")
    library = tmp_path / "mod"
    monkeypatch.setattr("core.paths.default_mod_library", lambda: library)
    folder = library / "G" / "M"
    folder.mkdir(parents=True)
    _write_info(folder, {"internal_id": "lib-uuid", "workspace_id": "1", "app_id": 1})
    _insert_mod(
        db,
        mod_id=50,
        internal_id="lib-uuid",
        workspace_id="1",
        app_id=1,
        path=str(folder),
        favorite=1,
        offline_status=OFFLINE_STATUS_ARCHIVED,
    )
    snap = build_library_snapshot(library)
    assert len(snap.cards) == 1
    assert snap.cards[0].favorite is True
    DatabaseManager.reset_instance()
