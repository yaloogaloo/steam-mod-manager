"""Backup must keep a slim offline/index.html for every live offline page."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.backup_storage_cleanup import strip_live_backup_offline_assets
from services.file_ops import INFO_DIR_NAME, persist_unified_metadata_dict
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import (
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    backup_root,
    mark_missing,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import (
    drain_backup_queue,
    rebuild_missing_metadata_backup,
    sync_after_metadata_change,
)
from services.mod_metadata_resolver import resolve_offline_page
from services.mod_presence import backup_offline_index, entity_state, ENTITY_MISS
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_index.db")
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
) -> tuple[Path, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=workshop_id,
            workshop_id=workshop_id,
            title=title,
            app_id=APP_ID,
            game_name="GameA",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = tmp_path / "mod" / "GameA" / title
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=workshop_id,
        workspace_id=workshop_id,
        app_id=APP_ID,
        game_name="GameA",
        extra={"source_url": f"https://example.test/{workshop_id}"},
    )
    bind_managed_path(db, pk, folder, game_name="GameA", title=title)
    return folder, pk


def _write_canonical_offline(folder: Path, html: str = "<html>canonical</html>") -> Path:
    offline = folder / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True, exist_ok=True)
    index = offline / "index.html"
    index.write_text(html, encoding="utf-8")
    assets = offline / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "all.css").write_text("body{color:red}", encoding="utf-8")
    (assets / "font.woff").write_bytes(b"WOFFDATA")
    return index


def _write_steam_legacy_offline(folder: Path, html: str = "<html>steam-legacy</html>") -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    index = info / "index.html"
    index.write_text(html, encoding="utf-8")
    assets = info / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "all.css").write_text("body{color:blue}", encoding="utf-8")
    return index


def _backup_index(pk: str) -> Path:
    return backup_root(pk) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX


def test_canonical_offline_first_backup_writes_index(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981001", title="CanonFirst")
    _write_canonical_offline(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    dest = _backup_index(pk)
    assert dest.is_file()
    assert "canonical" in dest.read_text(encoding="utf-8")
    assert not (backup_root(pk) / BACKUP_OFFLINE_DIR / "assets").exists()


def test_second_backup_keeps_offline_index(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981002", title="CanonSecond")
    _write_canonical_offline(folder, "<html>keep-me</html>")
    sync_after_metadata_change(pk, folder, "import", wait=True)
    first = _backup_index(pk).read_text(encoding="utf-8")
    sync_after_metadata_change(pk, folder, "rescan", wait=True)
    assert _backup_index(pk).is_file()
    assert _backup_index(pk).read_text(encoding="utf-8") == first


def test_metadata_update_does_not_drop_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981003", title="MetaKeep")
    _write_canonical_offline(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    data = json.loads((folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"))
    data["title"] = "MetaKeep-edited"
    persist_unified_metadata_dict(folder, data, sync_backup=False)
    sync_after_metadata_change(pk, folder, "edit", wait=True)
    assert _backup_index(pk).is_file()
    assert "canonical" in _backup_index(pk).read_text(encoding="utf-8")


def test_cover_update_does_not_drop_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981004", title="CoverKeep")
    _write_canonical_offline(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "cover.png").write_bytes(TINY_PNG)
    sync_after_metadata_change(pk, folder, "cover_change", wait=True)
    assert _backup_index(pk).is_file()
    assert list(backup_root(pk).glob("cover.*"))


def test_cleanup_does_not_delete_offline_index(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981005", title="CleanupKeep")
    _write_canonical_offline(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    leftover = backup_root(pk) / BACKUP_OFFLINE_DIR / "assets"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "old.css").write_text("stale", encoding="utf-8")
    stats = strip_live_backup_offline_assets(data_root / "mod_backup", [pk])
    assert stats["asset_directories"] == 1
    assert not leftover.exists()
    assert _backup_index(pk).is_file()


def test_full_offline_assets_never_enter_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981006", title="NoAssets")
    _write_canonical_offline(folder)
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    dest = backup_root(pk)
    assert (dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX).is_file()
    assert not (dest / BACKUP_OFFLINE_DIR / "assets").exists()
    assert not (dest / "assets").exists()
    rels = [p.relative_to(dest).as_posix().lower() for p in dest.rglob("*") if p.is_file()]
    assert "offline/index.html" in rels
    assert all("assets" not in rel for rel in rels)


def test_miss_reads_backup_offline_index(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981007", title="MissRead")
    _write_canonical_offline(folder, "<html>from-live</html>")
    from core.paths import asset_store_dir
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas

    assert finalize_live_offline_to_cas(
        folder, store=AssetStore(root=asset_store_dir())
    ).ok
    sync_after_metadata_change(pk, folder, "import", wait=True)
    shutil.rmtree(folder)
    mark_missing(pk)
    assert entity_state(pk, db=db) == ENTITY_MISS
    off = resolve_offline_page(pk, folder)
    assert off is not None and off.is_file()
    assert "offline_view" in str(off).replace("\\", "/")
    bak = backup_offline_index(pk)
    assert bak is not None and bak.is_file()
    assert bak.name == BACKUP_OFFLINE_INDEX
    assert BACKUP_OFFLINE_DIR in str(bak)
    assert "from-live" in off.read_text(encoding="utf-8")


def test_no_live_offline_does_not_invent_backup_page(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981008", title="NoPage")
    sync_after_metadata_change(pk, folder, "import", wait=True)
    assert not _backup_index(pk).exists()
    leftover = backup_root(pk) / BACKUP_OFFLINE_DIR
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / BACKUP_OFFLINE_INDEX).write_text("<html>stale</html>", encoding="utf-8")
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    assert snap.offline_path == ""
    assert not _backup_index(pk).exists()


def test_steam_legacy_info_index_is_copied_to_backup_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """Religion Expanded / steam_archive layout: .info/index.html, no .info/offline/."""
    folder, pk = _seed(db, tmp_path, workshop_id="1178185727", title="Religion Expanded")
    _write_steam_legacy_offline(folder)
    assert not (folder / INFO_DIR_NAME / "offline" / "index.html").exists()
    sync_after_metadata_change(pk, folder, "offline_change", wait=True)
    dest = _backup_index(pk)
    assert dest.is_file()
    assert "steam-legacy" in dest.read_text(encoding="utf-8")
    assert not (backup_root(pk) / BACKUP_OFFLINE_DIR / "assets").exists()
    assert not (backup_root(pk) / "assets").exists()
    row = db.get_mod_backup_row(pk)
    assert row is not None
    assert str(row.get("backup_offline_path") or "").endswith(
        f"{BACKUP_OFFLINE_DIR}\\{BACKUP_OFFLINE_INDEX}"
    ) or str(row.get("backup_offline_path") or "").endswith(
        f"{BACKUP_OFFLINE_DIR}/{BACKUP_OFFLINE_INDEX}"
    )


def test_metadata_update_keeps_steam_legacy_backup_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981009", title="LegacyKeep")
    _write_steam_legacy_offline(folder)
    sync_after_metadata_change(pk, folder, "offline_change", wait=True)
    data = json.loads((folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"))
    data["description"] = "edited-after-archive"
    persist_unified_metadata_dict(folder, data, sync_backup=False)
    sync_after_metadata_change(pk, folder, "edit", wait=True)
    assert _backup_index(pk).is_file()
    assert "steam-legacy" in _backup_index(pk).read_text(encoding="utf-8")


def test_rebuild_backfills_missing_steam_legacy_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981010", title="RebuildLegacy")
    _write_steam_legacy_offline(folder)
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / INFO_DIR_NAME / "metadata.json", dest / "metadata.json")
    (dest / "cover.png").write_bytes(TINY_PNG)
    assert not _backup_index(pk).exists()
    created = rebuild_missing_metadata_backup(tmp_path / "mod")
    assert created >= 1
    assert _backup_index(pk).is_file()
    assert "steam-legacy" in _backup_index(pk).read_text(encoding="utf-8")
    assert not (dest / BACKUP_OFFLINE_DIR / "assets").exists()


def test_rebuild_skips_complete_backup_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981011", title="RebuildSkip")
    _write_canonical_offline(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    created = rebuild_missing_metadata_backup(tmp_path / "mod")
    assert created == 0
    assert _backup_index(pk).is_file()
