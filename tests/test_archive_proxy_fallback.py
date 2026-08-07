"""Tests: optional proxy with automatic direct fallback."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from services import archive as archive_mod
from services.archive import OfflinePageArchiver, SteamArchiveRateLimiter


PROXY = "socks5://127.0.0.1:7897"
PROXY_DICT = {"http": PROXY, "https": PROXY}
URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546"


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
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    lim = SteamArchiveRateLimiter(0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)


def test_proxy_success_skips_direct(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[dict[str, Any] | None] = []
    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        calls.append(kwargs.get("proxies"))
        return _ok_response()

    session.get.side_effect = fake_get

    with caplog.at_level("INFO", logger=archive_mod.logger.name):
        with OfflinePageArchiver(session=session, proxies=PROXY_DICT) as archiver:
            archiver._http_get(URL)

    assert len(calls) == 1
    assert calls[0] == PROXY_DICT
    assert "[ARCHIVE] proxy success" in caplog.text
    assert "[ARCHIVE] direct success" not in caplog.text
    assert "fallback direct" not in caplog.text


def test_proxy_failure_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[dict[str, Any] | None] = []
    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        proxies = kwargs.get("proxies")
        calls.append(proxies)
        if proxies:
            raise ConnectionError("proxy down")
        return _ok_response()

    session.get.side_effect = fake_get

    with caplog.at_level("INFO", logger=archive_mod.logger.name):
        with OfflinePageArchiver(session=session, proxies=PROXY_DICT) as archiver:
            resp = archiver._http_get(URL)

    assert resp is not None
    assert len(calls) == 2
    assert calls[0] == PROXY_DICT
    assert calls[1] is None
    assert "[ARCHIVE] proxy failed" in caplog.text
    assert "fallback direct" in caplog.text
    assert "[ARCHIVE] direct success" in caplog.text


def test_no_proxy_uses_direct_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[dict[str, Any] | None] = []
    session = MagicMock()
    session.cookies = {}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        calls.append(kwargs.get("proxies"))
        return _ok_response()

    session.get.side_effect = fake_get

    with caplog.at_level("INFO", logger=archive_mod.logger.name):
        with OfflinePageArchiver(session=session) as archiver:
            assert archiver._proxies is None
            archiver._http_get(URL)

    assert calls == [None]
    assert "[ARCHIVE] direct success" in caplog.text
    assert "[ARCHIVE] proxy success" not in caplog.text
    assert "[ARCHIVE] proxy failed" not in caplog.text
