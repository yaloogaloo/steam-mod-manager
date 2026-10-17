"""Steam Sync / first materialize sidecar hydration: Backup wins over Workshop .info."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    readable_backup_root,
    restore_cover_from_backup,
    restore_offline_from_backup,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import sync_after_metadata_change
from services.sidecar_hydration import (
    ACTION_BACKUP_RESTORE,
    ACTION_NONE,
    ACTION_PRESERVE,
    ACTION_SOURCE_COPY,
    hydrate_managed_sidecar,
)
from services.sync import ModSyncService, SyncOptions
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar

APP_ID = 262060
WID = "2683922974"
TITLE = "Vermintide Mod"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bak_dir(pk: str, frozen: str) -> Path:
    dest = readable_backup_root(frozen, mod_pk=pk)
    assert dest is not None
    return dest


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "hydrate.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _seed_entity(db: DatabaseManager) -> tuple[str, str]:
    created = create_steam_test_mod(
        db, external_id=WID, title=TITLE, app_id=APP_ID, game_name="GameA"
    )
    return str(created.mod_id), str(created.internal_id or "")


def _write_workshop(
    root: Path,
    *,
    with_cover: bytes | None = None,
    with_offline: str | None = None,
    payload: bytes = b"workshop-payload",
) -> Path:
    folder = root / WID
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(payload)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (info / "metadata.json").write_text(
        f'{{"published_file_id":"{WID}","workspace_id":"{WID}","title":"{TITLE}",'
        f'"app_id":{APP_ID},"game_name":"GameA","source_type":"steam"}}',
        encoding="utf-8",
    )
    if with_cover is not None:
        (info / "cover.jpg").write_bytes(with_cover)
    if with_offline is not None:
        off = info / "offline"
        off.mkdir()
        (off / "index.html").write_text(with_offline, encoding="utf-8")
    return folder


def _seed_backup(
    db: DatabaseManager,
    tmp_path: Path,
    pk: str,
    frozen: str,
    *,
    cover: bytes = b"backup-cover-NEW",
    offline: str = "<html>backup-offline-NEW</html>",
) -> Path:
    staging = tmp_path / "staging" / TITLE
    staging.mkdir(parents=True)
    (staging / "payload.bin").write_bytes(b"old-live")
    info = staging / INFO_DIR_NAME
    info.mkdir()
    (info / "cover.jpg").write_bytes(cover)
    off = info / "offline"
    off.mkdir()
    (off / "index.html").write_text(offline, encoding="utf-8")
    write_info_sidecar(
        staging,
        internal_id=frozen,
        title=TITLE,
        external_id=WID,
        workspace_id=WID,
        app_id=APP_ID,
        game_name="GameA",
        extra={"cover_path": ".info/cover.jpg"},
    )
    bind_managed_path(db, pk, staging, game_name="GameA", title=TITLE)
    assert sync_after_metadata_change(pk, staging, "repair", wait=True)
    return staging


def _service(workshop: Path, library: Path) -> ModSyncService:
    return ModSyncService(workshop, library, client=MagicMock(), archiver=MagicMock())


def _meta(source: Path) -> ModMetadata:
    return ModMetadata(
        published_file_id=WID,
        title=TITLE,
        app_id=APP_ID,
        game_name="GameA",
        source_path=str(source),
        time_updated=1,
    )


def test_first_sync_restores_backup_cover_and_offline(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen)
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop)
    library = tmp_path / "lib"
    svc = _service(workshop, library)
    hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {},
    )
    assert hint == "success"
    live_cover = managed / INFO_DIR_NAME / "cover.jpg"
    live_off = managed / INFO_DIR_NAME / "offline" / "index.html"
    bak = _bak_dir(pk, frozen)
    bak_cover = bak / "cover.jpg"
    bak_off = bak / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live_cover.is_file() and bak_cover.is_file()
    assert live_off.is_file() and bak_off.is_file()
    assert _sha256(live_cover) == _sha256(bak_cover)
    assert _sha256(live_off) == _sha256(bak_off)
    assert live_cover.read_bytes() == b"backup-cover-NEW"
    assert "backup-offline-NEW" in live_off.read_text(encoding="utf-8")
    assert (managed / "payload.bin").read_bytes() == b"workshop-payload"
    assert (managed / INFO_DIR_NAME / "metadata.json").is_file()


def test_first_sync_metadata_only_no_backup_invents_nothing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop)
    library = tmp_path / "lib"
    svc = _service(workshop, library)
    hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {},
    )
    assert hint == "success"
    info = managed / INFO_DIR_NAME
    assert (info / "metadata.json").is_file()
    assert list(info.glob("cover.*")) == []
    assert not (info / "offline" / "index.html").exists()
    assert not (info / "index.html").exists()


def test_source_old_cover_backup_new_cover_wins(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, cover=b"BACKUP-NEW-COVER")
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop, with_cover=b"SOURCE-OLD-COVER")
    library = tmp_path / "lib"
    svc = _service(workshop, library)
    _hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {},
    )
    live = managed / INFO_DIR_NAME / "cover.jpg"
    bak = _bak_dir(pk, frozen) / "cover.jpg"
    assert live.read_bytes() == b"BACKUP-NEW-COVER"
    assert bak.read_bytes() == b"BACKUP-NEW-COVER"
    assert _sha256(live) == _sha256(bak)


def test_live_new_cover_not_replaced_by_old_backup_on_skip(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    staging = _seed_backup(db, tmp_path, pk, frozen, cover=b"OLD-BACKUP")
    live = tmp_path / "lib" / "GameA" / TITLE
    live.mkdir(parents=True)
    (live / "payload.bin").write_bytes(b"live-payload")
    info = live / INFO_DIR_NAME
    info.mkdir()
    (info / "cover.jpg").write_bytes(b"NEW-LIVE-COVER")
    write_info_sidecar(
        live,
        internal_id=frozen,
        title=TITLE,
        external_id=WID,
        workspace_id=WID,
        app_id=APP_ID,
        game_name="GameA",
        extra={"cover_path": ".info/cover.jpg"},
    )
    bind_managed_path(db, pk, live, game_name="GameA", title=TITLE)
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop, with_cover=b"workshop")
    svc = _service(workshop, tmp_path / "lib")
    hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {WID: live},
    )
    assert hint == "skipped_incomplete"
    assert managed == live
    assert (live / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"NEW-LIVE-COVER"
    result = restore_cover_from_backup(live, owner_mod_id=pk)
    assert result
    assert (live / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"NEW-LIVE-COVER"
    snap = snapshot_from_mod_folder(live, owner_mod_id=pk)
    assert snap is not None
    assert (live / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"NEW-LIVE-COVER"
    del staging


def test_live_new_offline_not_replaced_by_old_backup(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, offline="<html>OLD-BACKUP-OFF</html>")
    live = tmp_path / "lib" / "GameA" / TITLE
    live.mkdir(parents=True)
    info = live / INFO_DIR_NAME / "offline"
    info.mkdir(parents=True)
    (info / "index.html").write_text("<html>NEW-LIVE-OFF</html>", encoding="utf-8")
    write_info_sidecar(
        live,
        internal_id=frozen,
        title=TITLE,
        external_id=WID,
        workspace_id=WID,
        app_id=APP_ID,
        game_name="GameA",
    )
    bind_managed_path(db, pk, live, game_name="GameA", title=TITLE)
    restored = restore_offline_from_backup(live, owner_mod_id=pk)
    assert restored
    assert (live / INFO_DIR_NAME / "offline" / "index.html").read_text(
        encoding="utf-8"
    ) == "<html>NEW-LIVE-OFF</html>"
    snapshot_from_mod_folder(live, owner_mod_id=pk)
    assert (live / INFO_DIR_NAME / "offline" / "index.html").read_text(
        encoding="utf-8"
    ) == "<html>NEW-LIVE-OFF</html>"


def test_force_overwrite_metadata_only_source_keeps_user_sidecar(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(
        db,
        tmp_path,
        pk,
        frozen,
        cover=b"USER-COVER",
        offline="<html>USER-OFFLINE</html>",
    )
    library = tmp_path / "lib"
    dest = library / "GameA" / TITLE
    dest.mkdir(parents=True)
    (dest / "payload.bin").write_bytes(b"old-payload")
    info = dest / INFO_DIR_NAME
    info.mkdir()
    (info / "cover.jpg").write_bytes(b"USER-COVER")
    off = info / "offline"
    off.mkdir()
    (off / "index.html").write_text("<html>USER-OFFLINE</html>", encoding="utf-8")
    write_info_sidecar(
        dest,
        internal_id=frozen,
        title=TITLE,
        external_id=WID,
        workspace_id=WID,
        app_id=APP_ID,
        game_name="GameA",
        extra={"cover_path": ".info/cover.jpg"},
    )
    bind_managed_path(db, pk, dest, game_name="GameA", title=TITLE)
    assert sync_after_metadata_change(pk, dest, "repair", wait=True)

    workshop = tmp_path / "ws"
    source = _write_workshop(workshop, payload=b"new-workshop-payload")
    svc = _service(workshop, library)
    svc._force_overwrite_ids.add(WID)
    hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {WID: dest},
    )
    assert hint == "success"
    assert managed == dest
    assert (dest / "payload.bin").read_bytes() == b"new-workshop-payload"
    live_cover = dest / INFO_DIR_NAME / "cover.jpg"
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    assert live_cover.read_bytes() == b"USER-COVER"
    assert bak_cover.read_bytes() == b"USER-COVER"
    assert _sha256(live_cover) == _sha256(bak_cover)
    live_off = dest / INFO_DIR_NAME / "offline" / "index.html"
    bak_off = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert "USER-OFFLINE" in live_off.read_text(encoding="utf-8")
    assert _sha256(live_off) == _sha256(bak_off)


def test_snapshot_live_missing_does_not_delete_backup(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    staging = _seed_backup(db, tmp_path, pk, frozen, cover=b"KEEP-BACKUP")
    (staging / INFO_DIR_NAME / "cover.jpg").unlink()
    snap = snapshot_from_mod_folder(staging, owner_mod_id=pk)
    assert snap is not None
    bak = _bak_dir(pk, frozen) / "cover.jpg"
    assert bak.is_file()
    assert bak.read_bytes() == b"KEEP-BACKUP"
    live = staging / INFO_DIR_NAME / "cover.jpg"
    assert live.is_file()
    assert live.read_bytes() == b"KEEP-BACKUP"


def test_hydrate_success_completes_before_return(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, cover=b"INLINE-COVER")
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop)
    svc = _service(workshop, tmp_path / "lib")
    _hint, managed = svc._copy_only(
        _meta(source),
        SyncOptions(skip_existing=True, overwrite_files=False),
        {},
    )
    # No drain_backup_queue — Live cover must already exist.
    assert (managed / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"INLINE-COVER"


def test_backup_commit_failure_fails_copy(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, cover=b"X")
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop)
    monkeypatch.setattr(
        "services.metadata_backup_sync._sync_backup_now",
        lambda *a, **k: False,
    )
    svc = _service(workshop, tmp_path / "lib")
    with pytest.raises(RuntimeError, match="sidecar hydration failed"):
        svc._copy_only(
            _meta(source),
            SyncOptions(skip_existing=True, overwrite_files=False),
            {},
        )


def test_hydrate_source_cover_kept_when_no_backup(
    db: DatabaseManager, tmp_path: Path
) -> None:
    workshop = tmp_path / "ws"
    source = _write_workshop(workshop, with_cover=b"SOURCE-COVER")
    dest = tmp_path / "lib" / "GameA" / TITLE
    ModFileManager(tmp_path / "lib").copy_mod(
        _meta(source), overwrite_existing=False, destination=dest
    )
    result = hydrate_managed_sidecar(
        dest,
        workspace_id=WID,
        app_id=APP_ID,
        backup_wins=True,
        commit_backup=False,
    )
    assert result.ok
    assert result.cover_action == ACTION_SOURCE_COPY
    assert (dest / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"SOURCE-COVER"
    assert result.offline_action == ACTION_NONE


def test_hydrate_preserve_does_not_clobber_live(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, cover=b"OLD-BAK")
    live = tmp_path / "live" / TITLE
    live.mkdir(parents=True)
    info = live / INFO_DIR_NAME
    info.mkdir()
    (info / "cover.jpg").write_bytes(b"NEW-LIVE")
    write_info_sidecar(
        live,
        internal_id=frozen,
        title=TITLE,
        external_id=WID,
        workspace_id=WID,
        app_id=APP_ID,
        game_name="GameA",
    )
    result = hydrate_managed_sidecar(
        live,
        owner_mod_id=pk,
        backup_wins=False,
        commit_backup=False,
    )
    assert result.ok
    assert result.cover_action == ACTION_PRESERVE
    assert (live / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"NEW-LIVE"
    assert ACTION_BACKUP_RESTORE != result.cover_action
