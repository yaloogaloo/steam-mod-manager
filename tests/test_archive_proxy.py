"""Tests: archive proxy injection, stub cooldown, non-blocking ensure path."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services import archive as archive_mod
from services.archive import (
    ARCHIVE_RETRY_COOLDOWN_SEC,
    OfflinePageArchiver,
    SteamArchiveRateLimiter,
    archive_proxies_dict,
    ensure_offline_page_nonblocking_probe,
    is_archive_cooldown_active,
    is_stub_offline_page,
    write_last_archive_attempt,
)


STUB_HTML = """<!DOCTYPE html>
<html><head><title>X — Offline (stub)</title></head>
<body><p>未能下载完整的 Steam 创意工坊原网页</p>
<p>原因: <code>curl: (28) Connection timed out</code></p></body></html>
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    resp.text = "<html>" + ("x" * 300) + "</html>"
    resp.charset_encoding = "utf-8"
    return resp


@pytest.fixture(autouse=True)
def _no_qsettings_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: empty QSettings proxy so tests control injection explicitly."""
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    lim = SteamArchiveRateLimiter(0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)


def test_proxy_url_passed_to_curl_cffi(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = "socks5://127.0.0.1:7897"
    captured: dict[str, Any] = {}

    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        captured["url"] = url
        captured["proxies"] = kwargs.get("proxies")
        return _ok_response()

    session.get.side_effect = fake_get
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: proxy)

    with OfflinePageArchiver(session=session) as archiver:
        archiver._http_get("https://steamcommunity.com/sharedfiles/filedetails/?id=1")

    assert captured["proxies"] == {"http": proxy, "https": proxy}


def test_explicit_proxies_override_qsettings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        captured["proxies"] = kwargs.get("proxies")
        return _ok_response()

    session.get.side_effect = fake_get
    monkeypatch.setattr(
        archive_mod, "_get_archive_proxy", lambda: "socks5://should-not-use:1"
    )

    proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
    with OfflinePageArchiver(session=session, proxies=proxies) as archiver:
        archiver._http_get("https://example.com/")

    assert captured["proxies"] == proxies


def test_empty_proxy_keeps_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return _ok_response()

    session.get.side_effect = fake_get

    with OfflinePageArchiver(session=session) as archiver:
        archiver._http_get("https://example.com/")

    assert "proxies" not in captured["kwargs"]
    assert archive_proxies_dict(None) is None
    assert archive_proxies_dict("") is None


def test_stub_cooldown_skips_archive_within_10_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    stub = _write(info / "index.html", STUB_HTML)
    assert is_stub_offline_page(stub)

    write_last_archive_attempt(info, failed=True, when=time.time())
    assert is_archive_cooldown_active(info) is True

    called = {"archive": 0}

    def boom(*_a: Any, **_k: Any) -> Path:
        called["archive"] += 1
        raise AssertionError("archive must not run during cooldown")

    monkeypatch.setattr(OfflinePageArchiver, "archive", boom)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        path = archiver.ensure_offline_page(info, "3761838546")

    assert path == stub
    assert called["archive"] == 0


def test_stub_cooldown_expired_allows_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    _write(info / "index.html", STUB_HTML)
    write_last_archive_attempt(
        info,
        failed=True,
        when=time.time() - ARCHIVE_RETRY_COOLDOWN_SEC - 1,
    )
    assert is_archive_cooldown_active(info) is False

    called = {"archive": 0}

    def fake_archive(self: OfflinePageArchiver, *a: Any, **k: Any) -> Path:
        called["archive"] += 1
        return info / "index.html"

    monkeypatch.setattr(OfflinePageArchiver, "archive", fake_archive)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        archiver.ensure_offline_page(info, "3761838546")

    assert called["archive"] == 1


def test_archive_failure_path_does_not_block_ui_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    UI must be able to resolve an offline path without waiting on Steam.

    Cooldown + stub => ensure_offline_page returns immediately (no HTTP).
    """
    info = tmp_path / ".info"
    stub = _write(info / "index.html", STUB_HTML)
    write_last_archive_attempt(info, failed=True, when=time.time())

    http_calls = {"n": 0}

    def fake_get(*_a: Any, **_k: Any) -> MagicMock:
        http_calls["n"] += 1
        raise TimeoutError("should not be called")

    session = MagicMock()
    session.get.side_effect = fake_get

    t0 = time.perf_counter()
    with OfflinePageArchiver(session=session) as archiver:
        path = archiver.ensure_offline_page(info, "3761838546")
    elapsed = time.perf_counter() - t0

    assert path == stub
    assert http_calls["n"] == 0
    assert elapsed < 0.5
    assert ensure_offline_page_nonblocking_probe(info) is True


def test_ensure_offline_page_nonblocking_probe_false_when_needs_network(
    tmp_path: Path,
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    assert ensure_offline_page_nonblocking_probe(info) is False
