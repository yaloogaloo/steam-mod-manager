"""Steam Web API must reuse Sync Center / Archive proxy (not force NO_PROXY)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.steam_api import (
    SteamWorkshopClient,
    _format_steam_network_error,
    _resolve_proxies,
    _steam_result_fetch_error,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "steam_proxy.db")
    yield manager
    DatabaseManager.reset_instance()


def test_resolve_proxies_uses_archive_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = "socks5://127.0.0.1:7897"
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: {"http": proxy, "https": proxy},
    )
    assert _resolve_proxies() == {"http": proxy, "https": proxy}


def test_resolve_proxies_explicit_url_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: {"http": "socks5://ignored", "https": "socks5://ignored"},
    )
    assert _resolve_proxies(proxy_url="http://127.0.0.1:8080") == {
        "http": "http://127.0.0.1:8080",
        "https": "http://127.0.0.1:8080",
    }
    assert _resolve_proxies(proxy_url="") is None


def test_steam_client_uses_configured_proxy(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = "socks5://127.0.0.1:7897"
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: {"http": proxy, "https": proxy},
    )
    captured: dict[str, object] = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": {
                    "publishedfiledetails": [
                        {
                            "publishedfileid": "42",
                            "result": 1,
                            "title": "ViaProxy",
                            "description": "",
                            "file_size": 1,
                            "time_created": 1,
                            "time_updated": 1,
                        }
                    ]
                }
            }

    real_request = requests.Session.request

    def fake_session_request(self, method, url, **kwargs):
        # Prefer per-call proxies; else session-level proxies (owned client).
        proxies = kwargs.get("proxies")
        if proxies is None:
            proxies = dict(self.proxies) if self.proxies else None
        captured["proxies"] = proxies
        captured["url"] = url
        if "GetPublishedFileDetails" in str(url):
            return FakeResp()
        return real_request(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_session_request)

    client = SteamWorkshopClient(db=db, request_interval=0)
    try:
        assert client._proxies == {"http": proxy, "https": proxy}
        assert dict(client.session.proxies) == {"http": proxy, "https": proxy}
        out = client.refresh_details(["42"])
    finally:
        client.close()

    assert captured["proxies"] == {"http": proxy, "https": proxy}
    assert out[0].title == "ViaProxy"
    assert not out[0].fetch_error


def test_steam_client_direct_when_proxy_unset(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    captured: dict[str, object] = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": {"publishedfiledetails": []}}

    def fake_request(method, url, **kwargs):
        captured["proxies"] = kwargs.get("proxies")
        return FakeResp()

    client = SteamWorkshopClient(db=db, request_interval=0, proxies=None, proxy_url="")
    try:
        assert client._proxies is None
        monkeypatch.setattr(client, "_request", fake_request)
        out = client._request_published_file_details(["99"])
    finally:
        client.close()

    assert "proxies" not in captured or captured["proxies"] is None
    assert out[0].fetch_error == "Steam API returned empty publishedfiledetails"


def test_network_error_classified_not_empty_payload() -> None:
    exc = requests.exceptions.ConnectTimeout("connect timed out")
    msg = _format_steam_network_error(
        exc,
        url="https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        proxy_enabled=False,
    )
    assert "network failure" in msg
    assert "ConnectTimeout" in msg
    assert "proxy=direct" in msg
    assert "empty" not in msg.lower()


def test_result_codes_distinguish_not_found() -> None:
    assert "not found" in _steam_result_fetch_error(9, "1").lower()
    assert "access denied" in _steam_result_fetch_error(15, "1").lower()
    assert "empty result" in _steam_result_fetch_error(0, "1").lower()


def test_not_found_is_not_retryable_network(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.steam_api import _batch_needs_network_retry

    batch = [
        ModMetadata(
            published_file_id="1",
            fetch_error=_steam_result_fetch_error(9, "1"),
        )
    ]
    assert _batch_needs_network_retry(batch) is False
