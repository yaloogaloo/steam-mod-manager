"""P0: folder rename / reconcile must not mint a second Workspace ID.

These tests are the identity-lifecycle contract. Several currently fail on
the live reconcile path (Nexus workspace_id treated as Steam PK after path
change). Do not weaken assertions to match the bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_invariants import (
    DUPLICATE_WORKSPACE_ID_CREATION,
    FILESYSTEM_PATH_USED_AS_SOLE_MOD_IDENTITY,
    FOLDER_RENAME_CAUSES_NEW_MOD,
    INVALID_SOURCE_URL_REHYDRATION,
    RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF,
    SIDECAR_INTERNAL_ID_USED_AS_PLATFORM_ID,
    SIDECAR_REHYDRATES_INVALID_IDENTITY,
    UNSAFE_RECONCILE_IDENTITY_FALLBACK,
    scan_invalid_entities,
    scan_reconcile_identity_lifecycle,
)
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.mod_identity import INTERNAL_ID_KEY
from services.mod_metadata_resolver import list_visible_mods

WS = "10782"
APP = 292030
GAME = "巫师三"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "rename_id.db")
    manager.upsert_game(GameInfo(app_id=APP, name=GAME, folder_name=GAME))
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    yield manager
    DatabaseManager.reset_instance()


def _write_folder(library: Path, game: str, name: str, payload: dict) -> Path:
    folder = library / game / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.pak").write_bytes(b"pak")
    return folder


def _rows_for_workspace(db: DatabaseManager, workspace_id: str) -> list[dict]:
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT mod_id, platform, external_id, workspace_id, last_known_path,
                   folder_present
            FROM mods WHERE TRIM(COALESCE(workspace_id,'')) = ?
            """,
            (workspace_id,),
        ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _seed_nexus(db: DatabaseManager, library: Path, folder_name: str, payload: dict) -> tuple[str, Path]:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        title="Flashbacks",
        app_id=APP,
        game_name=GAME,
        operation="import",
    )
    folder = _write_folder(library, GAME, folder_name, payload)
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id=WS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        app_id=APP,
        title="Flashbacks",
        internal_id=str(payload.get(INTERNAL_ID_KEY) or "") or None,
    )
    return str(created.mod_id), folder


def test_folder_rename_does_not_create_new_mod(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "published_file_id": WS,
            "title": "Flashbacks",
            "display_name": "Flashbacks",
            "game_name": GAME,
        },
    )
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    renamed = folder.parent / "Flashbacks Renamed"
    folder.rename(renamed)
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert db.get_mod(mid) is not None
    assert db.get_mod(WS) is None


def test_folder_rename_preserves_internal_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Renamed")
    reconcile_library(library)
    assert db.get_mod(mid) is not None
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.mod_id) == mid


def test_folder_rename_preserves_workspace_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Renamed")
    reconcile_library(library)
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.workspace_id) == WS
    assert len(_rows_for_workspace(db, WS)) == 1


def test_folder_rename_preserves_external_id(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Renamed")
    reconcile_library(library)
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.external_id) == WS
    assert str(info.platform) == PLATFORM_NEXUS


def test_reconcile_rebinds_existing_identity_after_path_change(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "published_file_id": WS,
            "title": "Flashbacks",
            "display_name": "Flashbacks",
            "game_name": GAME,
        },
    )
    renamed = folder.parent / "Flashbacks Renamed"
    folder.rename(renamed)
    result = reconcile_library(library)
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == renamed.resolve()
    assert int(row.get("folder_present") or 0) == 1
    assert db.get_mod(WS) is None
    assert result.imported == 0


def test_duplicate_workspace_identity_cannot_be_created(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "published_file_id": WS,
            "title": "Flashbacks",
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Renamed")
    reconcile_library(library)
    ws_rows = _rows_for_workspace(db, WS)
    assert [str(r["mod_id"]) for r in ws_rows] == [mid]
    report = scan_invalid_entities(library, db=db)
    dup = [f for f in report.findings if f.violation_code == DUPLICATE_WORKSPACE_ID_CREATION]
    assert dup == []


def test_reconcile_does_not_create_without_identity_proof(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_folder(
        library,
        GAME,
        "Orphan",
        {"title": "Orphan", "display_name": "Orphan", "game_name": GAME},
    )
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert any("IDENTITY_UNRESOLVED" in n for n in result.notes)
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert data.get("identity_status") == "unresolved"


def test_unknown_folder_remains_unresolved_when_identity_is_ambiguous(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {"title": "Flashbacks", "display_name": "Flashbacks", "game_name": GAME},
    )
    folder.rename(folder.parent / "Renamed")
    result = reconcile_library(library)
    assert db.get_mod(mid) is not None
    assert db.get_mod(WS) is None
    row = db.get_mod_backup_row(mid) or {}
    # Ambiguous rename: do not INSERT; existing row stays, folder unresolved.
    assert any("IDENTITY_UNRESOLVED" in n for n in result.notes)
    assert str(row.get("last_known_path") or "").endswith("Flashbacks")
    assert int(row.get("folder_present") or 0) == 0


@pytest.mark.parametrize(
    "platform,payload,title,app_id,game",
    [
        (
            PLATFORM_STEAM,
            {
                "published_file_id": "3571849225",
                "workspace_id": "3571849225",
                "source_type": "steam",
                "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=3571849225",
                "title": "SteamMod",
                "app_id": APP,
                "game_name": GAME,
            },
            "SteamMod",
            APP,
            GAME,
        ),
        (
            PLATFORM_NEXUS,
            {
                "workspace_id": WS,
                "external_id": WS,
                "source_type": "nexus",
                "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
                "title": "NexusMod",
                "app_id": APP,
                "game_name": GAME,
            },
            "NexusMod",
            APP,
            GAME,
        ),
        (
            PLATFORM_GITHUB,
            {
                "source_type": "github",
                "url": "https://github.com/owner/repo",
                "external_id": "owner/repo",
                "title": "GHMod",
                "app_id": 100,
                "game_name": "SomeGame",
            },
            "GHMod",
            100,
            "SomeGame",
        ),
    ],
)
def test_folder_rename_preserves_identity_across_platforms(
    db: DatabaseManager,
    tmp_path: Path,
    platform: str,
    payload: dict,
    title: str,
    app_id: int,
    game: str,
) -> None:
    library = tmp_path / "mod"
    if platform == PLATFORM_STEAM:
        wid = "3571849225"
        db.upsert_mod(
            ModMetadata(published_file_id=wid, title=title, app_id=app_id, url=payload["url"])
        )
        folder = _write_folder(library, game, title, payload)
        db.update_mod_identity_fields(
            wid,
            last_known_path=str(folder.resolve()),
            folder_present=True,
            platform=PLATFORM_STEAM,
            workspace_id=wid,
            external_id=wid,
            app_id=app_id,
        )
        mid = wid
        ws = wid
    else:
        created = create_mod_identity(
            db,
            platform=platform,
            external_id=str(payload.get("external_id") or "owner/repo"),
            source_url=str(payload.get("url") or ""),
            title=title,
            app_id=app_id,
            game_name=game,
            operation="import",
        )
        body = dict(payload)
        if platform == PLATFORM_GITHUB:
            body["workspace_id"] = created.workspace_id
        folder = _write_folder(library, game, title, body)
        db.update_mod_identity_fields(
            created.mod_id,
            last_known_path=str(folder.resolve()),
            folder_present=True,
            platform=platform,
            workspace_id=str(body.get("workspace_id") or created.workspace_id),
            external_id=str(payload.get("external_id") or ""),
            source_url=str(payload.get("url") or ""),
            app_id=app_id,
        )
        mid = str(created.mod_id)
        ws = str(body.get("workspace_id") or created.workspace_id)

    before = db.get_mod_display_info(mid)
    assert before is not None
    folder.rename(folder.parent / f"{title} Renamed")
    reconcile_library(library)
    after = db.get_mod_display_info(mid)
    assert after is not None
    assert str(after.mod_id) == mid
    assert str(after.workspace_id) == str(before.workspace_id) == ws
    assert str(after.external_id) == str(before.external_id)
    assert db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"] == 1


def test_leftover_empty_folder_does_not_duplicate_workspace_cards(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    uuid = "eb8a3ddd-1886-40fb-85b6-c1b1be8159a3"
    mid, folder = _seed_nexus(
        db,
        library,
        "Empty Mod 0798b9fd",
        {
            "published_file_id": "",
            "workspace_id": WS,
            INTERNAL_ID_KEY: uuid,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "game_name": GAME,
            "identity_status": "complete",
        },
    )
    # Simulate production: sidecar still stores Internal PK as published_file_id.
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["published_file_id"] = mid
    (folder / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    renamed = folder.parent / "Flashbacks - Something you've already seen"
    import shutil

    shutil.copytree(folder, renamed)
    reconcile_library(library)
    visible = list_visible_mods(library, GAME)
    ws_rows = _rows_for_workspace(db, WS)
    assert len(ws_rows) == 1
    assert len(visible) == 1


def test_folder_move_does_not_create_new_mod(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "published_file_id": WS,
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    dest_root = library / "SomeGame"
    dest_root.mkdir(parents=True, exist_ok=True)
    moved = dest_root / folder.name
    folder.rename(moved)
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    info = db.get_mod_display_info(mid)
    assert after == before
    assert info is not None
    assert str(info.mod_id) == mid
    assert str(info.workspace_id) == WS
    assert str(info.external_id) == WS
    assert db.get_mod(WS) is None


def test_folder_rename_then_second_reconcile_preserves_identity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Rename + refresh, then a second reconcile (restart/refresh)."""
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "published_file_id": WS,
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Flashbacks Restart")
    reconcile_library(library)
    reconcile_library(library)
    assert db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"] == 1
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.workspace_id) == WS
    assert str(info.external_id) == WS
    assert db.get_mod(WS) is None


def test_sidecar_reload_after_rename_does_not_duplicate(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.info_sidecar import apply_sidecar_to_db

    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    renamed = folder.parent / "Flashbacks Sidecar"
    folder.rename(renamed)
    apply_sidecar_to_db(renamed, mod_id=mid, db=db)
    reconcile_library(library)
    assert len(_rows_for_workspace(db, WS)) == 1
    assert db.get_mod(mid) is not None
    assert db.get_mod(WS) is None


def test_static_guards_detect_reconcile_rename_identity_gap() -> None:
    findings = scan_reconcile_identity_lifecycle()
    codes = {f.violation_code for f in findings}
    assert FILESYSTEM_PATH_USED_AS_SOLE_MOD_IDENTITY not in codes
    assert FOLDER_RENAME_CAUSES_NEW_MOD not in codes
    assert RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF not in codes
    assert DUPLICATE_WORKSPACE_ID_CREATION not in codes
    assert UNSAFE_RECONCILE_IDENTITY_FALLBACK not in codes
    assert INVALID_SOURCE_URL_REHYDRATION not in codes
    assert SIDECAR_REHYDRATES_INVALID_IDENTITY not in codes
    assert SIDECAR_INTERNAL_ID_USED_AS_PLATFORM_ID not in codes
    from services.identity_invariants import (
        INTERNAL_ID_LEAKED_TO_PLATFORM_ID,
        INTERNAL_ID_LEAKED_TO_PLATFORM_URL,
        INTERNAL_ID_LEAKED_TO_WORKSPACE_ID,
        LEGACY_ID_USED_AS_NEW_IDENTITY_PROOF,
        MULTIPLE_FILESYSTEM_PATHS_ONE_ENTITY,
        MULTIPLE_VISIBLE_CARDS_ONE_INTERNAL_ID,
        NUMERIC_ID_USED_AS_STEAM_PROOF,
        PUBLISHED_FILE_ID_USED_AS_SOLE_IDENTITY,
        RECONCILE_CREATE_BEFORE_IDENTITY_RESOLUTION,
    )

    assert PUBLISHED_FILE_ID_USED_AS_SOLE_IDENTITY not in codes
    assert NUMERIC_ID_USED_AS_STEAM_PROOF not in codes
    assert INTERNAL_ID_LEAKED_TO_WORKSPACE_ID not in codes
    assert INTERNAL_ID_LEAKED_TO_PLATFORM_ID not in codes
    assert INTERNAL_ID_LEAKED_TO_PLATFORM_URL not in codes
    assert RECONCILE_CREATE_BEFORE_IDENTITY_RESOLUTION not in codes
    assert MULTIPLE_FILESYSTEM_PATHS_ONE_ENTITY not in codes
    assert MULTIPLE_VISIBLE_CARDS_ONE_INTERNAL_ID not in codes
    assert LEGACY_ID_USED_AS_NEW_IDENTITY_PROOF not in codes
    assert findings == []


def test_reconcile_does_not_rehydrate_internal_id_steam_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Incident B: empty DB source_url must not be restored from a fake Steam URL."""
    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1086940, name="Baldur's Gate 3", folder_name="Baldur's Gate 3"))
    uuid = "a2639141-3164-4f7b-9828-280923c5400e"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="title-not-id",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1580",
        title="BetterUI",
        app_id=1086940,
        game_name="Baldur's Gate 3",
        operation="import",
    )
    mid = str(created.mod_id)
    polluted = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}"
    folder = _write_folder(
        library,
        "Baldur's Gate 3",
        "BetterUI",
        {
            "published_file_id": mid,
            INTERNAL_ID_KEY: uuid,
            "workspace_id": "17870313801551871",
            "source_type": "nexus",
            "url": polluted,
            "title": "BetterUI",
            "app_id": 1086940,
            "game_name": "Baldur's Gate 3",
        },
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id="17870313801551871",
        source_url="",
        internal_id=uuid,
        app_id=1086940,
    )
    reconcile_library(library)
    row = db._conn.execute(
        "SELECT source_url FROM mods WHERE CAST(mod_id AS TEXT)=?",
        (mid,),
    ).fetchone()
    url = str(row["source_url"] or "")
    assert f"id={mid}" not in url.replace(" ", "")
    assert "steamcommunity.com" not in url.lower()


def test_apply_sidecar_does_not_restore_invalid_steam_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.info_sidecar import apply_sidecar_to_db

    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1086940, name="Baldur's Gate 3", folder_name="Baldur's Gate 3"))
    uuid = "a2639141-3164-4f7b-9828-280923c5400e"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="title-not-id",
        source_url="",
        title="BetterUI",
        app_id=1086940,
        game_name="Baldur's Gate 3",
        operation="import",
    )
    mid = str(created.mod_id)
    polluted = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}"
    folder = _write_folder(
        library,
        "Baldur's Gate 3",
        "BetterUI",
        {
            "published_file_id": mid,
            INTERNAL_ID_KEY: uuid,
            "workspace_id": "17870313801551871",
            "source_type": "nexus",
            "url": polluted,
            "title": "BetterUI",
        },
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        source_url="",
        internal_id=uuid,
        app_id=1086940,
    )
    apply_sidecar_to_db(folder, mod_id=mid, db=db)
    row = db._conn.execute(
        "SELECT source_url FROM mods WHERE CAST(mod_id AS TEXT)=?",
        (mid,),
    ).fetchone()
    url = str(row["source_url"] or "")
    assert f"id={mid}" not in url.replace(" ", "")


def test_invalid_sidecar_identity_fields_are_not_restored(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Sidecar workspace_id / external_id / platform must pass the Identity Contract."""
    from services.info_sidecar import apply_sidecar_to_db

    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1086940, name="Baldur's Gate 3", folder_name="Baldur's Gate 3"))
    uuid = "a2639141-3164-4f7b-9828-280923c5400e"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="title-not-id",
        source_url="",
        title="BetterUI",
        app_id=1086940,
        game_name="Baldur's Gate 3",
        operation="import",
    )
    mid = str(created.mod_id)
    folder = _write_folder(
        library,
        "Baldur's Gate 3",
        "BetterUI",
        {
            "published_file_id": mid,
            INTERNAL_ID_KEY: uuid,
            "workspace_id": mid,
            "external_id": mid,
            "source_type": "steam",
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}",
            "title": "BetterUI",
        },
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id="17870313801551871",
        external_id="title-not-id",
        source_url="",
        internal_id=uuid,
        app_id=1086940,
    )
    apply_sidecar_to_db(folder, mod_id=mid, db=db)
    reconcile_library(library)
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.workspace_id) != mid
    assert str(info.external_id) != mid
    assert f"id={mid}" not in str(info.source_url or "").replace(" ", "")


def test_folder_rename_twice_preserves_entity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    b = folder.parent / "RenamedB"
    folder.rename(b)
    reconcile_library(library)
    c = b.parent / "RenamedC"
    b.rename(c)
    reconcile_library(library)
    assert db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"] == 1
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.mod_id) == mid
    assert str(info.workspace_id) == WS
    assert str(info.platform) == PLATFORM_NEXUS
    assert len(list_visible_mods(library, GAME)) == 1


def test_numeric_nexus_workspace_id_never_infers_steam(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    folder.rename(folder.parent / "Flashbacks Numeric")
    reconcile_library(library)
    assert db.get_mod(WS) is None
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.platform) == PLATFORM_NEXUS
    assert str(info.workspace_id) == WS


def test_polluted_published_file_id_does_not_infer_steam(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    uuid = "eb8a3ddd-1886-40fb-85b6-c1b1be8159a3"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            INTERNAL_ID_KEY: uuid,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    data = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    data["published_file_id"] = mid
    (folder / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    folder.rename(folder.parent / "Flashbacks Polluted")
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert db.get_mod(WS) is None
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.workspace_id) == WS
    assert str(info.platform) == PLATFORM_NEXUS


def test_empty_mod_polluted_legacy_does_not_duplicate(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    uuid = "eb8a3ddd-1886-40fb-85b6-c1b1be8159a3"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            INTERNAL_ID_KEY: uuid,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    empty = folder.parent / "Empty Mod 0798b9fd"
    empty.mkdir(parents=True, exist_ok=True)
    info = empty / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                INTERNAL_ID_KEY: "c5897d4d-2055-48b2-acc1-37a9caeda2ac",
                "title": "Empty Mod 0798b9fd",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert db.get_mod(WS) is None
    visible = list_visible_mods(library, GAME)
    assert len(visible) == 1
    assert str(visible[0].published_file_id) == mid


def test_case_c_legacy_published_file_id_alone_does_not_create_steam(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_folder(
        library,
        GAME,
        "CaseC",
        {"published_file_id": WS, "title": "CaseC", "game_name": GAME},
    )
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert db.get_mod(WS) is None
    assert any("IDENTITY_UNRESOLVED" in n for n in result.notes)
    data = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert data.get("identity_status") == "unresolved"


def test_metadata_refresh_does_not_create_new_entity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.mod_refresh import refresh_mod

    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        "Flashbacks",
        {
            "workspace_id": WS,
            "external_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "app_id": APP,
            "game_name": GAME,
        },
    )
    renamed = folder.parent / "Flashbacks Refresh"
    folder.rename(renamed)
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    refresh_mod(mid, renamed, platform=PLATFORM_NEXUS, library_root=library, db=db)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert str(info.workspace_id) == WS
    assert db.get_mod(WS) is None

