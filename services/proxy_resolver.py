"""Session-cached proxy resolution for Archive / network.

Priority:
1. User manual proxy (``proxy_mode=manual`` + Sync Center URL)
2. System auto-detect (Windows LAN / WinHTTP / env)
3. Direct

Auto mode ignores leftover ``network/proxy_url`` so a previous machine's
saved URL cannot override the current LAN proxy.

Default mode is auto-detect. Detected values are session-cached and
re-detected at startup. Last-detected fields in QSettings are informational
only — auto mode must not treat them as a forever default.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import getproxies

logger = logging.getLogger(__name__)

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_DIRECT = "direct"

SOURCE_MANUAL = "manual"
SOURCE_WINDOWS_LAN = "windows_lan"
SOURCE_WINHTTP = "winhttp"
SOURCE_ENV = "env"
SOURCE_URLLIB = "urllib"
SOURCE_DIRECT = "direct"
SOURCE_NONE = "none"

SETTINGS_ORG = "SteamModManager"
SETTINGS_APP = "WorkshopLibrary"
SETTING_PROXY_URL = "network/proxy_url"
SETTING_PROXY_MODE = "network/proxy_mode"
SETTING_LAST_SCHEME = "network/proxy_last_scheme"
SETTING_LAST_HOST = "network/proxy_last_host"
SETTING_LAST_PORT = "network/proxy_last_port"
SETTING_LAST_SOURCE = "network/proxy_last_source"
SETTING_LAST_DETECTED_AT = "network/proxy_last_detected_at"

SCHEME_HTTP = "http"
SCHEME_HTTPS = "https"
SCHEME_SOCKS5 = "socks5"
SCHEME_SOCKS4 = "socks4"
SCHEME_UNKNOWN = "unknown"


@dataclass
class ProxyEndpoint:
    source: str = SOURCE_NONE
    scheme: str = ""
    host: str = ""
    port: int = 0
    enabled: bool = False
    url: str = ""
    detected_by: str = ""
    raw: str = ""
    listening: bool | None = None
    protocol_probe: str = ""
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResolvedProxy:
    mode: str = MODE_AUTO
    source: str = SOURCE_DIRECT
    scheme: str = ""
    host: str = ""
    port: int = 0
    url: str = ""
    enabled: bool = False
    detected_by: str = ""
    cached: bool = False
    detected_at: str = ""
    candidates: list[ProxyEndpoint] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def proxies_dict(self) -> dict[str, str] | None:
        if not self.url:
            return None
        return {"http": self.url, "https": self.url}


_cache_lock = threading.Lock()
_session_cache: ResolvedProxy | None = None

SystemDetectFn = Callable[[], list[ProxyEndpoint]]
SettingsReadFn = Callable[[], dict[str, str]]
SettingsWriteFn = Callable[[dict[str, str]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_proxy_url(raw: str) -> ProxyEndpoint:
    text = str(raw or "").strip()
    if not text:
        return ProxyEndpoint()
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    host = str(parsed.hostname or "").strip()
    try:
        port = int(parsed.port or 0)
    except (TypeError, ValueError):
        port = 0
    scheme = str(parsed.scheme or SCHEME_HTTP).lower()
    if scheme == "socks":
        scheme = SCHEME_SOCKS5
    url = f"{scheme}://{host}:{port}" if host and port else ""
    return ProxyEndpoint(
        source=SOURCE_MANUAL,
        scheme=scheme,
        host=host,
        port=port,
        enabled=bool(url),
        url=url,
        detected_by="parse_proxy_url",
        raw=str(raw or "").strip(),
    )


def _parse_windows_proxy_server(raw: str) -> list[ProxyEndpoint]:
    """Parse IE/LAN ``ProxyServer`` (``host:port`` or ``http=host:port;socks=...``)."""
    text = str(raw or "").strip()
    if not text:
        return []
    out: list[ProxyEndpoint] = []
    if "=" not in text and ";" not in text:
        ep = parse_proxy_url(text)
        ep.source = SOURCE_WINDOWS_LAN
        ep.detected_by = "winreg.ProxyServer"
        ep.raw = text
        if ep.url:
            out.append(ep)
        return out
    for part in text.split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        scheme = SCHEME_HTTP
        rest = chunk
        if "=" in chunk:
            key, rest = chunk.split("=", 1)
            key = key.strip().lower()
            rest = rest.strip()
            if key in ("socks", "socks5"):
                scheme = SCHEME_SOCKS5
            elif key in ("socks4",):
                scheme = SCHEME_SOCKS4
            elif key in ("https",):
                scheme = SCHEME_HTTPS
            else:
                scheme = SCHEME_HTTP
        if "://" not in rest:
            rest = f"{scheme}://{rest}"
        ep = parse_proxy_url(rest)
        ep.source = SOURCE_WINDOWS_LAN
        ep.scheme = scheme if ep.scheme else scheme
        ep.detected_by = "winreg.ProxyServer"
        ep.raw = chunk
        if ep.host and ep.port:
            ep.url = f"{ep.scheme}://{ep.host}:{ep.port}"
            ep.enabled = True
            out.append(ep)
    return out


def detect_windows_lan_proxy() -> list[ProxyEndpoint]:
    try:
        import winreg
    except ImportError:
        return []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
    except OSError as exc:
        logger.debug("winreg Internet Settings unavailable: %s", exc)
        return []
    enabled = 0
    server = ""
    pac = ""
    try:
        try:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
        except OSError:
            enabled = 0
        try:
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
        except OSError:
            server = ""
        try:
            pac = str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "").strip()
        except OSError:
            pac = ""
    finally:
        try:
            key.Close()
        except Exception:  # noqa: BLE001
            pass
    endpoints = _parse_windows_proxy_server(server) if enabled and server else []
    if pac:
        endpoints.append(
            ProxyEndpoint(
                source=SOURCE_WINDOWS_LAN,
                scheme="pac",
                enabled=True,
                detected_by="winreg.AutoConfigURL",
                raw=pac,
                evidence=f"PAC URL present: {pac}",
            )
        )
    for ep in endpoints:
        ep.enabled = bool(enabled) and ep.enabled
        if not ep.evidence:
            ep.evidence = f"ProxyEnable={enabled} ProxyServer={server!r}"
    return endpoints


def detect_env_proxies() -> list[ProxyEndpoint]:
    out: list[ProxyEndpoint] = []
    for key in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        ep = parse_proxy_url(raw)
        ep.source = SOURCE_ENV
        ep.detected_by = f"env.{key}"
        ep.raw = raw
        if ep.url:
            out.append(ep)
            break
    return out


def detect_urllib_proxies() -> list[ProxyEndpoint]:
    out: list[ProxyEndpoint] = []
    try:
        mapping = getproxies() or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("urllib.getproxies failed: %s", exc)
        return out
    for key in ("https", "http", "all"):
        raw = str(mapping.get(key) or "").strip()
        if not raw:
            continue
        ep = parse_proxy_url(raw)
        ep.source = SOURCE_URLLIB
        ep.detected_by = f"urllib.getproxies.{key}"
        ep.raw = raw
        if ep.url:
            out.append(ep)
            break
    return out


def detect_winhttp_proxy() -> list[ProxyEndpoint]:
    """Best-effort WinHTTP proxy via netsh. Empty when unavailable."""
    try:
        import subprocess
    except ImportError:
        return []
    try:
        completed = subprocess.run(
            ["netsh", "winhttp", "show", "proxy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    endpoints: list[ProxyEndpoint] = []
    for line in text.splitlines():
        lowered = line.lower()
        if "direct access" in lowered:
            continue
        if "proxy server" in lowered and ":" in line:
            raw = line.split(":", 1)[-1].strip()
            if not raw or raw.lower() in ("none", "(none)"):
                continue
            for ep in _parse_windows_proxy_server(raw):
                ep.source = SOURCE_WINHTTP
                ep.detected_by = "netsh winhttp show proxy"
                endpoints.append(ep)
    return endpoints


def tcp_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_proxy_protocol(host: str, port: int, timeout: float = 1.5) -> str:
    """Identify HTTP vs SOCKS without assuming a port number."""
    if not host or not port:
        return SCHEME_UNKNOWN
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError:
        return SCHEME_UNKNOWN
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        data = sock.recv(2)
        if data[:1] == b"\x05":
            return SCHEME_SOCKS5
        if data[:1] == b"\x04":
            return SCHEME_SOCKS4
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError:
        return SCHEME_UNKNOWN
    try:
        sock.settimeout(timeout)
        sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        data = sock.recv(32)
        if data.startswith(b"HTTP/"):
            return SCHEME_HTTP
    except OSError:
        return SCHEME_UNKNOWN
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return SCHEME_UNKNOWN


def default_system_detect() -> list[ProxyEndpoint]:
    seen: set[tuple[str, str, int]] = set()
    ordered: list[ProxyEndpoint] = []
    for group in (
        detect_windows_lan_proxy(),
        detect_winhttp_proxy(),
        detect_env_proxies(),
        detect_urllib_proxies(),
    ):
        for ep in group:
            key = (ep.scheme, ep.host, ep.port)
            if ep.host and ep.port and key in seen:
                continue
            if ep.host and ep.port:
                seen.add(key)
            ordered.append(ep)
    return ordered


def _read_qsettings() -> dict[str, str]:
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return {}
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    keys = (
        SETTING_PROXY_URL,
        SETTING_PROXY_MODE,
        SETTING_LAST_SCHEME,
        SETTING_LAST_HOST,
        SETTING_LAST_PORT,
        SETTING_LAST_SOURCE,
        SETTING_LAST_DETECTED_AT,
    )
    out: dict[str, str] = {}
    for key in keys:
        out[key] = str(settings.value(key, "", str) or "").strip()
    return out


def _write_qsettings(values: dict[str, str]) -> None:
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    for key, value in values.items():
        settings.setValue(key, value)


def _pick_system_endpoint(candidates: list[ProxyEndpoint]) -> ProxyEndpoint | None:
    usable = [
        ep
        for ep in candidates
        if ep.enabled and ep.host and ep.port and ep.scheme != "pac"
    ]
    if not usable:
        return None
    for source in (SOURCE_WINDOWS_LAN, SOURCE_WINHTTP, SOURCE_ENV, SOURCE_URLLIB):
        for ep in usable:
            if ep.source == source:
                return ep
    return usable[0]


def resolve_proxy(
    *,
    refresh: bool = False,
    detect_fn: SystemDetectFn | None = None,
    settings_read: SettingsReadFn | None = None,
    settings_write: SettingsWriteFn | None = None,
    probe: bool = False,
) -> ResolvedProxy:
    """Resolve the session proxy. ``refresh=True`` drops the cache (startup)."""
    global _session_cache
    with _cache_lock:
        if not refresh and _session_cache is not None:
            cached = ResolvedProxy(**{**_session_cache.as_dict(), "cached": True})
            cached.candidates = list(_session_cache.candidates)
            return cached

        stored = (settings_read or _read_qsettings)()
        mode = str(stored.get(SETTING_PROXY_MODE) or MODE_AUTO).strip().lower() or MODE_AUTO
        if mode not in (MODE_AUTO, MODE_MANUAL, MODE_DIRECT):
            mode = MODE_AUTO
        manual_url = str(stored.get(SETTING_PROXY_URL) or "").strip()

        if mode == MODE_DIRECT:
            resolved = ResolvedProxy(
                mode=MODE_DIRECT,
                source=SOURCE_DIRECT,
                detected_by="proxy_mode=direct",
                detected_at=_utc_now(),
            )
            _session_cache = resolved
            return resolved

        # Manual wins only when the user explicitly chose manual mode.
        # Leftover ``network/proxy_url`` under auto must NOT skip system detect
        # (old PC port in QSettings would otherwise hide the current LAN proxy).
        if mode == MODE_MANUAL:
            ep = parse_proxy_url(manual_url)
            resolved = ResolvedProxy(
                mode=MODE_MANUAL,
                source=SOURCE_MANUAL,
                scheme=ep.scheme,
                host=ep.host,
                port=ep.port,
                url=ep.url,
                enabled=bool(ep.url),
                detected_by="qsettings.network/proxy_url",
                detected_at=_utc_now(),
                candidates=[ep],
            )
            if ep.url:
                _session_cache = resolved
                return resolved
            mode = MODE_AUTO

        try:
            candidates = list((detect_fn or default_system_detect)())
        except Exception as exc:  # noqa: BLE001
            logger.warning("system proxy detect failed; using direct: %s", exc)
            candidates = []
        if probe:
            for ep in candidates:
                if ep.host and ep.port:
                    ep.listening = tcp_listening(ep.host, ep.port)
                    if ep.listening:
                        probed = probe_proxy_protocol(ep.host, ep.port)
                        ep.protocol_probe = probed
                        if probed in (SCHEME_HTTP, SCHEME_HTTPS, SCHEME_SOCKS5, SCHEME_SOCKS4):
                            ep.scheme = probed
                            ep.url = f"{ep.scheme}://{ep.host}:{ep.port}"
        chosen = _pick_system_endpoint(candidates)
        if chosen is None:
            resolved = ResolvedProxy(
                mode=MODE_AUTO,
                source=SOURCE_DIRECT,
                detected_by="system_auto_detect_empty",
                detected_at=_utc_now(),
                candidates=candidates,
            )
        else:
            resolved = ResolvedProxy(
                mode=MODE_AUTO,
                source=chosen.source,
                scheme=chosen.scheme,
                host=chosen.host,
                port=chosen.port,
                url=chosen.url,
                enabled=True,
                detected_by=chosen.detected_by,
                detected_at=_utc_now(),
                candidates=candidates,
            )
            writer = settings_write or _write_qsettings
            writer(
                {
                    SETTING_LAST_SCHEME: resolved.scheme,
                    SETTING_LAST_HOST: resolved.host,
                    SETTING_LAST_PORT: str(resolved.port or ""),
                    SETTING_LAST_SOURCE: resolved.source,
                    SETTING_LAST_DETECTED_AT: resolved.detected_at,
                }
            )
        _session_cache = resolved
        return resolved


def refresh_system_proxy(
    *,
    detect_fn: SystemDetectFn | None = None,
    settings_read: SettingsReadFn | None = None,
    settings_write: SettingsWriteFn | None = None,
    probe: bool = False,
) -> ResolvedProxy:
    """Startup / explicit re-detect. Never mints identity."""
    return resolve_proxy(
        refresh=True,
        detect_fn=detect_fn,
        settings_read=settings_read,
        settings_write=settings_write,
        probe=probe,
    )


def clear_proxy_cache() -> None:
    global _session_cache
    with _cache_lock:
        _session_cache = None


def resolved_proxy_url(*, refresh: bool = False) -> str | None:
    resolved = resolve_proxy(refresh=refresh)
    return resolved.url or None
