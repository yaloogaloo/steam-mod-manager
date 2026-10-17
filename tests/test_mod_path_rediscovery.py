"""Rename / rediscovery must run before MISS. Folder names are never identity."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import backup_root, mark_missing, reconcile_folder_presence
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from services.mod_presence import (
    ENTITY_LIVE,
    ENTITY_MISS,
    RECOVERY_AMBIGUOUS,
    attempt_recovery,
    entity_state,
    last_rediscovery_stats,
    rediscover_entity_path,
)
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "rediscover.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _create(
    db: DatabaseManager,
    *,
    library: Path,
    folder: str,
    workshop_id: str,
    title: str,
    game: str = "GameA",
    app_id: int = APP_ID,
) -> tuple[Path, str, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=app_id,
            game_name=game,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    path = library / game / folder
    path.mkdir(parents=True)
    (path / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        path,
        internal_id=frozen,
        title=title,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=app_id,
        game_name=game,
        extra={"updated_at": "2020-01-01T00:00:00+00:00"},
    )
    bind_managed_path(db, pk, path, game_name=game, title=title)
    db.update_mod_identity_fields(
        pk, workspace_id=str(created.workspace_id or workshop_id)
    )
    sync_after_metadata_change(pk, path, "edit", wait=True)
    return path, pk, frozen


def test_rename_does_not_enter_miss_and_updates_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Religion Expanded", workshop_id="1178185727",
        title="Religion Expanded",
    )
    backup_before = (backup_root(pk) / "metadata.json").read_text(encoding="utf-8")
    ws_before = str(db.get_mod_backup_row(pk).get("workspace_id") or "")
    renamed = folder.parent / "Religion Expanded v2"
    folder.rename(renamed)
    assert not folder.exists()
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_LIVE
    row = db.get_mod_backup_row(pk)
    assert Path(str(row.get("last_known_path") or "")).resolve() == renamed.resolve()
    assert str(row.get("internal_id") or "") == frozen
    assert str(row.get("workspace_id") or "") == ws_before
    assert int(row.get("folder_present") or 0) == 1
    backup_after = (backup_root(pk) / "metadata.json").read_text(encoding="utf-8")
    from services.mod_identity import read_entity_key

    bak_before = json.loads(backup_before)
    bak_after = json.loads(backup_after)
    assert read_entity_key(bak_after) == frozen
    assert read_entity_key(bak_before) == frozen
    assert bak_after.get("workspace_id") == bak_before.get("workspace_id") == ws_before
    info = read_info_metadata_dict(renamed) or {}
    assert info.get("internal_id") == frozen
    assert "entity_key" not in info
    assert info.get("workspace_id") == ws_before
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 1


def test_rename_survives_second_reconcile(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Alpha", workshop_id="991001", title="Alpha"
    )
    renamed = folder.parent / "AlphaRenamed"
    folder.rename(renamed)
    reconcile_folder_presence(library)
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_LIVE
    assert Path(str(db.get_mod_backup_row(pk).get("last_known_path") or "")).resolve() == renamed.resolve()
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_other_mod_info_is_not_claimed(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Mine", workshop_id="991002", title="Mine"
    )
    other, other_pk, other_frozen = _create(
        db, library=library, folder="Other", workshop_id="991003", title="Other"
    )
    shutil.rmtree(folder)
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_MISS
    row = db.get_mod_backup_row(pk)
    assert Path(str(row.get("last_known_path") or "")).resolve() == folder.resolve()
    assert str(row.get("internal_id") or "") == frozen
    assert str(db.get_mod_backup_row(other_pk).get("internal_id") or "") == other_frozen
    assert Path(str(db.get_mod_backup_row(other_pk).get("last_known_path") or "")).resolve() == other.resolve()
    assert entity_state(other_pk, db=db) == ENTITY_LIVE


def test_internal_id_match_workspace_conflict_not_adopted(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Clash", workshop_id="991004", title="Clash"
    )
    renamed = folder.parent / "ClashMoved"
    folder.rename(renamed)
    write_info_sidecar(
        renamed,
        internal_id=frozen,
        title="Clash",
        external_id="99999",
        workspace_id="99999",
        app_id=APP_ID,
        game_name="GameA",
    )
    old = str(db.get_mod_backup_row(pk).get("last_known_path") or "")
    result = rediscover_entity_path(pk, db=db, library_root=library)
    assert result.success is False
    assert result.reason == "workspace_mismatch"
    assert entity_state(pk, db=db) != ENTITY_LIVE or Path(old).is_dir()
    row = db.get_mod_backup_row(pk)
    assert Path(str(row.get("last_known_path") or "")).resolve() == Path(old).resolve()
    assert str(row.get("internal_id") or "") == frozen
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_MISS


def test_multiple_candidates_are_not_guessed(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Dup", workshop_id="991005", title="Dup"
    )
    sidecar = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    shutil.rmtree(folder)
    a = folder.parent / "DupA"
    b = folder.parent / "DupB"
    for dest in (a, b):
        dest.mkdir(parents=True)
        (dest / INFO_DIR_NAME).mkdir()
        (dest / INFO_DIR_NAME / METADATA_FILENAME).write_text(sidecar, encoding="utf-8")
        (dest / "payload.txt").write_text("x", encoding="utf-8")
    result = rediscover_entity_path(pk, db=db, library_root=library)
    assert result.success is False
    assert result.state == RECOVERY_AMBIGUOUS
    assert result.reason == "ambiguous_candidates"
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_MISS
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 1
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_folder_without_info_internal_id_is_not_a_candidate(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Bare", workshop_id="991006", title="Bare"
    )
    renamed = folder.parent / "BareMoved"
    folder.rename(renamed)
    (renamed / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps({"title": "Bare", "workspace_id": "991006"}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = rediscover_entity_path(pk, db=db, library_root=library)
    assert result.success is False
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_MISS
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_true_delete_becomes_miss_and_backup_takeover_works(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Gone", workshop_id="991007", title="Gone"
    )
    assert (backup_root(pk) / "metadata.json").is_file()
    shutil.rmtree(folder)
    mark_missing(pk)
    reconcile_folder_presence(library)
    assert entity_state(pk, db=db) == ENTITY_MISS
    assert not folder.exists()
    bak = json.loads((backup_root(pk) / "metadata.json").read_text(encoding="utf-8"))
    from services.mod_identity import read_entity_key

    assert read_entity_key(bak) == frozen
    assert bak.get("title") == "Gone"


def test_old_path_return_uses_two_evidence_recovery(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="Back", workshop_id="991008", title="Back"
    )
    shutil.rmtree(folder)
    mark_missing(pk)
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="Back",
        external_id="991008",
        workspace_id="991008",
        app_id=APP_ID,
        game_name="GameA",
        extra={"updated_at": "2020-01-01T00:00:00+00:00"},
    )
    result = attempt_recovery(pk, db=db)
    assert result.success is True
    assert result.internal_id == frozen
    assert entity_state(pk, db=db) == ENTITY_LIVE
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 1


def test_rediscovery_only_runs_for_missing_paths(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db, library=library, folder="Stay", workshop_id="991009", title="Stay"
    )
    decoy_root = library / "OtherGame"
    decoy = decoy_root / "Decoy"
    decoy.mkdir(parents=True)
    (decoy / INFO_DIR_NAME).mkdir()
    (decoy / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps({"internal_id": "should-not-read", "title": "Decoy"}),
        encoding="utf-8",
    )
    reconcile_folder_presence(library)
    stats = last_rediscovery_stats()
    assert stats["missing_attempted"] == 0
    assert stats["backup_scanned"] is False
    game_root = str(folder.parent.resolve())
    assert game_root not in stats["roots"] or stats["missing_attempted"] == 0
    assert not any("OtherGame" in r for r in stats["roots"])
    assert not any("mod_backup" in r.replace("\\", "/") for r in stats["roots"])
    assert entity_state(pk, db=db) == ENTITY_LIVE


def test_rediscovery_does_not_scan_other_games_or_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db, library=library, folder="ScanMe", workshop_id="991010", title="ScanMe"
    )
    decoy = library / "OtherGame" / "Decoy"
    decoy.mkdir(parents=True)
    (decoy / INFO_DIR_NAME).mkdir()
    (decoy / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps({"internal_id": frozen, "title": "Trap", "workspace_id": "991010"}),
        encoding="utf-8",
    )
    backup_trap = data_root / "mod_backup" / "trap"
    backup_trap.mkdir(parents=True)
    (backup_trap / "metadata.json").write_text("{}", encoding="utf-8")
    renamed = folder.parent / "ScanMeMoved"
    folder.rename(renamed)
    reconcile_folder_presence(library)
    stats = last_rediscovery_stats()
    assert stats["missing_attempted"] >= 1
    assert stats["backup_scanned"] is False
    assert any(Path(r).resolve() == folder.parent.resolve() for r in stats["roots"])
    assert not any("OtherGame" in r for r in stats["roots"])
    assert not any("mod_backup" in r.replace("\\", "/") for r in stats["roots"])
    assert entity_state(pk, db=db) == ENTITY_LIVE
    assert Path(str(db.get_mod_backup_row(pk).get("last_known_path") or "")).resolve() == renamed.resolve()
