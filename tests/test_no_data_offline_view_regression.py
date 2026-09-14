"""Regression: retired data/offline_view must never be recreated."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_store import AssetStore
from services.backup_asset_migration import repair_info_assets_from_backup_store
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
)
from services.offline.backup_closure import snapshot_offline_closure


TINY = b"no-data-offline-view-aaa"


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
    (info / "internal_id").write_text("9001", encoding="utf-8")
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    return folder


def _isolate_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Return (data_root, cache_ov, store_root) with paths patched."""
    data_root = tmp_path / "data"
    cache_root = tmp_path / "cache"
    store_root = data_root / "asset_store"
    data_root.mkdir(parents=True)
    cache_ov = cache_root / "offline_view"

    def _data_dir() -> Path:
        data_root.mkdir(parents=True, exist_ok=True)
        return data_root

    def _cache_dir() -> Path:
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root

    def _ov() -> Path:
        cache_ov.mkdir(parents=True, exist_ok=True)
        return cache_ov

    monkeypatch.setattr("core.paths.data_dir", _data_dir)
    monkeypatch.setattr("core.paths.get_cache_dir", _cache_dir)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", _ov)
    monkeypatch.setattr("core.paths.offline_view_dir", _ov)
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    return data_root, cache_ov, store_root


def test_open_writes_cache_not_data_offline_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, cache_ov, store_root = _isolate_roots(tmp_path, monkeypatch)
    legacy = data_root / "offline_view"
    mod = _steam_mod(tmp_path / "Mod", {"a.png": TINY})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    assert not (mod / ".info" / "assets").exists()

    opened = ensure_live_offline_openable(mod, store=store, mod_id="9001")
    assert opened is not None
    opened_s = str(opened.resolve())
    assert str(cache_ov.resolve()) in opened_s
    assert "offline_view" in opened_s
    assert not legacy.exists()


def test_miss_repair_does_not_create_data_offline_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, _cache_ov, store_root = _isolate_roots(tmp_path, monkeypatch)
    legacy = data_root / "offline_view"
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: data_root / "mod_backup" / str(mid),
    )
    mod = _steam_mod(tmp_path / "ModR", {"a.png": TINY})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    dest = data_root / "mod_backup" / "9" / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.asset_manifest import MANIFEST_FILENAME

    (mod / ".info" / MANIFEST_FILENAME).unlink()
    repair = repair_info_assets_from_backup_store(mod, mod_id="9", store=store)
    assert repair.ok
    assert not legacy.exists()
    assert not (mod / ".info" / "assets").exists()


def test_backup_snapshot_does_not_create_data_offline_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, _cache_ov, store_root = _isolate_roots(tmp_path, monkeypatch)
    legacy = data_root / "offline_view"
    mod = _steam_mod(tmp_path / "ModB", {"a.png": TINY})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    dest = tmp_path / "backup_offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    assert not legacy.exists()


def test_cache_wipe_rebuilds_cache_offline_view_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, cache_ov, store_root = _isolate_roots(tmp_path, monkeypatch)
    legacy = data_root / "offline_view"
    (data_root / "mod_manager.db").write_bytes(b"db")
    store_root.mkdir(parents=True, exist_ok=True)
    (store_root / "keep").write_bytes(b"cas")
    bak = data_root / "mod_backup" / "1" / "offline"
    bak.mkdir(parents=True)
    (bak / "manifest.json").write_text("{}", encoding="utf-8")

    mod = _steam_mod(tmp_path / "ModC", {"a.png": TINY})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    opened1 = ensure_live_offline_openable(mod, store=store, mod_id="9001")
    assert opened1 is not None

    shutil.rmtree(cache_ov.parent)
    assert not cache_ov.exists()
    assert (data_root / "mod_manager.db").is_file()
    assert (store_root / "keep").is_file()
    assert (bak / "manifest.json").is_file()

    opened2 = ensure_live_offline_openable(mod, store=store, mod_id="9001")
    assert opened2 is not None
    assert str(cache_ov.resolve()) in str(opened2.resolve())
    assert not legacy.exists()
    assert not (mod / ".info" / "assets").exists()


def test_project_data_offline_view_must_not_exist() -> None:
    from core.paths import get_cache_dir, offline_view_cache_dir, project_root

    project_legacy = project_root() / "data" / "offline_view"
    assert not project_legacy.exists(), (
        f"retired path still present: {project_legacy} — delete data/offline_view"
    )
    ov = offline_view_cache_dir()
    assert ov.parent == get_cache_dir()
    try:
        ov.resolve().relative_to((project_root() / "data").resolve())
        raise AssertionError("offline_view_cache_dir resolved under data/")
    except ValueError:
        pass
