"""Backup protection contract: user writes wait; Live missing never deletes Backup."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_STEAM,
    PROVIDER_STEAM_ARCHIVE,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.image_picker import apply_cover_to_mod
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    delete_backup_cover,
    delete_backup_offline,
    readable_backup_root,
    restore_cover_from_backup,
    restore_offline_from_backup,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import (
    BackupSyncError,
    drain_backup_queue,
    sync_after_metadata_change,
)
from services.offline.base import OFFLINE_OUTCOME_FAILED, OFFLINE_OUTCOME_SUCCESS, OfflineUpdateResult
from services.offline.manager import OfflineManager
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar

APP_ID = 4242
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bak_dir(pk: str, frozen: str) -> Path:
    dest = readable_backup_root(frozen, mod_pk=pk)
    assert dest is not None
    return dest


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "protect.db")
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


def _seed(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    workshop_id: str,
    title: str,
    with_cover: bool = False,
    with_offline: bool = False,
    cover_bytes: bytes = b"cover-v1",
    offline_html: str = "<html>offline-v1</html>",
) -> tuple[Path, str]:
    created = create_steam_test_mod(
        db, external_id=workshop_id, title=title, app_id=APP_ID, game_name="GameA"
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = tmp_path / "mod" / "GameA" / title
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    extra: dict = {"source_url": f"https://example.test/{workshop_id}"}
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    if with_cover:
        (info / "cover.jpg").write_bytes(cover_bytes)
        extra["cover_path"] = ".info/cover.jpg"
    if with_offline:
        offline = info / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        (offline / "index.html").write_text(offline_html, encoding="utf-8")
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=workshop_id,
        workspace_id=workshop_id,
        app_id=APP_ID,
        game_name="GameA",
        extra=extra,
    )
    bind_managed_path(db, pk, folder, game_name="GameA", title=title)
    return folder, pk, frozen


def test_cover_change_updates_backup_before_return(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(db, tmp_path, workshop_id="991001", title="CoverNow")
    src = tmp_path / "new.png"
    src.write_bytes(TINY_PNG + b"NEWCOVER")
    rel = apply_cover_to_mod(folder, src, mod_id=pk, update_db=True)
    assert rel
    live = folder / INFO_DIR_NAME / "cover.png"
    bak = next(_bak_dir(pk, frozen).glob("cover.*"))
    assert live.is_file()
    assert bak.is_file()
    assert _sha256(live) == _sha256(bak)
    assert _sha256(live) == hashlib.sha256(TINY_PNG + b"NEWCOVER").hexdigest()


def test_offline_change_updates_backup_before_return(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(db, tmp_path, workshop_id="991002", title="OffNow")
    html = "<html>saved-now-unique</html>"

    class _Fake:
        def can_handle(self, mod: object) -> bool:
            return True

        def get_provider_name(self) -> str:
            return PROVIDER_STEAM_ARCHIVE

        def update_offline_page(self, mod_id, *, managed_path=None, **kwargs):
            path = Path(managed_path)
            info = path / INFO_DIR_NAME
            info.mkdir(parents=True, exist_ok=True)
            index = info / "index.html"
            index.write_text(html, encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=index,
                status=OFFLINE_STATUS_ARCHIVED,
                provider=PROVIDER_STEAM_ARCHIVE,
                outcome=OFFLINE_OUTCOME_SUCCESS,
                write_performed=True,
            )

    manager = OfflineManager(library_root=tmp_path / "mod", providers=[_Fake()])
    result = manager.update_mod_offline(
        pk, managed_path=folder, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_SUCCESS
    live = folder / INFO_DIR_NAME / "index.html"
    bak = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live.is_file()
    assert bak.is_file()
    assert _sha256(live) == _sha256(bak)
    assert live.read_text(encoding="utf-8") == html


def test_missing_live_cover_preserves_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db, tmp_path, workshop_id="991003", title="KeepCover", with_cover=True
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    original = (_bak_dir(pk, frozen) / "cover.jpg").read_bytes()
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    bak = _bak_dir(pk, frozen) / "cover.jpg"
    assert bak.is_file()
    assert bak.read_bytes() == original


def test_missing_live_cover_restores_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db,
        tmp_path,
        workshop_id="991004",
        title="RestoreCover",
        with_cover=True,
        cover_bytes=b"restore-cover-bytes",
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()
    restored = restore_cover_from_backup(folder, owner_mod_id=pk)
    assert restored
    live = folder / INFO_DIR_NAME / "cover.jpg"
    bak = _bak_dir(pk, frozen) / "cover.jpg"
    assert live.is_file() and bak.is_file()
    assert _sha256(live) == _sha256(bak)
    assert live.read_bytes() == b"restore-cover-bytes"


def test_missing_live_offline_preserves_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db, tmp_path, workshop_id="991005", title="KeepOff", with_offline=True
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    original = (_bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX).read_text(
        encoding="utf-8"
    )
    (folder / INFO_DIR_NAME / "offline" / "index.html").unlink()
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    bak = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == original


def test_missing_live_offline_restores_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db,
        tmp_path,
        workshop_id="991006",
        title="RestoreOff",
        with_offline=True,
        offline_html="<html>restore-off</html>",
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "offline" / "index.html").unlink()
    restored = restore_offline_from_backup(folder, owner_mod_id=pk)
    assert restored
    live = folder / INFO_DIR_NAME / "offline" / "index.html"
    bak = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live.is_file() and bak.is_file()
    assert _sha256(live) == _sha256(bak)
    assert "restore-off" in live.read_text(encoding="utf-8")


def test_cover_backup_failure_fails_user_operation(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk, frozen = _seed(db, tmp_path, workshop_id="991007", title="CoverFail")
    src = tmp_path / "fail.png"
    src.write_bytes(TINY_PNG)

    def _boom(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "services.metadata_backup.snapshot_from_mod_folder", _boom
    )
    with pytest.raises(BackupSyncError):
        apply_cover_to_mod(folder, src, mod_id=pk, update_db=True)


def test_offline_backup_failure_fails_user_operation(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk, frozen = _seed(db, tmp_path, workshop_id="991008", title="OffFail")

    class _Fake:
        def can_handle(self, mod: object) -> bool:
            return True

        def get_provider_name(self) -> str:
            return PROVIDER_STEAM_ARCHIVE

        def update_offline_page(self, mod_id, *, managed_path=None, **kwargs):
            path = Path(managed_path)
            index = path / INFO_DIR_NAME / "index.html"
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text("<html>live-ok</html>", encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=index,
                status=OFFLINE_STATUS_ARCHIVED,
                provider=PROVIDER_STEAM_ARCHIVE,
                outcome=OFFLINE_OUTCOME_SUCCESS,
                write_performed=True,
            )

    monkeypatch.setattr(
        "services.metadata_backup.snapshot_from_mod_folder", lambda *a, **k: None
    )
    manager = OfflineManager(library_root=tmp_path / "mod", providers=[_Fake()])
    result = manager.update_mod_offline(
        pk, managed_path=folder, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_FAILED
    assert result.error


def test_snapshot_does_not_delete_backup_cover(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db, tmp_path, workshop_id="991009", title="SnapKeep", with_cover=True
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()
    snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert list(_bak_dir(pk, frozen).glob(f"{BACKUP_COVER_BASENAME}.*"))


def test_explicit_delete_backup_cover_and_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db,
        tmp_path,
        workshop_id="991010",
        title="ExplicitDel",
        with_cover=True,
        with_offline=True,
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()
    (folder / INFO_DIR_NAME / "offline" / "index.html").unlink()
    snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert list(_bak_dir(pk, frozen).glob("cover.*"))
    assert (_bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX).is_file()
    assert delete_backup_cover(pk)
    assert list(_bak_dir(pk, frozen).glob("cover.*")) == []
    assert delete_backup_offline(pk)
    assert not (_bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX).exists()


def test_live_cover_is_not_overwritten_by_old_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk, frozen = _seed(
        db,
        tmp_path,
        workshop_id="991011",
        title="NoClobber",
        with_cover=True,
        cover_bytes=b"live-newer",
    )
    sync_after_metadata_change(pk, folder, "import", wait=True)
    bak = next(_bak_dir(pk, frozen).glob("cover.*"))
    bak.write_bytes(b"backup-older")
    restored = restore_cover_from_backup(folder, owner_mod_id=pk)
    assert restored
    assert (folder / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"live-newer"
