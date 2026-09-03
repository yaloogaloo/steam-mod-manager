"""Proxy Resolution Contract — no hardcoded local ports."""

from __future__ import annotations

from pathlib import Path

from services.proxy_resolver import (
    MODE_AUTO,
    MODE_DIRECT,
    MODE_MANUAL,
    SETTING_PROXY_MODE,
    SETTING_PROXY_URL,
    SOURCE_ENV,
    SOURCE_MANUAL,
    SOURCE_WINDOWS_LAN,
    ProxyEndpoint,
    clear_proxy_cache,
    parse_proxy_url,
    refresh_system_proxy,
    resolve_proxy,
)
from services.archive import archive_proxies_dict, _get_archive_proxy
from services.archive_observability import NETWORK_FAILURE, classify_archive_error


def _settings(url: str = "", mode: str = MODE_AUTO) -> dict[str, str]:
    return {SETTING_PROXY_URL: url, SETTING_PROXY_MODE: mode}


def test_system_proxy_detected() -> None:
    clear_proxy_cache()
    written: dict[str, str] = {}

    def detect() -> list[ProxyEndpoint]:
        return [
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="http",
                host="127.0.0.1",
                port=5555,
                enabled=True,
                url="http://127.0.0.1:5555",
                detected_by="test.lan",
            )
        ]

    resolved = resolve_proxy(
        refresh=True,
        detect_fn=detect,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=written.update,
    )
    assert resolved.url == "http://127.0.0.1:5555"
    assert resolved.source == SOURCE_WINDOWS_LAN
    assert resolved.detected_by == "test.lan"
    assert written.get("network/proxy_last_host") == "127.0.0.1"


def test_http_proxy_detected() -> None:
    ep = parse_proxy_url("http://127.0.0.1:5555")
    assert ep.scheme == "http"
    assert ep.port == 5555
    assert "socks" not in ep.scheme


def test_socks_proxy_detected() -> None:
    ep = parse_proxy_url("socks5://127.0.0.1:5556")
    assert ep.scheme == "socks5"
    assert ep.port == 5556


def test_no_proxy() -> None:
    clear_proxy_cache()
    resolved = resolve_proxy(
        refresh=True,
        detect_fn=list,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    assert resolved.url == ""
    assert resolved.source in ("direct", "none") or not resolved.enabled


def test_manual_overrides_system() -> None:
    clear_proxy_cache()

    def detect() -> list[ProxyEndpoint]:
        return [
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="http",
                host="10.0.0.1",
                port=1,
                enabled=True,
                url="http://10.0.0.1:1",
                detected_by="test.lan",
            )
        ]

    resolved = resolve_proxy(
        refresh=True,
        detect_fn=detect,
        settings_read=lambda: _settings("socks5://127.0.0.1:5556", MODE_MANUAL),
        settings_write=lambda _v: None,
    )
    assert resolved.source == SOURCE_MANUAL
    assert resolved.url == "socks5://127.0.0.1:5556"


def test_auto_mode_ignores_leftover_qsettings_url() -> None:
    clear_proxy_cache()

    def detect() -> list[ProxyEndpoint]:
        return [
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="http",
                host="127.0.0.1",
                port=5555,
                enabled=True,
                url="http://127.0.0.1:5555",
                detected_by="test.lan",
            )
        ]

    resolved = resolve_proxy(
        refresh=True,
        detect_fn=detect,
        settings_read=lambda: _settings("http://127.0.0.1:1", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    assert resolved.source == SOURCE_WINDOWS_LAN
    assert resolved.url == "http://127.0.0.1:5555"
    assert "127.0.0.1:1" not in resolved.url


def test_session_cache() -> None:
    clear_proxy_cache()
    calls = {"n": 0}

    def detect() -> list[ProxyEndpoint]:
        calls["n"] += 1
        return [
            ProxyEndpoint(
                source=SOURCE_ENV,
                scheme="http",
                host="127.0.0.1",
                port=9,
                enabled=True,
                url="http://127.0.0.1:9",
                detected_by="test.env",
            )
        ]

    a = resolve_proxy(
        refresh=True,
        detect_fn=detect,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    b = resolve_proxy(
        refresh=False,
        detect_fn=detect,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    assert a.url == b.url
    assert b.cached is True
    assert calls["n"] == 1


def test_startup_refreshes_cached_system_proxy() -> None:
    clear_proxy_cache()
    current = {"port": 1}

    def detect() -> list[ProxyEndpoint]:
        port = current["port"]
        return [
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="http",
                host="127.0.0.1",
                port=port,
                enabled=True,
                url=f"http://127.0.0.1:{port}",
                detected_by="test.lan",
            )
        ]

    first = resolve_proxy(
        refresh=True,
        detect_fn=detect,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    current["port"] = 2
    second = refresh_system_proxy(
        detect_fn=detect,
        settings_read=lambda: _settings("", MODE_AUTO),
        settings_write=lambda _v: None,
    )
    assert first.port == 1
    assert second.port == 2
    assert second.cached is False


def test_old_ports_not_hardcoded_in_resolver_or_archive() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "services/proxy_resolver.py",
        "services/archive.py",
        "ui/main_window.py",
        "ui/sync_view.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "7890" not in text, rel
        assert "7897" not in text, rel
        assert "12450" not in text, rel


def test_archive_receives_resolved_proxy(monkeypatch) -> None:
    from services import archive as archive_mod
    from services import proxy_resolver as pr

    pr.clear_proxy_cache()
    monkeypatch.setattr(pr, "resolved_proxy_url", lambda refresh=False: "http://127.0.0.1:5555")
    assert archive_mod._get_archive_proxy() == "http://127.0.0.1:5555"
    assert archive_proxies_dict() == {
        "http": "http://127.0.0.1:5555",
        "https": "http://127.0.0.1:5555",
    }


def test_direct_mode() -> None:
    clear_proxy_cache()
    resolved = resolve_proxy(
        refresh=True,
        detect_fn=lambda: [
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="http",
                host="127.0.0.1",
                port=8,
                enabled=True,
                url="http://127.0.0.1:8",
                detected_by="x",
            )
        ],
        settings_read=lambda: _settings("", MODE_DIRECT),
        settings_write=lambda _v: None,
    )
    assert resolved.url == ""
    assert resolved.mode == MODE_DIRECT


def test_curl_28_classification_unchanged() -> None:
    assert (
        classify_archive_error("curl: (28) Connection timed out after 15003 milliseconds")
        == NETWORK_FAILURE
    )
