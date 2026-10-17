"""HTML Archive proxy policy: configured proxy must not fall back to direct."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from curl_cffi.requests.exceptions import CurlError

from services import archive as archive_mod
from services.archive import (
    DEFAULT_TIMEOUT,
    IMPERSONATE,
    OfflinePageArchiver,
    SteamArchiveRateLimiter,
)
from services.archive_observability import NETWORK_FAILURE, classify_archive_error


PROXY = "http://127.0.0.1:12450"
PROXY_DICT = {"http": PROXY, "https": PROXY}
URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=3636741836"


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


def test_html_request_kwargs_match_production() -> None:
    """Production Steam HTML GET knobs: chrome131, timeout 15, no verify override."""
    with OfflinePageArchiver(session=MagicMock(), proxies=PROXY_DICT) as archiver:
        kwargs = archiver._request_kwargs()
    assert kwargs["impersonate"] == IMPERSONATE == "chrome131"
    assert kwargs["timeout"] == DEFAULT_TIMEOUT == 15
    assert "verify" not in kwargs
    assert "http_version" not in kwargs
    assert "trust_env" not in kwargs


def test_proxy_success_skips_direct(
    caplog: pytest.LogCaptureFixture,
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


def test_proxy_failure_does_not_fall_back_to_direct(
    caplog: pytest.LogCaptureFixture,
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
            with pytest.raises(ConnectionError) as caught:
                archiver._http_get(URL)

    assert len(calls) == 1
    assert calls[0] == PROXY_DICT
    text = str(caught.value)
    assert "direct_fallback=not_executed" in text
    assert PROXY in text
    assert URL in text
    assert "proxy down" in text
    assert "[ARCHIVE] proxy failed" in caplog.text
    assert "client=curl_cffi.Session" in caplog.text
    assert "impersonate=chrome131" in caplog.text
    assert "verify=default" in caplog.text
    assert "fallback direct" not in caplog.text
    assert "[ARCHIVE] direct success" not in caplog.text


def test_proxy_tls_error_preserves_curl_35() -> None:
    session = MagicMock()
    session.cookies = {}
    original = CurlError(
        "curl: (35) BoringSSL SSL_connect: Connection closed abruptly "
        "(SSL_ERROR_SYSCALL)"
    )

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        raise original

    session.get.side_effect = fake_get

    with OfflinePageArchiver(session=session, proxies=PROXY_DICT) as archiver:
        with pytest.raises(CurlError) as caught:
            archiver._http_get(URL)

    text = str(caught.value)
    assert "direct_fallback=not_executed" in text
    assert "curl: (35)" in text
    assert "SSL_ERROR_SYSCALL" in text
    assert classify_archive_error(caught.value, proxy=PROXY) == NETWORK_FAILURE


def test_no_proxy_uses_direct_only(
    caplog: pytest.LogCaptureFixture,
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
