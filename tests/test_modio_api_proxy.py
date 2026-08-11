"""Mod.io API client: proxy reuse, config key, timeout errors."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from services.modio_api import (
    DEFAULT_TIMEOUT,
    ModioAPIError,
    ModioClient,
    _format_connection_error,
)


def test_modio_client_uses_configured_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = "socks5://127.0.0.1:7897"
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: {"http": proxy, "https": proxy},
    )
    captured: dict[str, object] = {}

    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": []}
    session.get.return_value = response

    client = ModioClient(api_key="test-key-not-secret", session=session)
    assert client._proxies == {"http": proxy, "https": proxy}
    assert client.timeout == DEFAULT_TIMEOUT == (10.0, 30.0)

    client._get("games", params={"name_id": "anno-1800"})
    kwargs = session.get.call_args.kwargs
    captured["proxies"] = kwargs.get("proxies")
    captured["timeout"] = kwargs.get("timeout")
    assert captured["proxies"] == {"http": proxy, "https": proxy}
    assert captured["timeout"] == (10.0, 30.0)
    # API key must be in query, never logged by this assertion path.
    assert session.get.call_args.kwargs.get("params", {}).get("api_key") == (
        "test-key-not-secret"
    )


def test_modio_client_explicit_proxy_url_overrides_qsettings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: (
            {"http": proxy_url, "https": proxy_url}
            if proxy_url
            else {"http": "socks5://should-not-use:1", "https": "socks5://should-not-use:1"}
        ),
    )
    client = ModioClient(
        api_key="k",
        session=MagicMock(),
        proxy_url="http://127.0.0.1:8080",
    )
    assert client._proxies == {
        "http": "http://127.0.0.1:8080",
        "https": "http://127.0.0.1:8080",
    }


def test_modio_client_proxy_disabled_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {}
    session.get.return_value = response

    client = ModioClient(api_key="k", session=session)
    assert client._proxies is None
    client._get("games")
    assert "proxies" not in session.get.call_args.kwargs


def test_api_key_still_loaded_from_modio_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = tmp_path / "modio.json"
    cfg.write_text(
        '{"api_key": "from-config-file-key-32chars!!", "api_base_url": "https://api.mod.io/v1"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    for name in ("MODIO_API_KEY", "MOD_IO_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "services.modio_config.default_modio_config_path",
        lambda: cfg,
    )

    client = ModioClient(api_key="", session=MagicMock())
    assert client.require_api_key() == "from-config-file-key-32chars!!"


def test_timeout_exception_produces_readable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    session = MagicMock()
    session.get.side_effect = requests.exceptions.ConnectTimeout(
        "HTTPSConnectionPool(host='api.mod.io', port=443): "
        "Max retries exceeded with url: /v1/games?api_key=SECRETKEY123"
    )
    client = ModioClient(api_key="SECRETKEY123", session=session)
    with pytest.raises(ModioAPIError) as ei:
        client._get("games")
    msg = str(ei.value)
    assert "ConnectTimeout" in msg
    assert "api.mod.io" in msg
    assert "SECRETKEY123" not in msg
    assert "api_key" not in msg.lower() or "api_key=***" in msg.lower()


def test_format_connection_error_hostname_only() -> None:
    exc = requests.exceptions.ConnectTimeout("ignored detail with secret")
    msg = _format_connection_error(exc, hostname="api.mod.io")
    assert msg == "Mod.io API connection failed: ConnectTimeout api.mod.io"
