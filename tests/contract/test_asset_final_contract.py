"""Phase 12: leftover inventory isolation, capture staging, Store-only OPEN."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME
from services.asset_store import AssetStore, sha256_bytes
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
)
from services.offline.html_rewriter import rewrite_imported_html
from services.offline.staging import (
    capture_staging_root,
    resolve_capture_assets_dir,
)
from services.offline_view_cache import is_offline_view_path

PAYLOAD = b"phase12-store-only-bytes"


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def test_capture_staging_not_in_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_temp = tmp_path / "cache_temp"
    monkeypatch.setattr("core.paths.cache_temp_dir", lambda: cache_temp)

    mod = tmp_path / "Mod"
    info = mod / ".info"
    info.mkdir(parents=True)
    src = tmp_path / "page.html"
    img = tmp_path / "hero.png"
    img.write_bytes(PAYLOAD)
    src.write_text(
        '<html><body><img src="hero.png"></body></html>', encoding="utf-8"
    )

    rewrite_imported_html(
        src.read_text(encoding="utf-8"),
        html_path=src,
        output_dir=info,
    )
    staged = resolve_capture_assets_dir(info, create=False)
    assert staged.is_dir()
    assert any(staged.rglob("*"))
    assert not (info / "assets").exists()
    assert str(cache_temp / "offline_staging") in str(capture_staging_root(info))


def test_cache_delete_rebuild_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    view = tmp_path / "cache" / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store.root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view)

    mod = tmp_path / "Mod"
    info = mod / ".info"
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(PAYLOAD)
    (info / "index.html").write_text(
        '<html><body><img src="./assets/a.png"></body></html>', encoding="utf-8"
    )
    assert finalize_live_offline_to_cas(mod, store=store, mod_id="12").ok
    first = ensure_live_offline_openable(mod, store=store, mod_id="12")
    assert first is not None
    assert is_offline_view_path(first)
    shutil.rmtree(view)
    assert not (mod / ".info" / "assets").exists()
    again = ensure_live_offline_openable(mod, store=store, mod_id="12")
    assert again is not None
    assert is_offline_view_path(again)
    assert (again.parent / "assets" / "a.png").read_bytes() == PAYLOAD


def test_asset_store_is_only_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    view = tmp_path / "cache" / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store.root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view)

    mod = tmp_path / "Mod"
    info = mod / ".info"
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(PAYLOAD)
    (info / "index.html").write_text(
        '<html><body><img src="./assets/a.png"></body></html>', encoding="utf-8"
    )
    assert finalize_live_offline_to_cas(mod, store=store, mod_id="13").ok
    digest = sha256_bytes(PAYLOAD)
    assert store.has(digest)
    assert (info / MANIFEST_FILENAME).is_file()

    # Plant a leftover tree with different bytes — OPEN must ignore it.
    leftover = info / "assets"
    leftover.mkdir(parents=True)
    (leftover / "a.png").write_bytes(b"stale-leftover-not-source")
    opened = ensure_live_offline_openable(mod, store=store, mod_id="13")
    assert opened is not None
    assert is_offline_view_path(opened)
    assert opened != (info / "index.html").resolve()
    assert (opened.parent / "assets" / "a.png").read_bytes() == PAYLOAD
    assert (leftover / "a.png").read_bytes() == b"stale-leftover-not-source"
