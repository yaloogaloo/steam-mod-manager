"""Phase 5: CAS-only Backup Offline Snapshot (no durable offline/assets dual-write)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.backup_asset_migration import (
    repair_info_assets_from_backup_store,
    restore_backup_assets_from_store,
)
from services.info_asset_migration import migrate_info_assets
from services.offline.backup_closure import (
    ensure_backup_offline_openable,
    snapshot_offline_closure,
    usable_backup_offline_index,
)

TINY_A = b"phase5-cas-only-aaa"
TINY_B = b"phase5-cas-only-bbb"


def _steam_mod(folder: Path, assets: dict[str, bytes]) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    return folder


def test_snapshot_does_not_create_offline_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    store = AssetStore(root=store_root)
    assert migrate_info_assets(mod, store=store).ok
    dest = tmp_path / "backup" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert (dest / "index.html").is_file()
    assert (dest / MANIFEST_FILENAME).is_file()
    assert not (dest / "assets").exists()
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    assert len(man.assets) == 2
    for ref in man.assets:
        assert store.verify(ref.sha256)


def test_snapshot_again_no_asset_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    migrate_info_assets(mod, store=AssetStore(root=store_root))
    dest = tmp_path / "bak" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert not (dest / "assets").exists()
    # Plant a fake legacy asset to prove rerun clears it
    planted = dest / "assets" / "leak.png"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"should-be-cleared")
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert not (dest / "assets").exists()
    assert usable_backup_offline_index(dest) is not None


def test_restore_from_manifest_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert not (dest / "assets").exists()
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok and restore.written == 1
    assert sha256_file(dest / "assets" / "a.png") == sha256_bytes(TINY_A)
    # Restore for test only — production OPEN uses offline_view
    # Clear again to prove durable state stays CAS-only after cleanup intent
    from services.offline.backup_closure import clear_backup_offline_assets_dir

    clear_backup_offline_assets_dir(dest)
    assert not (dest / "assets").exists()


def test_repair_and_miss_from_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    from core.cas_runtime import reset_cas_runtime_cache

    reset_cas_runtime_cache()
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "5" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # MISS-like: wipe LIVE assets
    for p in (mod / ".info" / "assets").iterdir():
        p.unlink()
    (mod / ".info" / "assets").rmdir()
    repair = repair_info_assets_from_backup_store(mod, mod_id="5", store=store)
    assert repair.ok
    # Phase 6: Repair does not recreate durable .info/assets
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    from services.info_asset_runtime import ensure_live_offline_openable

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").is_file()
    assert sha256_file(opened.parent / "assets" / "a.png") == sha256_bytes(TINY_A)
    # Backup still has no durable assets
    assert not (dest / "assets").exists()


def test_missing_cas_repair_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "6" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    digest = sha256_bytes(TINY_A)
    store.delete(digest)
    for p in (mod / ".info" / "assets").iterdir():
        p.unlink()
    repair = repair_info_assets_from_backup_store(mod, mod_id="6", store=store)
    assert not repair.ok
    assert any("missing" in i.lower() or "missing" in repair.reason.lower() for i in (repair.issues or [repair.reason]))


def test_openable_view_does_not_write_backup_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    migrate_info_assets(mod, store=AssetStore(root=store_root))
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert not (dest / "assets").exists()
    opened = ensure_backup_offline_openable(dest)
    assert opened is not None
    assert opened.is_file()
    assert "offline_view" in str(opened)
    assert (opened.parent / "assets" / "a.png").is_file()
    # Backup durable tree still has no assets
    assert not (dest / "assets").exists()


def test_legacy_backup_without_assets_still_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old Backup with only index+manifest (assets already cleaned) restores."""
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "legacy" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert usable_backup_offline_index(dest) is not None
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok
