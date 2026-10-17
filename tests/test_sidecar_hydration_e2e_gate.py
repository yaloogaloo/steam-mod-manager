"""Final Gate: real ModSyncService.sync / cover / offline / Junction / Backup.

Temp directories only. Production services, not a parallel fake pipeline.
No drain_backup_queue, no sleep, no wait-after-the-fact remediation.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_STEAM,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.importers.image_picker import apply_cover_to_mod
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    _copy_cover,
    _copy_offline_index,
    readable_backup_root,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import BackupSyncError, sync_after_metadata_change
from services.offline.base import (
    OFFLINE_OUTCOME_FAILED,
    OFFLINE_OUTCOME_SUCCESS,
    OfflineUpdateResult,
)
from services.offline.manager import OfflineManager
from services.sidecar_hydration import hydrate_managed_sidecar
from services.steam_sync_junction import (
    evaluate_steam_sync_update_copy,
    is_junction,
)
from services.sync import ModSyncService, SyncOptions
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar

APP_ID = 262060
WID = "2683922974"
TITLE = "GateE2E Mod"
USER_COVER = b"USER-COVER-BYTES-v1"
USER_OFFLINE = "<html>USER-OFFLINE-v1</html>"
OLD_COVER = b"SOURCE-OLD-COVER"
NEW_COVER = b"BACKUP-NEW-COVER"
OLD_OFFLINE = "<html>SOURCE-OLD-OFFLINE</html>"
NEW_OFFLINE = "<html>BACKUP-NEW-OFFLINE</html>"
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
)
WINDOWS = os.name == "nt"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bak_dir(pk: str, frozen: str) -> Path:
    dest = readable_backup_root(frozen, mod_pk=pk)
    assert dest is not None
    return dest


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "e2e_gate.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _seed_entity(db: DatabaseManager, *, wid: str = WID, title: str = TITLE):
    created = create_steam_test_mod(
        db, external_id=wid, title=title, app_id=APP_ID, game_name="GameA"
    )
    return str(created.mod_id), str(created.internal_id or "")


def _write_workshop(
    root: Path,
    *,
    wid: str = WID,
    with_cover: bytes | None = None,
    with_offline: str | None = None,
    payload: bytes = b"workshop-payload",
    title: str = TITLE,
) -> Path:
    folder = root / wid
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(payload)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (info / "metadata.json").write_text(
        f'{{"published_file_id":"{wid}","workspace_id":"{wid}","title":"{title}",'
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
    wid: str = WID,
    title: str = TITLE,
    cover: bytes = USER_COVER,
    offline: str = USER_OFFLINE,
) -> Path:
    staging = tmp_path / "staging" / title
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
        title=title,
        external_id=wid,
        workspace_id=wid,
        app_id=APP_ID,
        game_name="GameA",
        extra={"cover_path": ".info/cover.jpg"},
    )
    bind_managed_path(db, pk, staging, game_name="GameA", title=title)
    assert sync_after_metadata_change(pk, staging, "repair", wait=True)
    return staging


def _write_live(
    library: Path,
    db: DatabaseManager,
    pk: str,
    frozen: str,
    *,
    wid: str = WID,
    title: str = TITLE,
    cover: bytes = USER_COVER,
    offline: str = USER_OFFLINE,
    payload: bytes = b"live-payload",
) -> Path:
    dest = library / "GameA" / title
    dest.mkdir(parents=True)
    (dest / "payload.bin").write_bytes(payload)
    info = dest / INFO_DIR_NAME
    info.mkdir()
    (info / "cover.jpg").write_bytes(cover)
    off = info / "offline"
    off.mkdir()
    (off / "index.html").write_text(offline, encoding="utf-8")
    write_info_sidecar(
        dest,
        internal_id=frozen,
        title=title,
        external_id=wid,
        workspace_id=wid,
        app_id=APP_ID,
        game_name="GameA",
        extra={"cover_path": ".info/cover.jpg", "time_updated": 1000},
    )
    bind_managed_path(db, pk, dest, game_name="GameA", title=title)
    assert sync_after_metadata_change(pk, dest, "repair", wait=True)
    return dest


def _client(*, wid: str = WID, title: str = TITLE) -> MagicMock:
    meta = ModMetadata(
        published_file_id=wid,
        title=title,
        app_id=APP_ID,
        game_name="GameA",
        time_updated=1000,
        preview_url="",
    )
    client = MagicMock()
    client.timeout = 10
    client.get_details_batch.return_value = [meta]
    client.resolve_game_names.return_value = None
    client.refresh_details.return_value = []
    return client


def _opts(**kwargs) -> SyncOptions:
    base = dict(
        skip_existing=True,
        download_covers=False,
        archive_pages=False,
        overwrite_files=False,
        recursive_scan=False,
        io_workers=1,
        net_workers=1,
    )
    base.update(kwargs)
    return SyncOptions(**base)


def _sync(workshop: Path, library: Path, **opt_kw):
    svc = ModSyncService(
        workshop, library, client=_client(), archiver=MagicMock()
    )
    return svc.sync(_opts(**opt_kw)), svc


def _assert_ok(result) -> Path:
    assert not result.failed, result.failed
    assert not result.registration_failed, result.registration_failed
    landed = result.success or result.updated
    assert landed, "sync returned no success/updated mods"
    managed = Path(str(landed[0].managed_path or ""))
    assert managed.is_dir()
    return managed


def _assert_failed(result) -> None:
    assert result.failed, "expected Sync != SUCCESS"
    assert not result.success
    assert not result.updated


def _file_set(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix().lower()] = _sha256(path)
    return out


def _inject_backup_copy2(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.metadata_backup as mb
    import services.offline.backup_closure as bc

    orig_mb = mb.shutil.copy2
    orig_bc = bc.shutil.copy2

    def _fail(src, dst, *a, **k):
        text = str(dst).replace("\\", "/")
        if "/mod_backup/" in text:
            raise OSError("injected backup copy failure")
        return orig_mb(src, dst, *a, **k)

    def _fail_bc(src, dst, *a, **k):
        text = str(dst).replace("\\", "/")
        if "/mod_backup/" in text:
            raise OSError("injected backup copy failure")
        return orig_bc(src, dst, *a, **k)

    monkeypatch.setattr(mb.shutil, "copy2", _fail)
    monkeypatch.setattr(bc.shutil, "copy2", _fail_bc)


def _inject_backup_mkdir(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = Path.mkdir

    def _fail(self, *args, **kwargs):
        text = str(self).replace("\\", "/")
        if "/mod_backup/" in text or text.endswith("/mod_backup"):
            raise OSError("injected backup mkdir failure")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _fail)


def _inject_backup_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    import os as os_mod

    import services.asset_manifest as am

    orig = os_mod.replace

    def _fail(src, dst, *a, **k):
        text = str(dst).replace("\\", "/")
        if "/mod_backup/" in text:
            raise OSError("injected backup replace failure")
        return orig(src, dst, *a, **k)

    monkeypatch.setattr(am.os, "replace", _fail)


def _spy_rmtree(monkeypatch: pytest.MonkeyPatch, dest: Path) -> list[Path]:
    import services.file_ops as fo

    orig = fo.shutil.rmtree
    hits: list[Path] = []

    def _spy(path, *a, **k):
        candidate = Path(path)
        try:
            if candidate.resolve() == dest.resolve():
                hits.append(candidate)
        except OSError:
            if candidate == dest:
                hits.append(candidate)
        return orig(path, *a, **k)

    monkeypatch.setattr(fo.shutil, "rmtree", _spy)
    return hits


def _make_junction(link: Path, target: Path) -> None:
    if not WINDOWS:
        pytest.skip("Junctions require Windows mklink /J")
    link.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not is_junction(link):
        pytest.skip(
            "mklink /J unavailable: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )


def _trace_copy_hydrate(monkeypatch: pytest.MonkeyPatch) -> dict:
    chain = {
        "copy_mod": 0,
        "hydrate": 0,
        "overwrite": [],
        "backup_committed": [],
    }
    orig_copy = ModFileManager.copy_mod

    def _copy(self, metadata, *, overwrite_existing=False, destination=None, ignore_files=None):
        chain["copy_mod"] += 1
        chain["overwrite"].append(bool(overwrite_existing))
        return orig_copy(
            self,
            metadata,
            overwrite_existing=overwrite_existing,
            destination=destination,
            ignore_files=ignore_files,
        )

    orig_h = hydrate_managed_sidecar

    def _h(*args, **kwargs):
        chain["hydrate"] += 1
        result = orig_h(*args, **kwargs)
        chain["backup_committed"].append(bool(result.backup_committed))
        return result

    monkeypatch.setattr(ModFileManager, "copy_mod", _copy)
    monkeypatch.setattr(
        "services.sidecar_hydration.hydrate_managed_sidecar", _h
    )
    return chain


class _FakeOffline:
    def __init__(self, html: str) -> None:
        self.html = html

    def can_handle(self, mod: object) -> bool:
        return True

    def get_provider_name(self) -> str:
        return PROVIDER_STEAM_ARCHIVE

    def update_offline_page(self, mod_id, *, managed_path=None, **kwargs):
        path = Path(managed_path)
        off = path / INFO_DIR_NAME / "offline"
        off.mkdir(parents=True, exist_ok=True)
        index = off / "index.html"
        index.write_text(self.html, encoding="utf-8")
        return OfflineUpdateResult(
            mod_id=str(mod_id),
            index_path=index,
            status=OFFLINE_STATUS_ARCHIVED,
            provider=PROVIDER_STEAM_ARCHIVE,
            outcome=OFFLINE_OUTCOME_SUCCESS,
            write_performed=True,
        )


# ---------------------------------------------------------------------------
# Case A — first Steam Sync through ModSyncService.sync
# ---------------------------------------------------------------------------


def test_case_a_first_steam_sync_real_path(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen)
    workshop = tmp_path / "ws"
    _write_workshop(workshop)
    library = tmp_path / "lib"
    chain = _trace_copy_hydrate(monkeypatch)
    result, _svc = _sync(workshop, library)
    managed = _assert_ok(result)
    assert chain["copy_mod"] >= 1
    assert False in chain["overwrite"]
    assert chain["hydrate"] >= 1

    live_meta = managed / INFO_DIR_NAME / "metadata.json"
    live_cover = managed / INFO_DIR_NAME / "cover.jpg"
    live_off = managed / INFO_DIR_NAME / "offline" / "index.html"
    bak = _bak_dir(pk, frozen)
    bak_cover = bak / "cover.jpg"
    bak_off = bak / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live_meta.is_file()
    assert live_cover.is_file() and bak_cover.is_file()
    assert live_off.is_file() and bak_off.is_file()
    assert live_cover.read_bytes() == USER_COVER
    assert bak_cover.read_bytes() == USER_COVER
    assert "USER-OFFLINE-v1" in live_off.read_text(encoding="utf-8")
    assert "USER-OFFLINE-v1" in bak_off.read_text(encoding="utf-8")
    assert _sha256(live_cover) == _sha256(bak_cover)
    assert _sha256(live_off) == _sha256(bak_off)
    assert (managed / "payload.bin").read_bytes() == b"workshop-payload"


# ---------------------------------------------------------------------------
# Case B — first materialize, no Backup
# ---------------------------------------------------------------------------


def test_case_b_first_sync_no_backup_invents_nothing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    workshop = tmp_path / "ws"
    _write_workshop(workshop)
    library = tmp_path / "lib"
    result, _svc = _sync(workshop, library)
    managed = _assert_ok(result)
    info = managed / INFO_DIR_NAME
    assert (info / "metadata.json").is_file()
    assert list(info.glob("cover.*")) == []
    assert not (info / "offline" / "index.html").exists()
    assert not (info / "index.html").exists()


# ---------------------------------------------------------------------------
# Case C — Backup NEW beats Workshop OLD
# ---------------------------------------------------------------------------


def test_case_c_backup_new_beats_source_old(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen, cover=NEW_COVER, offline=NEW_OFFLINE)
    workshop = tmp_path / "ws"
    source = _write_workshop(
        workshop, with_cover=OLD_COVER, with_offline=OLD_OFFLINE
    )
    library = tmp_path / "lib"
    result, _svc = _sync(workshop, library)
    managed = _assert_ok(result)
    live_cover = managed / INFO_DIR_NAME / "cover.jpg"
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    live_off = managed / INFO_DIR_NAME / "offline" / "index.html"
    bak_off = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live_cover.read_bytes() == NEW_COVER
    assert bak_cover.read_bytes() == NEW_COVER
    assert "BACKUP-NEW-OFFLINE" in live_off.read_text(encoding="utf-8")
    assert "BACKUP-NEW-OFFLINE" in bak_off.read_text(encoding="utf-8")
    assert (source / INFO_DIR_NAME / "cover.jpg").read_bytes() == OLD_COVER
    assert OLD_COVER not in (live_cover.read_bytes(), bak_cover.read_bytes())
    assert "SOURCE-OLD-OFFLINE" not in live_off.read_text(encoding="utf-8")
    assert "SOURCE-OLD-OFFLINE" not in bak_off.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Case D — protect failure must not rmtree(dest)
# ---------------------------------------------------------------------------


def test_case_d_protect_copy_failure_blocks_rmtree(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(
        library, db, pk, frozen, payload=b"LIVE-MUST-SURVIVE"
    )
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    assert bak_cover.is_file()
    bak_cover.unlink()
    workshop = tmp_path / "ws"
    _write_workshop(workshop, payload=b"workshop-new-payload")
    hits = _spy_rmtree(monkeypatch, dest)
    _inject_backup_copy2(monkeypatch)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    _assert_failed(result)
    assert hits == []
    assert dest.is_dir()
    assert (dest / "payload.bin").read_bytes() == b"LIVE-MUST-SURVIVE"
    assert (dest / INFO_DIR_NAME / "cover.jpg").read_bytes() == USER_COVER
    assert (dest / INFO_DIR_NAME / "offline" / "index.html").is_file()


def test_case_d_protect_mkdir_failure_blocks_rmtree(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen, payload=b"LIVE-MKDIR-SURVIVE")
    workshop = tmp_path / "ws"
    _write_workshop(workshop, payload=b"workshop-new-payload")
    hits = _spy_rmtree(monkeypatch, dest)
    _inject_backup_mkdir(monkeypatch)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    _assert_failed(result)
    assert hits == []
    assert dest.is_dir()
    assert (dest / "payload.bin").read_bytes() == b"LIVE-MKDIR-SURVIVE"


def test_case_d_protect_replace_failure_blocks_rmtree(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen, payload=b"LIVE-REPLACE-SURVIVE")
    workshop = tmp_path / "ws"
    _write_workshop(workshop, payload=b"workshop-new-payload")
    hits = _spy_rmtree(monkeypatch, dest)
    _inject_backup_replace(monkeypatch)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    _assert_failed(result)
    assert hits == []
    assert dest.is_dir()
    assert (dest / "payload.bin").read_bytes() == b"LIVE-REPLACE-SURVIVE"


# ---------------------------------------------------------------------------
# Case E — force overwrite restores user sidecar
# ---------------------------------------------------------------------------


def test_case_e_force_overwrite_restores_user_sidecar(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen, payload=b"old-payload")
    workshop = tmp_path / "ws"
    _write_workshop(workshop, payload=b"new-workshop-payload")
    hits = _spy_rmtree(monkeypatch, dest)
    chain = _trace_copy_hydrate(monkeypatch)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    managed = _assert_ok(result)
    assert managed == dest
    assert hits, "expected real rmtree(dest) after successful protect"
    assert True in chain["overwrite"]
    assert chain["hydrate"] >= 1
    assert (dest / "payload.bin").read_bytes() == b"new-workshop-payload"
    live_cover = dest / INFO_DIR_NAME / "cover.jpg"
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    live_off = dest / INFO_DIR_NAME / "offline" / "index.html"
    bak_off = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live_cover.read_bytes() == USER_COVER
    assert bak_cover.read_bytes() == USER_COVER
    assert _sha256(live_cover) == _sha256(bak_cover)
    assert "USER-OFFLINE-v1" in live_off.read_text(encoding="utf-8")
    assert _sha256(live_off) == _sha256(bak_off)


# ---------------------------------------------------------------------------
# Case F — Junction skip
# ---------------------------------------------------------------------------


def test_case_f_junction_skip_does_not_clobber_live_new(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(
        library, db, pk, frozen, cover=b"LIVE-NEW-COVER", payload=b"junction-live"
    )
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    bak_cover.write_bytes(b"BACKUP-OLD-COVER")
    workshop = tmp_path / "ws"
    link = workshop / WID
    _make_junction(link, dest)
    decision = evaluate_steam_sync_update_copy(
        source=link, destination=dest, workspace_id=WID
    )
    assert dest.is_dir()
    assert decision.skip_physical_copy is True
    hits = _spy_rmtree(monkeypatch, dest)
    chain = _trace_copy_hydrate(monkeypatch)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    managed = _assert_ok(result)
    assert managed == dest
    assert hits == []
    assert chain["copy_mod"] == 0
    live_cover = dest / INFO_DIR_NAME / "cover.jpg"
    assert live_cover.read_bytes() == b"LIVE-NEW-COVER"
    assert live_cover.read_bytes() != b"BACKUP-OLD-COVER"


def test_case_f_junction_skip_restores_missing_live(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen, cover=USER_COVER)
    (dest / INFO_DIR_NAME / "cover.jpg").unlink()
    (dest / INFO_DIR_NAME / "offline" / "index.html").unlink()
    workshop = tmp_path / "ws"
    _make_junction(workshop / WID, dest)
    result, _svc = _sync(workshop, library, overwrite_files=True)
    managed = _assert_ok(result)
    assert managed == dest
    live_cover = dest / INFO_DIR_NAME / "cover.jpg"
    bak_cover = _bak_dir(pk, frozen) / "cover.jpg"
    live_off = dest / INFO_DIR_NAME / "offline" / "index.html"
    bak_off = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    assert live_cover.is_file()
    assert live_cover.read_bytes() == USER_COVER
    assert _sha256(live_cover) == _sha256(bak_cover)
    assert live_off.is_file()
    assert _sha256(live_off) == _sha256(bak_off)


# ---------------------------------------------------------------------------
# Case G — snapshot / reconcile, Live missing preserves + restores Backup
# ---------------------------------------------------------------------------


def test_case_g_snapshot_reconcile_live_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen)
    bak = _bak_dir(pk, frozen)
    bak_cover = bak / "cover.jpg"
    bak_off = bak / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    cover_hash = _sha256(bak_cover)
    off_hash = _sha256(bak_off)
    (dest / INFO_DIR_NAME / "cover.jpg").unlink()
    (dest / INFO_DIR_NAME / "offline" / "index.html").unlink()
    snap = snapshot_from_mod_folder(dest, owner_mod_id=pk)
    assert snap is not None
    from services.library_reconcile import reconcile_library
    from services.metadata_backup import reconcile_library_presence

    reconcile_library_presence(library)
    reconcile_library(library)
    assert bak_cover.is_file()
    assert bak_off.is_file()
    assert _sha256(bak_cover) == cover_hash
    assert _sha256(bak_off) == off_hash
    live_cover = dest / INFO_DIR_NAME / "cover.jpg"
    live_off = dest / INFO_DIR_NAME / "offline" / "index.html"
    assert live_cover.is_file()
    assert live_off.is_file()
    assert _sha256(live_cover) == cover_hash
    assert _sha256(live_off) == off_hash
    src = inspect.getsource(_copy_cover)
    assert "src is None" in src
    assert "_clear_backup_covers" not in src
    src_off = inspect.getsource(_copy_offline_index)
    assert "_clear_backup_offline" not in src_off


# ---------------------------------------------------------------------------
# Case H — real cover-change service path (same as UI after QFileDialog)
# ---------------------------------------------------------------------------


def test_case_h_real_cover_change_path(db: DatabaseManager, tmp_path: Path) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen)
    chosen = tmp_path / "user-new.png"
    chosen.write_bytes(TINY_PNG + b"USER-NEW-COVER")
    rel = apply_cover_to_mod(dest, chosen, mod_id=pk, update_db=True)
    assert rel
    live = dest / INFO_DIR_NAME / "cover.png"
    bak = next(_bak_dir(pk, frozen).glob("cover.*"))
    assert live.is_file() and bak.is_file()
    assert _sha256(live) == _sha256(bak)
    assert live.read_bytes() == TINY_PNG + b"USER-NEW-COVER"


# ---------------------------------------------------------------------------
# Case I — real OfflineManager.update_mod_offline path
# ---------------------------------------------------------------------------


def test_case_i_real_offline_save_path(db: DatabaseManager, tmp_path: Path) -> None:
    pk, frozen = _seed_entity(db)
    library = tmp_path / "lib"
    dest = _write_live(library, db, pk, frozen)
    html = "<html>user-saved-offline-unique</html>"
    manager = OfflineManager(
        library_root=library, providers=[_FakeOffline(html)]
    )
    result = manager.update_mod_offline(
        pk, managed_path=dest, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_SUCCESS
    live_dir = dest / INFO_DIR_NAME / "offline"
    bak_dir = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR
    live_index = live_dir / "index.html"
    bak_index = bak_dir / BACKUP_OFFLINE_INDEX
    assert live_index.is_file() and bak_index.is_file()
    assert _sha256(live_index) == _sha256(bak_index)
    live_files = _file_set(live_dir)
    bak_files = _file_set(bak_dir)
    assert "index.html" in live_files and "index.html" in bak_files
    assert live_files["index.html"] == bak_files["index.html"]
    for name, digest in live_files.items():
        if name == "manifest.json":
            continue
        assert name in bak_files
        assert bak_files[name] == digest


# ---------------------------------------------------------------------------
# Production-path Backup failure propagation
# ---------------------------------------------------------------------------


def test_cover_backup_copy_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    src = tmp_path / "fail.png"
    src.write_bytes(TINY_PNG + b"FAIL")
    _inject_backup_copy2(monkeypatch)
    with pytest.raises(BackupSyncError):
        apply_cover_to_mod(dest, src, mod_id=pk, update_db=True)


def test_cover_backup_mkdir_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    src = tmp_path / "fail.png"
    src.write_bytes(TINY_PNG + b"FAIL")
    _inject_backup_mkdir(monkeypatch)
    with pytest.raises(BackupSyncError):
        apply_cover_to_mod(dest, src, mod_id=pk, update_db=True)


def test_cover_backup_replace_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    src = tmp_path / "fail.png"
    src.write_bytes(TINY_PNG + b"FAIL-REPLACE")
    _inject_backup_copy2(monkeypatch)
    with pytest.raises(BackupSyncError):
        apply_cover_to_mod(dest, src, mod_id=pk, update_db=True)


def test_offline_backup_copy_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    bak_off = _bak_dir(pk, frozen) / BACKUP_OFFLINE_DIR
    if bak_off.is_dir():
        import shutil

        shutil.rmtree(bak_off)
    _inject_backup_copy2(monkeypatch)
    manager = OfflineManager(
        library_root=tmp_path / "lib",
        providers=[_FakeOffline("<html>live-ok</html>")],
    )
    result = manager.update_mod_offline(
        pk, managed_path=dest, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_FAILED
    assert result.error


def test_offline_backup_mkdir_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    _inject_backup_mkdir(monkeypatch)
    manager = OfflineManager(
        library_root=tmp_path / "lib",
        providers=[_FakeOffline("<html>live-ok</html>")],
    )
    result = manager.update_mod_offline(
        pk, managed_path=dest, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_FAILED


def test_offline_backup_replace_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    dest = _write_live(tmp_path / "lib", db, pk, frozen)
    _inject_backup_replace(monkeypatch)
    manager = OfflineManager(
        library_root=tmp_path / "lib",
        providers=[_FakeOffline("<html>live-ok-replace</html>")],
    )
    result = manager.update_mod_offline(
        pk, managed_path=dest, platform=PLATFORM_STEAM, force_refresh=True
    )
    assert result.outcome == OFFLINE_OUTCOME_FAILED


def test_steam_sync_backup_copy_failure_is_not_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen = _seed_entity(db)
    _seed_backup(db, tmp_path, pk, frozen)
    workshop = tmp_path / "ws"
    _write_workshop(workshop)
    monkeypatch.setattr(
        "services.metadata_backup._same_file_content", lambda *a, **k: False
    )
    _inject_backup_copy2(monkeypatch)
    result, _svc = _sync(workshop, tmp_path / "lib")
    _assert_failed(result)


# ---------------------------------------------------------------------------
# Static order + destructive-delete scan
# ---------------------------------------------------------------------------


def test_static_force_overwrite_protect_before_rmtree() -> None:
    copy_only = inspect.getsource(ModSyncService._copy_only)
    protect_at = copy_only.index("protect_live_sidecar_before_overwrite")
    copy_true_at = copy_only.index("overwrite_existing=True")
    assert protect_at < copy_true_at
    raise_at = copy_only.index("sidecar backup protect failed")
    assert protect_at < raise_at < copy_true_at
    copy_mod = inspect.getsource(ModFileManager.copy_mod)
    exists_at = copy_mod.index("if dest.exists():")
    reuse_at = copy_mod.index("if not overwrite_existing:")
    rmtree_at = copy_mod.index("shutil.rmtree(dest)")
    assert exists_at < reuse_at < rmtree_at
    assert "if not overwrite_existing:" in copy_mod.split("shutil.rmtree(dest)")[0]


def test_static_backup_delete_only_explicit_apis() -> None:
    hits_clear: list[str] = []
    hits_src_none_clear: list[str] = []
    for path in (REPO_ROOT / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        if "_clear_backup_covers(" in text or "_clear_backup_offline(" in text:
            hits_clear.append(rel)
        if "src is None" in text and (
            "_clear_backup_covers(" in text or "_clear_backup_offline(" in text
        ):
            # Same-file proximity is checked below for metadata_backup.
            hits_src_none_clear.append(rel)
    assert hits_clear == ["services/metadata_backup.py"]
    src = (REPO_ROOT / "services" / "metadata_backup.py").read_text(encoding="utf-8")
    assert src.count("def _clear_backup_covers(") == 1
    assert src.count("def _clear_backup_offline(") == 1
    assert src.count("_clear_backup_covers(") == 2
    assert src.count("_clear_backup_offline(") == 2
    assert "def delete_backup_cover" in src
    assert "def delete_backup_offline" in src
    cover_fn = inspect.getsource(_copy_cover)
    assert "if src is None or not src.is_file():" in cover_fn
    assert "_clear_backup" not in cover_fn
    snap = inspect.getsource(snapshot_from_mod_folder)
    assert "_clear_backup_covers" not in snap
    assert "_clear_backup_offline" not in snap
    assert not hits_src_none_clear or hits_src_none_clear == [
        "services/metadata_backup.py"
    ]
    assert "_clear_backup_covers(dest)" in src.split("def delete_backup_cover")[1]


def test_static_no_live_missing_deletes_backup() -> None:
    snap = inspect.getsource(snapshot_from_mod_folder)
    assert "Live asset missing" in snap or "missing" in snap.lower()
    assert "_clear_backup_covers" not in snap
    restore = inspect.getsource(hydrate_managed_sidecar)
    assert "Never deletes Backup" in inspect.getdoc(hydrate_managed_sidecar) or (
        "_clear_backup" not in restore
    )
    assert "_clear_backup" not in restore
