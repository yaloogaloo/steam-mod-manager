"""P0 identity lifecycle: Steam workshop id must never become an internal 900… id."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    NON_STEAM_MOD_ID_BASE,
    PLATFORM_STEAM,
    is_internal_mod_id,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import (
    IDENTITY_CONFLICT,
    allocate_internal_id,
    classify_identity_state,
    persist_workspace_id,
    validate_binding,
)
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_mod_identity
from services.mod_identity_validator import IdentityIssueCode, validate_db_row_identity
from services.mod_refresh import refresh_mod
from services.path_lifecycle import commit_path_change

STEAM_ID = "3591453758"
INTERNAL_POLLUTED = "9000000000003438"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_lifecycle.db")
    manager.upsert_game(
        GameInfo(app_id=3167020, name="逃离鸭科夫", folder_name="逃离鸭科夫")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_steam_folder(
    library: Path,
    *,
    folder_name: str,
    published_file_id: str,
    extra: dict | None = None,
) -> Path:
    folder = library / "逃离鸭科夫" / folder_name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": published_file_id,
        "title": folder_name,
        "app_id": 3167020,
        "game_name": "逃离鸭科夫",
        "source_type": "steam",
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={published_file_id}",
        "workspace_id": published_file_id,
    }
    if extra:
        payload.update(extra)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "info.ini").write_text("[Mod]\nname=x\n", encoding="utf-8")
    return folder


def _identity_tuple(db: DatabaseManager, mid: str) -> tuple[str, str, str]:
    info = db.get_mod_display_info(mid)
    assert info is not None
    return str(info.mod_id), str(info.external_id or ""), str(info.workspace_id or "")


def test_1_steam_reconcile_identity_unchanged(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    _write_steam_folder(library, folder_name="Collectibles", published_file_id=STEAM_ID)
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    before = _identity_tuple(db, STEAM_ID)
    reconcile_library(library)
    assert _identity_tuple(db, STEAM_ID) == before
    assert db.get_mod(INTERNAL_POLLUTED) is None
    assert not is_internal_mod_id(before[0])


def test_2_offline_save_does_not_create_identity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_steam_folder(
        library, folder_name="Collectibles", published_file_id=STEAM_ID
    )
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    db.update_mod_identity_fields(STEAM_ID, last_known_path=str(folder.resolve()))
    before = _identity_tuple(db, STEAM_ID)
    db.update_mod_offline_status(STEAM_ID, status="archived", provider="steam_archive")
    assert _identity_tuple(db, STEAM_ID) == before
    assert db.get_mod(STEAM_ID) is not None


def test_3_metadata_refresh_identity_unchanged(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder = _write_steam_folder(
        library, folder_name="Collectibles", published_file_id=STEAM_ID
    )
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    db.update_mod_identity_fields(STEAM_ID, last_known_path=str(folder.resolve()))
    before = _identity_tuple(db, STEAM_ID)

    monkeypatch.setattr(
        "core.steam_api.SteamWorkshopClient.refresh_details",
        MagicMock(
            return_value=[
                ModMetadata(
                    published_file_id=STEAM_ID,
                    title="更多收集品",
                    app_id=3167020,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "core.steam_api.SteamWorkshopClient.fetch_and_save_cover",
        lambda *a, **k: None,
    )
    result = refresh_mod(
        STEAM_ID, folder, platform=PLATFORM_STEAM, library_root=library, db=db
    )
    assert result.success
    after = _identity_tuple(db, STEAM_ID)
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2]
    assert db.get_mod(INTERNAL_POLLUTED) is None


def test_4_deploy_identity_unchanged(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder = _write_steam_folder(
        library, folder_name="Collectibles", published_file_id=STEAM_ID
    )
    game = tmp_path / "game" / "Duckov_Data" / "Mods"
    game.mkdir(parents=True)
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    db.update_mod_identity_fields(
        STEAM_ID, last_known_path=str(folder.resolve()), app_id=3167020
    )
    db.update_game_deploy_config(
        3167020,
        name="逃离鸭科夫",
        install_path=str(tmp_path / "game"),
        mod_path=str(game),
    )
    before = _identity_tuple(db, STEAM_ID)
    ModDeployer(library_root=library, db=db).deploy_mod(STEAM_ID)
    assert _identity_tuple(db, STEAM_ID) == before


def test_5_restart_reconcile_identity_unchanged(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    _write_steam_folder(library, folder_name="Collectibles", published_file_id=STEAM_ID)
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    reconcile_library(library)
    before = _identity_tuple(db, STEAM_ID)
    path = tmp_path / "identity_lifecycle.db"
    DatabaseManager.reset_instance()
    db2 = DatabaseManager.instance(path)
    reconcile_library(library)
    info = db2.get_mod_display_info(STEAM_ID)
    assert info is not None
    assert (str(info.mod_id), str(info.external_id), str(info.workspace_id)) == before
    DatabaseManager.reset_instance()


def test_6_missing_metadata_restores_existing_db_identity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_steam_folder(
        library, folder_name="Collectibles", published_file_id=STEAM_ID
    )
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    db.update_mod_identity_fields(STEAM_ID, last_known_path=str(folder.resolve()))
    meta = folder / INFO_DIR_NAME / METADATA_FILENAME
    meta.write_text("{}", encoding="utf-8")
    before_count = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    mid, payload, _ = ensure_mod_identity(folder, {"_managed_path": str(folder.resolve())})
    assert mid == STEAM_ID
    assert not is_internal_mod_id(mid)
    after_count = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert after_count == before_count


def test_7_workspace_internal_id_is_conflict(db: DatabaseManager) -> None:
    findings = validate_db_row_identity(
        mod_id=INTERNAL_POLLUTED,
        platform=PLATFORM_STEAM,
        external_id=INTERNAL_POLLUTED,
        workspace_id=INTERNAL_POLLUTED,
        source_url="",
    )
    codes = {f.code for f in findings}
    assert IdentityIssueCode.WORKSPACE_ID_POLLUTION in codes
    ws = persist_workspace_id(
        platform=PLATFORM_STEAM,
        mod_id=INTERNAL_POLLUTED,
        workspace_id=INTERNAL_POLLUTED,
    )
    assert ws == ""
    state = classify_identity_state(
        mod_id=INTERNAL_POLLUTED,
        platform=PLATFORM_STEAM,
        workspace_id=INTERNAL_POLLUTED,
        findings=findings,
    )
    assert state == IDENTITY_CONFLICT


def test_8_external_id_internal_pollution_not_used_as_platform(
    db: DatabaseManager,
) -> None:
    findings = validate_binding(
        mod_id=INTERNAL_POLLUTED,
        platform=PLATFORM_STEAM,
        external_id=INTERNAL_POLLUTED,
        workspace_id="",
    )
    assert any(
        f.code == IdentityIssueCode.INTERNAL_ID_AS_EXTERNAL_ID
        or f.code == IdentityIssueCode.STEAM_ID_POLLUTION
        for f in findings
    )
    from services.identity_service import sanitize_platform_external_id

    assert sanitize_platform_external_id(
        PLATFORM_STEAM, INTERNAL_POLLUTED, mod_id=INTERNAL_POLLUTED
    ) == ""


def test_9_duplicate_steam_url_does_not_mint_third(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={STEAM_ID}"
    _write_steam_folder(library, folder_name="Collectibles", published_file_id=STEAM_ID)
    ghost = library / "逃离鸭科夫" / f"Unknown Mod {STEAM_ID}"
    info = ghost / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": INTERNAL_POLLUTED,
                "title": f"Unknown Mod {STEAM_ID}",
                "url": url,
                "source_type": "steam",
            }
        ),
        encoding="utf-8",
    )
    (ghost / "info.ini").write_text("[Mod]\nname=x\n", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    ghost_mid = str(allocate_internal_id(db))
    db.update_mod_identity_fields(
        ghost_mid,
        platform=PLATFORM_STEAM,
        source_url=url,
        last_known_path=str(ghost.resolve()),
    )
    count_before = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    reconcile_library(library)
    count_after = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert count_after <= count_before
    assert db.get_mod(STEAM_ID) is not None
    steam_ids = [
        str(r["mod_id"])
        for r in db._conn.execute(
            "SELECT mod_id FROM mods WHERE source_url LIKE ?", (f"%{STEAM_ID}%",)
        ).fetchall()
    ]
    assert STEAM_ID in steam_ids
    # Reconcile must not mint another 900… row for the leftover folder.
    internals = [m for m in steam_ids if is_internal_mod_id(m)]
    assert len(set(internals)) <= 1


def test_10_gui_lifecycle_services_identity_stable(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder = _write_steam_folder(
        library, folder_name="Collectibles", published_file_id=STEAM_ID
    )
    db.upsert_mod(
        ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020)
    )
    db.update_mod_identity_fields(
        STEAM_ID, last_known_path=str(folder.resolve()), app_id=3167020
    )
    game = tmp_path / "game" / "Duckov_Data" / "Mods"
    game.mkdir(parents=True)
    db.update_game_deploy_config(
        3167020,
        name="逃离鸭科夫",
        install_path=str(tmp_path / "game"),
        mod_path=str(game),
    )
    before = _identity_tuple(db, STEAM_ID)
    monkeypatch.setattr(
        "core.steam_api.SteamWorkshopClient.refresh_details",
        MagicMock(
            return_value=[
                ModMetadata(published_file_id=STEAM_ID, title="更多收集品", app_id=3167020)
            ]
        ),
    )
    monkeypatch.setattr(
        "core.steam_api.SteamWorkshopClient.fetch_and_save_cover", lambda *a, **k: None
    )
    refresh_mod(STEAM_ID, folder, platform=PLATFORM_STEAM, library_root=library, db=db)
    db.update_mod_offline_status(STEAM_ID, status="archived", provider="steam_archive")
    ModDeployer(library_root=library, db=db).deploy_mod(STEAM_ID)
    new_folder = folder.parent / "RenamedCollectibles"
    folder.rename(new_folder)
    commit_path_change(
        STEAM_ID, old_path=folder, new_path=new_folder, renamed=True, db=db
    )
    reconcile_library(library)
    after = _identity_tuple(db, STEAM_ID)
    assert after == before
    row = db.get_mod_backup_row(STEAM_ID) or {}
    assert str(row.get("last_known_path") or "").endswith("RenamedCollectibles")


def test_unknown_mod_folder_reconcile_does_not_allocate(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    ghost = library / "逃离鸭科夫" / f"Unknown Mod {STEAM_ID}"
    info = ghost / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text("{}", encoding="utf-8")
    (ghost / "info.ini").write_text("[Mod]\nname=x\n", encoding="utf-8")
    reconcile_library(library)
    internals = db._conn.execute(
        "SELECT COUNT(*) FROM mods WHERE mod_id >= ?",
        (NON_STEAM_MOD_ID_BASE,),
    ).fetchone()[0]
    assert internals == 0
    assert db.get_mod(STEAM_ID) is None
    sidecar = json.loads((info / METADATA_FILENAME).read_text(encoding="utf-8"))
    pub = str(sidecar.get("published_file_id") or "")
    assert not is_internal_mod_id(pub)
    assert sidecar.get("identity_status") == "unresolved"
