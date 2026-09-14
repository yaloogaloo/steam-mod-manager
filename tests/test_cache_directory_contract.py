"""Contract: cache/ is regenerable; data/ business state is independent."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from core.paths import project_root
from services.asset_manifest import AssetManifest, AssetReference
from services.asset_store import AssetStore
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
)
from services.offline_view_cache import FINGERPRINT_FILENAME, fingerprint_manifest


TINY_A = b"cache-contract-aaa"


@pytest.fixture(autouse=True)
def _cas_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.cas_runtime import reset_cas_runtime_cache

    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def _steam_mod(folder: Path, assets: dict[str, bytes]) -> Path:
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "internal_id").write_text("424242", encoding="utf-8")
    asset_dir = info / "assets"
    asset_dir.mkdir(parents=True)
    for name, blob in assets.items():
        (asset_dir / name).write_bytes(blob)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    return folder


def test_cache_helpers_point_under_cache_not_data() -> None:
    import core.paths as paths

    cache = paths.get_cache_dir()
    ov = paths.offline_view_cache_dir()
    assert ov == cache / "offline_view"
    assert paths.offline_view_dir() == ov
    assert ov.parent == cache
    try:
        ov.resolve().relative_to((project_root() / "data").resolve())
        raise AssertionError("offline_view must not resolve under data/")
    except ValueError:
        pass


def test_cache_delete_does_not_affect_business_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.paths as paths

    cache = tmp_path / "cache"
    data = tmp_path / "data"
    data.mkdir()
    (data / "mod_manager.db").write_bytes(b"sqlite")
    store = data / "asset_store"
    store.mkdir()
    (store / "obj").write_bytes(b"cas")
    backup = data / "mod_backup" / "1"
    backup.mkdir(parents=True)
    (backup / "offline").mkdir()
    (backup / "offline" / "manifest.json").write_text("{}", encoding="utf-8")

    def _ov() -> Path:
        p = cache / "offline_view"
        p.mkdir(parents=True, exist_ok=True)
        return p

    monkeypatch.setattr(
        paths,
        "get_cache_dir",
        lambda: cache.mkdir(parents=True, exist_ok=True) or cache,
    )
    monkeypatch.setattr(paths, "offline_view_cache_dir", _ov)
    monkeypatch.setattr(paths, "offline_view_dir", _ov)

    junk = _ov() / "live_1"
    junk.mkdir(parents=True)
    (junk / "index.html").write_text("x", encoding="utf-8")

    shutil.rmtree(cache)
    assert not cache.exists()
    assert (data / "mod_manager.db").is_file()
    assert (store / "obj").is_file()
    assert (backup / "offline" / "manifest.json").is_file()
    assert paths.get_cache_dir() == cache
    assert cache.is_dir()


def test_offline_view_open_hit_and_miss_rematerialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "cache" / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    monkeypatch.setattr(
        "core.cas_runtime.cas_only_info_asset_runtime", lambda: True
    )

    mod = _steam_mod(tmp_path / "ModA", {"a.png": TINY_A})
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(mod, store=store).ok
    assert not (mod / ".info" / "assets").exists()

    t0 = time.perf_counter()
    opened1 = ensure_live_offline_openable(mod, store=store, mod_id="424242")
    miss_ms = (time.perf_counter() - t0) * 1000.0
    assert opened1 is not None
    assert "offline_view" in str(opened1)
    assert (opened1.parent / FINGERPRINT_FILENAME).is_file()
    assert (opened1.parent / "assets" / "a.png").is_file()
    assert opened1.parent.name == "live_424242"

    t1 = time.perf_counter()
    opened2 = ensure_live_offline_openable(mod, store=store, mod_id="424242")
    hit_ms = (time.perf_counter() - t1) * 1000.0
    assert opened2 is not None
    assert opened2.resolve() == opened1.resolve()
    assert hit_ms < 100.0, (
        f"cache hit too slow: {hit_ms:.1f}ms (miss was {miss_ms:.1f}ms)"
    )

    shutil.rmtree(view_root)
    assert not (mod / ".info" / "assets").exists()
    opened3 = ensure_live_offline_openable(mod, store=store, mod_id="424242")
    assert opened3 is not None
    assert (opened3.parent / "assets" / "a.png").is_file()
    assert not (mod / ".info" / "assets").exists()


def test_cache_not_source_of_truth_paths() -> None:
    import core.paths as paths

    # Helpers: cache vs data layering (ignore pytest data_dir isolation root).
    assert paths.offline_view_cache_dir().name == "offline_view"
    assert paths.offline_view_cache_dir().parent == paths.get_cache_dir()
    assert paths.asset_store_dir().name == "asset_store"
    assert paths.database_path().name == "mod_manager.db"
    assert paths.offline_view_dir() == paths.offline_view_cache_dir()
    ov = paths.offline_view_cache_dir().resolve()
    assert ov.parent == paths.get_cache_dir().resolve()

def test_fingerprint_stable_roundtrip() -> None:
    m = AssetManifest(
        assets=[
            AssetReference(path="assets/a.png", sha256="a" * 64, size=1),
            AssetReference(path="assets/b.png", sha256="b" * 64, size=2),
        ]
    )
    fp = fingerprint_manifest(m)
    assert len(fp) == 64
    assert fingerprint_manifest(m) == fp
