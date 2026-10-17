"""Regression: CAS_ONLY capture → no durable .info/assets → OPEN via offline_view."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
    missing_live_asset_refs,
)
from services.mod_metadata_resolver import ModMetadataResolver


CSS_BYTES = (
    b"@font-face{font-family:Demo;src:url(demo.woff2) format('woff2')}"
    b"body{font-family:Demo,sans-serif;background:url(bg.png) no-repeat}"
)
IMG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-hero-bytes-0123456789"
FONT_BYTES = b"wOF2" + b"fake-woff2-font-payload-abcdefgh"
BG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-bg-bytes-9876543210"


@pytest.fixture(autouse=True)
def _cas_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def _write_simulated_offline_page(info: Path) -> None:
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "style.css").write_bytes(CSS_BYTES)
    (assets / "hero.png").write_bytes(IMG_BYTES)
    (assets / "demo.woff2").write_bytes(FONT_BYTES)
    (assets / "bg.png").write_bytes(BG_BYTES)
    (info / "index.html").write_text(
        """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="./assets/style.css">
</head><body>
<img src="./assets/hero.png" alt="hero">
<p>cas-only regression</p>
</body></html>
""",
        encoding="utf-8",
    )


def test_cas_only_capture_open_materializes_html_css_image_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "asset_store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)

    mod = tmp_path / "ModA"
    info = mod / ".info"
    _write_simulated_offline_page(info)

    store = AssetStore(root=store_root)
    # Mirror Steam archive finalize call site (info_dir, not mod root).
    result = finalize_live_offline_to_cas(info, store=store, mod_id="9001")
    assert result.ok
    assert result.cleared_files == 4
    assert not (info / "assets").exists()
    assert (info / MANIFEST_FILENAME).is_file()

    man = AssetManifest.from_path(info / MANIFEST_FILENAME)
    assert {a.path for a in man.assets} == {
        "assets/style.css",
        "assets/hero.png",
        "assets/demo.woff2",
        "assets/bg.png",
    }
    for ref in man.assets:
        assert store.verify(ref.sha256)

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert "offline_view" in str(opened)
    assert opened.name == "index.html"
    assert not (info / "assets").exists()

    view = opened.parent
    for name, payload in (
        ("style.css", CSS_BYTES),
        ("hero.png", IMG_BYTES),
        ("demo.woff2", FONT_BYTES),
        ("bg.png", BG_BYTES),
    ):
        path = view / "assets" / name
        assert path.is_file(), name
        assert sha256_file(path) == sha256_bytes(payload)

    html = opened.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)=["\'](\./assets/[^"\']+)["\']', html)
    assert refs
    for rel in refs:
        assert (view / rel.lstrip("./")).is_file(), rel
    assert missing_live_asset_refs(opened) == []

    # CSS-nested font + background must resolve beside the stylesheet.
    css_text = (view / "assets" / "style.css").read_text(encoding="utf-8")
    assert "demo.woff2" in css_text
    assert "bg.png" in css_text
    assert (view / "assets" / "demo.woff2").is_file()
    assert (view / "assets" / "bg.png").is_file()


def test_resolver_does_not_open_raw_info_after_cas_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "asset_store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)

    mod = tmp_path / "ModB"
    info = mod / ".info"
    _write_simulated_offline_page(info)
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(info, store=store).ok
    assert not (info / "assets").exists()

    # Presence probe / OPEN are separate: Detail resolve uses probe (no materialize).
    # When probe finds nothing, CAS_ONLY must still NOT fall back to raw .info.
    monkeypatch.setattr(
        "services.info_asset_runtime.probe_live_offline_available",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.info_asset_runtime.ensure_live_offline_openable",
        lambda *_a, **_k: None,
    )
    resolver = ModMetadataResolver()
    # Bypass full resolve identity: call the LIVE existing helper directly.
    opened = resolver._offline_existing(mod, backup=None)
    assert opened is None


def test_open_prefers_steam_cas_over_empty_offline_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "asset_store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)

    mod = tmp_path / "ModC"
    info = mod / ".info"
    _write_simulated_offline_page(info)
    store = AssetStore(root=store_root)
    assert finalize_live_offline_to_cas(info, store=store).ok

    # Preferred layout stub with empty usable manifest must not win OPEN.
    offline = info / "offline"
    offline.mkdir(parents=True)
    (offline / "index.html").write_text(
        "<html><body>empty stub</body></html>", encoding="utf-8"
    )
    (offline / MANIFEST_FILENAME).write_text(
        '{"schema_version":1,"assets":[]}', encoding="utf-8"
    )

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert "offline_view" in str(opened)
    assert (opened.parent / "assets" / "hero.png").is_file()
    assert (opened.parent / "assets" / "style.css").is_file()
    html = opened.read_text(encoding="utf-8")
    assert "hero.png" in html
    assert "empty stub" not in html
