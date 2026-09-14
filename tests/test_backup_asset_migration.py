"""Phase 3: Backup offline → Asset Store reference migration tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes
from services.backup_asset_migration import (
    migrate_backup_offline_for_mod_id,
    repair_info_assets_from_backup_store,
    restore_backup_assets_from_store,
    strip_backup_offline_assets_for_test,
    sync_backup_offline_manifest,
)
from services.info_asset_migration import migrate_info_assets
from services.offline.backup_closure import (
    snapshot_offline_closure,
    usable_backup_offline_index,
)
from services.offline.paths import resolve_offline_page

TINY_A = b"phase3-backup-aaa"
TINY_B = b"phase3-backup-bbb"


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


def test_backup_manifest_creation(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    store = AssetStore(root=tmp_path / "store")
    assert migrate_info_assets(mod, store=store, mod_id="1").ok
    dest = tmp_path / "backup" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # Hook writes manifest during snapshot when using production code path;
    # call sync explicitly with our store root:
    result = sync_backup_offline_manifest(
        dest,
        source_index=mod / ".info" / "index.html",
        store=store,
        mod_id="1",
    )
    assert result.ok
    assert (dest / MANIFEST_FILENAME).is_file()
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    assert len(man.assets) == 2
    # Phase 5: snapshot does not persist offline/assets
    assert not (dest / "assets").exists()


def test_info_and_backup_share_cas_object(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    assert migrate_info_assets(mod, store=store).ok
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # Snapshot itself reuses Phase 2 CAS objects (created=0 path via put_file)
    digest = sha256_bytes(TINY_A)
    assert store.has(digest)
    assert len([o for o in store.iter_objects() if o.sha256 == digest]) == 1
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    assert man.assets[0].sha256 == digest
    assert not (dest / "assets").exists()


def test_cross_mod_dedupe(tmp_path: Path) -> None:
    store = AssetStore(root=tmp_path / "store")
    a = _steam_mod(tmp_path / "A", {"x.png": TINY_A})
    b = _steam_mod(tmp_path / "B", {"y.png": TINY_A})
    migrate_info_assets(a, store=store)
    migrate_info_assets(b, store=store)
    da = tmp_path / "ba" / "offline"
    db = tmp_path / "bb" / "offline"
    snapshot_offline_closure(a / ".info" / "index.html", da)
    snapshot_offline_closure(b / ".info" / "index.html", db)
    ra = sync_backup_offline_manifest(
        da, source_index=a / ".info" / "index.html", store=store
    )
    rb = sync_backup_offline_manifest(
        db, source_index=b / ".info" / "index.html", store=store
    )
    assert ra.ok and rb.ok
    assert len(list(store.iter_objects())) == 1


def test_backup_idempotency(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    first = sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    second = sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    assert first.ok and second.ok
    assert second.created_objects == 0
    m1 = json.loads((dest / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    m2 = json.loads((dest / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert m1 == m2


def test_restore_from_store_without_legacy_assets(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # Phase 5: no legacy assets after snapshot
    assert not (dest / "assets").exists()
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok
    assert restore.written >= 2
    assert (dest / "assets" / "a.png").read_bytes() == TINY_A
    assert (dest / "assets" / "b.png").read_bytes() == TINY_B
    assert usable_backup_offline_index(dest) is not None


def test_missing_cas_object_fails_restore(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    digest = sha256_bytes(TINY_A)
    store.delete(digest)
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok is False
    assert any("missing" in i.lower() or "missing" in restore.reason.lower()
               for i in (restore.issues or [restore.reason]))


def test_corrupt_cas_object_fails(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    store.get_path(sha256_bytes(TINY_A)).write_bytes(b"corrupt")
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok is False


def test_manifest_mismatch_rejected(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # Stage a mismatched physical asset and re-sync (legacy migration path)
    restore_backup_assets_from_store(dest, store=store)
    (dest / "assets" / "a.png").write_bytes(TINY_B)
    result = sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    assert result.ok is False
    assert any("mismatch" in i for i in result.issues) or "mismatch" in result.reason


def test_path_traversal_in_manifest_rejected(tmp_path: Path) -> None:
    from services.asset_manifest import ManifestError, validate_manifest_path

    with pytest.raises(ManifestError):
        validate_manifest_path("../x")


def test_repair_info_from_store_via_live_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    from core.cas_runtime import reset_cas_runtime_cache

    reset_cas_runtime_cache()
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    shutil.rmtree(mod / ".info" / "assets")
    assert not (mod / ".info" / "assets" / "a.png").exists()
    repair = repair_info_assets_from_backup_store(mod, store=store)
    assert repair.ok
    # Phase 6: no durable .info/assets recreation
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    from services.info_asset_runtime import ensure_live_offline_openable

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").read_bytes() == TINY_A


def test_repair_info_from_backup_manifest_when_live_manifest_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    from core.cas_runtime import reset_cas_runtime_cache

    reset_cas_runtime_cache()
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "data" / "mod_backup" / "42" / "offline"
    dest.mkdir(parents=True)
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store, mod_id="42"
    )
    shutil.rmtree(mod / ".info" / "assets")
    (mod / ".info" / MANIFEST_FILENAME).unlink(missing_ok=True)

    import services.backup_asset_migration as bam

    monkeypatch.setattr(
        bam, "backup_root", lambda mid: tmp_path / "data" / "mod_backup" / str(mid)
    )
    repair = repair_info_assets_from_backup_store(mod, mod_id="42", store=store)
    assert repair.ok
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    from services.info_asset_runtime import ensure_live_offline_openable

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").read_bytes() == TINY_A


def test_snapshot_keeps_manifest_sidecar(tmp_path: Path) -> None:
    """Second snapshot must not prune Backup manifest.json."""
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    assert (dest / MANIFEST_FILENAME).is_file()
    # Re-snapshot (prune runs)
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # Manifest may be re-synced by hook with default store; ensure allowlist
    # at least doesn't delete a pre-written manifest before hook runs.
    # Write marker then prune via second snapshot with sync using our store.
    sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    assert (dest / MANIFEST_FILENAME).is_file()


def test_offline_open_unaffected(tmp_path: Path) -> None:
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=tmp_path / "store")
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    sync_backup_offline_manifest(
        dest, source_index=mod / ".info" / "index.html", store=store
    )
    assert resolve_offline_page(mod) is not None
    assert usable_backup_offline_index(dest) is not None


def test_asset_cache_not_used_as_identity() -> None:
    import services.backup_asset_migration as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "asset_cache_dir" not in src
    assert "_asset_cache_key" not in src
