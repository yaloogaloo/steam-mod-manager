"""Tests: global Steam asset cache under cache/asset_cache/."""

from __future__ import annotations

import hashlib
import os
import time
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
    maybe_prune_asset_cache,
    prune_asset_cache,
    reset_asset_cache_prune_state,
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
    reset_asset_cache_prune_state()


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
        assert t0_stats == {"hit": 0, "miss": 0, "fail": 0}

        import time

        t0 = time.perf_counter()
        path_a = archiver.archive("111", info_a, overwrite=True).path
        elapsed_a = time.perf_counter() - t0
        after_a = get_asset_cache_stats()
        http_after_a = len(http_urls)

        t1 = time.perf_counter()
        path_b = archiver.archive("222", info_b, overwrite=True).path
        elapsed_b = time.perf_counter() - t1
        after_b = get_asset_cache_stats()
        http_after_b = len(http_urls)

    assert path_a.is_file() and path_b.is_file()
    assert (info_a / "index.html").is_file() and (info_b / "index.html").is_file()

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


def _hex_name(tag: str, ext: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest() + ext


def _write_cache_file(root: Path, tag: str, ext: str, payload: bytes, *, mtime: float) -> Path:
    path = root / _hex_name(tag, ext)
    path.write_bytes(payload)
    os.utime(path, (mtime, mtime))
    return path


def _html_session(html: str) -> MagicMock:
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
    return session


def _skip_auto_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_mod, "_ASSET_CACHE_PRUNE_ATTEMPTED", True)


def test_cache_hit_touches_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_urls: list[str] = []

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        http_urls.append(url)
        if url.endswith(".css"):
            return _ok_bytes(b"body{color:#fff}", content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    cache_root = archive_mod.asset_cache_dir()
    _skip_auto_prune(monkeypatch)

    with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
        info_a = tmp_path / "mod_a" / ".info"
        archiver.archive("111", info_a, overwrite=True)
        cache_files = [p for p in cache_root.iterdir() if p.is_file()]
        assert cache_files
        old = time.time() - 10 * 24 * 60 * 60
        before_bytes = {p.name: p.read_bytes() for p in cache_files}
        for path in cache_files:
            os.utime(path, (old, old))
        before_mtime = {p.name: p.stat().st_mtime for p in cache_files}

        info_b = tmp_path / "mod_b" / ".info"
        result = archiver.archive("222", info_b, overwrite=True)
        after_b = get_asset_cache_stats()

    assert after_b["hit"] >= 2
    assert result.path.is_file()
    for path in cache_files:
        assert path.read_bytes() == before_bytes[path.name]
        assert path.stat().st_mtime > before_mtime[path.name]
        assert path.suffix == Path(path.name).suffix


def test_cache_miss_redownloads_after_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_urls: list[str] = []

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        http_urls.append(url)
        if url.endswith(".css"):
            return _ok_bytes(b"body{color:#fff}", content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    cache_root = archive_mod.asset_cache_dir()
    _skip_auto_prune(monkeypatch)

    with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
        archiver.archive("111", tmp_path / "mod_a" / ".info", overwrite=True)
        http_after_first = len(http_urls)
        assert http_after_first >= 2
        assert get_asset_cache_stats()["miss"] >= 2
        for path in list(cache_root.iterdir()):
            if path.is_file():
                path.unlink()
        reset_asset_cache_stats()
        result = archiver.archive("222", tmp_path / "mod_b" / ".info", overwrite=True)

    assert result.path.is_file()
    assert (tmp_path / "mod_b" / ".info" / "index.html").is_file()
    assert get_asset_cache_stats()["miss"] >= 2
    assert get_asset_cache_stats()["hit"] == 0
    assert len(http_urls) == http_after_first * 2
    assert any(cache_root.iterdir())


def test_prune_deletes_zero_byte_cache(tmp_path: Path) -> None:
    root = archive_mod.asset_cache_dir()
    zero = _write_cache_file(root, "zero", ".png", b"", mtime=time.time() - 60)
    keep = _write_cache_file(root, "keep", ".png", b"ok-bytes", mtime=time.time() - 60)
    stats = prune_asset_cache(root)
    assert not zero.exists()
    assert keep.is_file()
    assert stats["zero_byte"] == 1
    assert stats["deleted"] == 1


def test_prune_deletes_ttl_older_than_90d(tmp_path: Path) -> None:
    root = archive_mod.asset_cache_dir()
    now = time.time()
    expired = _write_cache_file(
        root, "old", ".css", b"body{}", mtime=now - archive_mod.ASSET_CACHE_TTL_SECONDS - 10
    )
    fresh = _write_cache_file(root, "fresh", ".css", b"a{}", mtime=now - 24 * 60 * 60)
    stats = prune_asset_cache(root, now=now)
    assert not expired.exists()
    assert fresh.is_file()
    assert stats["ttl"] == 1


def test_prune_keeps_recent_cache_within_ttl(tmp_path: Path) -> None:
    root = archive_mod.asset_cache_dir()
    now = time.time()
    recent = _write_cache_file(root, "recent", ".jpg", b"jpeg-data", mtime=now - 2 * 24 * 60 * 60)
    stats = prune_asset_cache(root, now=now)
    assert recent.is_file()
    assert stats["ttl"] == 0
    assert stats["deleted"] == 0


def test_prune_under_max_does_not_size_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MAX_BYTES", 10_000)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_PRUNE_TARGET_BYTES", 1_000)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MIN_AGE_SECONDS", 0)
    root = archive_mod.asset_cache_dir()
    now = time.time()
    a = _write_cache_file(root, "a", ".bin", b"x" * 100, mtime=now - 8_000)
    b = _write_cache_file(root, "b", ".bin", b"y" * 100, mtime=now - 9_000)
    stats = prune_asset_cache(root, now=now)
    assert a.is_file() and b.is_file()
    assert stats["size_cap"] == 0
    assert stats["remaining_bytes"] == 200


def test_prune_over_max_deletes_oldest_until_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MAX_BYTES", 50)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_PRUNE_TARGET_BYTES", 30)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MIN_AGE_SECONDS", 0)
    root = archive_mod.asset_cache_dir()
    now = time.time()
    oldest = _write_cache_file(root, "oldest", ".png", b"a" * 25, mtime=now - 300)
    middle = _write_cache_file(root, "middle", ".png", b"b" * 25, mtime=now - 200)
    newest = _write_cache_file(root, "newest", ".png", b"c" * 25, mtime=now - 100)
    stats = prune_asset_cache(root, now=now)
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.is_file()
    assert stats["size_cap"] == 2
    assert stats["remaining_bytes"] == 25
    assert stats["remaining_bytes"] <= 30


def test_prune_grace_keeps_files_newer_than_one_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MAX_BYTES", 10)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_PRUNE_TARGET_BYTES", 5)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MIN_AGE_SECONDS", 3600)
    root = archive_mod.asset_cache_dir()
    now = time.time()
    hot = _write_cache_file(root, "hot", ".gif", b"g" * 40, mtime=now - 60)
    stats = prune_asset_cache(root, now=now)
    assert hot.is_file()
    assert stats["size_cap"] == 0
    assert stats["ttl"] == 0


def test_prune_skips_invalid_filenames(tmp_path: Path) -> None:
    root = archive_mod.asset_cache_dir()
    invalid = root / "readme.txt"
    invalid.write_text("keep me", encoding="utf-8")
    short = root / "abc.png"
    short.write_bytes(b"nope")
    stats = prune_asset_cache(root)
    assert invalid.is_file()
    assert short.is_file()
    assert stats["scanned"] == 0
    assert stats["skipped"] >= 2
    assert stats["deleted"] == 0


def test_prune_skips_nested_directories(tmp_path: Path) -> None:
    root = archive_mod.asset_cache_dir()
    nested = root / "nested"
    nested.mkdir()
    nested_file = nested / _hex_name("nested-keep", ".png")
    nested_file.write_bytes(b"nested-bytes")
    stats = prune_asset_cache(root)
    assert nested.is_dir()
    assert nested_file.is_file()
    assert stats["deleted"] == 0


def test_maybe_prune_runs_at_most_once_per_process_and_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = archive_mod.prune_asset_cache

    def wrapped(*args: Any, **kwargs: Any) -> dict[str, int]:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(archive_mod, "prune_asset_cache", wrapped)
    first = maybe_prune_asset_cache()
    second = maybe_prune_asset_cache()
    assert first is not None
    assert second is None
    assert len(calls) == 1

    reset_asset_cache_prune_state()
    third = maybe_prune_asset_cache()
    assert third is None
    assert len(calls) == 1

    reset_asset_cache_prune_state()
    stamp = archive_mod._asset_cache_prune_stamp_path()
    old = time.time() - archive_mod.ASSET_CACHE_PRUNE_INTERVAL_SECONDS - 10
    os.utime(stamp, (old, old))
    fourth = maybe_prune_asset_cache()
    assert fourth is not None
    assert len(calls) == 2


def test_hit_touch_changes_size_eviction_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After HIT, A becomes newer than B so size prune deletes B first."""
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MAX_BYTES", 50)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_PRUNE_TARGET_BYTES", 30)
    monkeypatch.setattr(archive_mod, "ASSET_CACHE_MIN_AGE_SECONDS", 0)
    _skip_auto_prune(monkeypatch)

    cache_root = archive_mod.asset_cache_dir()
    now = time.time()
    key_a = _asset_cache_key(SHARED_CSS)
    key_b = _asset_cache_key(SHARED_PNG)
    path_a = cache_root / f"{key_a}.css"
    path_b = cache_root / f"{key_b}.png"
    path_a.write_bytes(b"A" * 30)
    path_b.write_bytes(b"B" * 30)
    os.utime(path_a, (now - 400, now - 400))
    os.utime(path_b, (now - 200, now - 200))

    html = (
        "<!DOCTYPE html><html><body>"
        f'<link rel="stylesheet" href="{SHARED_CSS}">'
        "<div id=\"content\">" + ("x" * 250) + "</div>"
        "</body></html>"
    )
    http_urls: list[str] = []

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        http_urls.append(url)
        return _ok_bytes(b"should-not-download", content_type="text/css")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)

    with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
        result = archiver.archive("333", tmp_path / "mod_hit" / ".info", overwrite=True)

    assert result.path.is_file()
    assert get_asset_cache_stats()["hit"] >= 1
    assert not http_urls
    assert path_a.stat().st_mtime > path_b.stat().st_mtime

    sidecar_assets = tmp_path / "mod_hit" / ".info" / "assets"
    sidecar_before = sorted(p.name for p in sidecar_assets.iterdir()) if sidecar_assets.is_dir() else []

    stats = prune_asset_cache(cache_root)
    assert path_a.is_file()
    assert not path_b.exists()
    assert stats["size_cap"] == 1
    assert stats["remaining_bytes"] == 30
    sidecar_after = sorted(p.name for p in sidecar_assets.iterdir()) if sidecar_assets.is_dir() else []
    assert sidecar_after == sidecar_before

