"""Phase 6: CAS-only LIVE .info asset runtime (no durable .info/assets SoT)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.backup_asset_migration import (
    restore_backup_assets_from_store,
)
from services.info_asset_runtime import (
    clear_info_assets_dir,
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
    repair_live_from_cas,
)
from services.offline.backup_closure import (
    ensure_backup_offline_openable,
    snapshot_offline_closure,
)

TINY_A = b"phase6-cas-only-aaa"
TINY_B = b"phase6-cas-only-bbb"


@pytest.fixture(autouse=True)
def _cas_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


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


def test_finalize_clears_info_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    result = finalize_live_offline_to_cas(mod, store=AssetStore(root=store_root))
    assert result.ok
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    assert not (mod / ".info" / "assets").exists()
    store = AssetStore(root=store_root)
    man = AssetManifest.from_path(mod / ".info" / MANIFEST_FILENAME)
    assert len(man.assets) == 2
    for ref in man.assets:
        assert store.verify(ref.sha256)


def test_repair_does_not_recreate_info_assets(
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
    assert finalize_live_offline_to_cas(mod, store=store).ok
    dest = tmp_path / "backup" / "9" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # wipe LIVE manifest temporarily then repair from Backup
    (mod / ".info" / MANIFEST_FILENAME).unlink()
    repair = repair_live_from_cas(mod, mod_id="9", store=store)
    assert repair.ok
    assert repair.reason == "cas_verified_no_durable_info_assets"
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    # OPEN still works
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").is_file()


def test_miss_recovery_via_backup_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    finalize_live_offline_to_cas(mod, store=store)
    dest = tmp_path / "backup" / "10" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    # MISS: wipe LIVE assets (already gone) + keep index+manifest, then wipe assets if any
    clear_info_assets_dir(mod / ".info")
    repair = repair_live_from_cas(mod, mod_id="10", store=store)
    assert repair.ok
    assert not (mod / ".info" / "assets").exists()
    opened = ensure_backup_offline_openable(dest)
    assert opened is not None
    assert "offline_view" in str(opened)


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
    finalize_live_offline_to_cas(mod, store=store)
    dest = tmp_path / "backup" / "11" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    digest = sha256_bytes(TINY_A)
    store.delete(digest)
    repair = repair_live_from_cas(mod, mod_id="11", store=store)
    assert not repair.ok
    assert "CAS" in repair.reason or any(
        "missing" in i.lower() for i in repair.issues
    )


def test_backup_snapshot_without_live_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    finalize_live_offline_to_cas(mod, store=store)
    assert not (mod / ".info" / "assets").exists()
    dest = tmp_path / "bak" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert (dest / MANIFEST_FILENAME).is_file()
    assert not (dest / "assets").exists()
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    assert len(man.assets) == 1
    assert store.verify(man.assets[0].sha256)


def test_restore_still_from_backup_manifest_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    finalize_live_offline_to_cas(mod, store=store)
    dest = tmp_path / "bak" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok
    assert sha256_file(dest / "assets" / "a.png") == sha256_bytes(TINY_A)
