"""Targeted tests: Archive asset cache/seen, proxy fallback, fail stats."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from curl_cffi.requests.exceptions import CurlError

from services import archive as archive_mod
from services.archive import (
    GLOBAL_ASSET_WORKERS,
    OfflinePageArchiver,
    SteamArchiveLimiter,
    _CSS_LOCALIZED_MARK,
    get_asset_cache_stats,
    reset_asset_cache_stats,
)


SHARED_CSS = "https://community.akamai.steamstatic.com/public/shared/css/shared_global.css"
SHARED_PNG = "https://community.akamai.steamstatic.com/public/images/skin_1/icon.png"
NESTED_WOFF = "https://community.akamai.steamstatic.com/public/shared/fonts/motiva-sans.woff2"

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><body>"
    '<link rel="stylesheet" href="{css}">'
    '<img src="{png}">'
    "<div id=\"content\">" + ("x" * 250) + "</div>"
    "</body></html>"
)

PROXY_DICT = {
    "http": "http://127.0.0.1:12450",
    "https": "http://127.0.0.1:12450",
}


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


def _ok_bytes(payload: bytes, *, content_type: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=[payload])
    resp.close = MagicMock()
    resp.content = payload
    resp._smm_via = "direct"
    return resp


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


def test_global_asset_workers_unchanged() -> None:
    assert GLOBAL_ASSET_WORKERS == 6
    assert archive_mod.ASSET_DOWNLOAD_WORKERS == 6


def test_asset_timeout_does_not_direct_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        calls.append("proxy" if kwargs.get("proxies") else "direct")
        raise CurlError("curl: (28) Operation timed out after 15000 milliseconds")

    monkeypatch.setattr(archive_mod.curl_requests, "get", fake_get)
    with OfflinePageArchiver(proxies=PROXY_DICT, timeout=5) as archiver:
        with pytest.raises(CurlError):
            archiver._perform_asset_get(
                "https://community.akamai.steamstatic.com/x.png",
                {"timeout": 5},
            )
    assert calls == ["proxy"]


def test_asset_immediate_proxy_down_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        calls.append("proxy" if kwargs.get("proxies") else "direct")
        if kwargs.get("proxies"):
            raise ConnectionError("connection refused")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(archive_mod.curl_requests, "get", fake_get)
    with OfflinePageArchiver(proxies=PROXY_DICT, timeout=5) as archiver:
        resp = archiver._perform_asset_get(
            "https://community.akamai.steamstatic.com/x.png",
            {"timeout": 5},
        )
    assert calls == ["proxy", "direct"]
    assert getattr(resp, "_smm_via", "") == "direct"


def test_failed_asset_increments_fail_and_logs_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger=archive_mod.logger.name)

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        if url.endswith(".png"):
            raise CurlError("curl: (28) Operation timed out after 15000 milliseconds")
        return _ok_bytes(b"body{color:#fff}", content_type="text/css")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    info = tmp_path / "mod" / ".info"
    with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
        result = archiver.archive("111", info, overwrite=True)

    assert result.path.is_file()
    stats = get_asset_cache_stats()
    assert stats["fail"] >= 1
    assert stats["miss"] >= 1
    assert "[ARCHIVE ASSET] result=fail" in caplog.text
    assert "failure_type=timeout" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "url=" in caplog.text
    assert "[ARCHIVE ASSETS]" in caplog.text
    assert "nested_ok=" in caplog.text


def test_css_nested_url_localized_and_cache_hit_skips_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http_urls: list[str] = []
    css_body = (
        f"body{{color:#fff;background:url('{SHARED_PNG}')}}"
        f"@font-face{{src:url('{NESTED_WOFF}')}}"
    ).encode("utf-8")

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        http_urls.append(url)
        if url.endswith(".css"):
            return _ok_bytes(css_body, content_type="text/css")
        if url.endswith(".woff2"):
            return _ok_bytes(b"wOFF", content_type="font/woff2")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    info_a = tmp_path / "mod_a" / ".info"
    info_b = tmp_path / "mod_b" / ".info"

    with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
        path_a = archiver.archive("111", info_a, overwrite=True).path
        http_after_a = list(http_urls)
        after_a = get_asset_cache_stats()
        path_b = archiver.archive("222", info_b, overwrite=True).path
        after_b = get_asset_cache_stats()

    assert path_a.is_file() and path_b.is_file()
    assert len(set(http_after_a)) == len(http_after_a)
    assert set(http_after_a) == {SHARED_CSS, SHARED_PNG, NESTED_WOFF}
    assert http_urls == http_after_a
    assert after_a["miss"] >= 3
    assert after_a["fail"] == 0
    assert after_b["hit"] >= 3

    css_files = list((info_a / "assets").glob("*.css"))
    assert css_files
    rewritten = css_files[0].read_text(encoding="utf-8")
    assert _CSS_LOCALIZED_MARK in rewritten
    assert SHARED_PNG not in rewritten
    assert NESTED_WOFF not in rewritten
    assert "url(" in rewritten
    woff_files = list((info_a / "assets").glob("*.woff2"))
    png_files = list((info_a / "assets").glob("*.png"))
    assert woff_files and png_files
    assert woff_files[0].name in rewritten
    assert png_files[0].name in rewritten


def test_css_self_url_does_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    css_body = f"body{{background:url('{SHARED_CSS}')}}".encode("utf-8")

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        if url.endswith(".css"):
            return _ok_bytes(css_body, content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    html = HTML_TEMPLATE.format(css=SHARED_CSS, png=SHARED_PNG)
    info = tmp_path / "mod" / ".info"
    done = threading.Event()
    error: list[BaseException] = []

    def _run() -> None:
        try:
            with OfflinePageArchiver(session=_html_session(html), timeout=5) as archiver:
                archiver.archive("111", info, overwrite=True)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert done.wait(8), "CSS self-url localization deadlocked"
    assert not error, error[0]
    css_files = list((info / "assets").glob("*.css"))
    assert css_files
    text = css_files[0].read_text(encoding="utf-8")
    assert _CSS_LOCALIZED_MARK in text
    assert "url(" in text
    assert SHARED_CSS not in text
