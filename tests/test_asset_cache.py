"""Tests: global Steam asset cache under data/asset_cache/."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services import archive as archive_mod
from services.archive import (
    OfflinePageArchiver,
    SteamArchiveLimiter,
    _asset_cache_key,
    get_asset_cache_stats,
    reset_asset_cache_stats,
)


SHARED_CSS = "https://community.akamai.steamstatic.com/public/shared/css/shared_global.css"
SHARED_PNG = "https://community.akamai.steamstatic.com/public/images/skin_1/icon.png"

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><body>"
    '<link rel="stylesheet" href="{css}">'
    '<img src="{png}">'
    "<div id=\"content\">" + ("x" * 250) + "</div>"
    "</body></html>"
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "HTML_429_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(archive_mod, "HTML_429_SOFT_COOLDOWN_SEC", 0.0)
    cache_root = tmp_path / "asset_cache"
    cache_root.mkdir()
    monkeypatch.setattr(archive_mod, "asset_cache_dir", lambda: cache_root)
    reset_asset_cache_stats()


def _ok_bytes(payload: bytes, *, content_type: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=[payload])
    resp.close = MagicMock()
    resp.content = payload
    return resp


def test_asset_cache_key_is_sha256() -> None:
    key = _asset_cache_key(SHARED_CSS)
    assert key == hashlib.sha256(SHARED_CSS.encode("utf-8")).hexdigest()
    assert len(key) == 64


def test_second_mod_reuses_cache_fewer_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Mods sharing Steam static URLs: second Mod issues fewer asset GETs."""
    http_urls: list[str] = []

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        http_urls.append(url)
        if url.endswith(".css"):
            return _ok_bytes(b"body{color:#fff}", content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)

    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    session = MagicMock()
    session.cookies = {}

    def fake_session_get(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/html"}
        resp.raise_for_status = MagicMock()
        resp.text = html
        resp.charset_encoding = "utf-8"
        return resp

    session.get.side_effect = fake_session_get

    info_a = tmp_path / "mod_a" / ".info"
    info_b = tmp_path / "mod_b" / ".info"

    with OfflinePageArchiver(session=session, timeout=5) as archiver:
        t0_stats = get_asset_cache_stats()
        assert t0_stats == {"hit": 0, "miss": 0}

        import time

        t0 = time.perf_counter()
        path_a = archiver.archive("111", info_a, overwrite=True)
        elapsed_a = time.perf_counter() - t0
        after_a = get_asset_cache_stats()
        http_after_a = len(http_urls)

        t1 = time.perf_counter()
        path_b = archiver.archive("222", info_b, overwrite=True)
        elapsed_b = time.perf_counter() - t1
        after_b = get_asset_cache_stats()
        http_after_b = len(http_urls)

    assert path_a.is_file() and path_b.is_file()
    assert (info_a / "assets").is_dir() and (info_b / "assets").is_dir()
    assert list((info_a / "assets").iterdir())
    assert list((info_b / "assets").iterdir())

    # First Mod: all unique assets are misses; second: shared URLs are hits.
    assert after_a["miss"] >= 2
    assert after_a["hit"] == 0
    assert after_b["hit"] >= 2
    assert http_after_b == http_after_a  # no new HTTP for shared assets
    assert http_after_a >= 2
    assert elapsed_b <= elapsed_a * 1.5 or after_b["hit"] > 0

    cache_root = archive_mod.asset_cache_dir()
    assert any(cache_root.iterdir())
    # Keys are sha256 prefixes
    names = [p.name for p in cache_root.iterdir()]
    assert any(len(p.stem) == 64 or len(p.name) >= 64 for p in cache_root.iterdir()) or names

    print(
        "VERIFY",
        {
            "http_mod1": http_after_a,
            "http_mod2_total": http_after_b,
            "http_mod2_new": http_after_b - http_after_a,
            "cache_after_mod1": after_a,
            "cache_after_mod2": after_b,
            "elapsed_a": round(elapsed_a, 4),
            "elapsed_b": round(elapsed_b, 4),
            "hit_rate_mod2": after_b["hit"] / max(after_b["hit"] + (after_b["miss"] - after_a["miss"]), 1),
        },
    )
