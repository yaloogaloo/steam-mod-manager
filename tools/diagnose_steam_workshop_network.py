#!/usr/bin/env python3
"""Temporary Steam Workshop network diagnosis (read-only).

Does NOT import or mutate Archive business logic.
Mirrors production curl_cffi knobs observed in services/archive.py:
  - impersonate = chrome131
  - timeout = 15
  - HTTP version = not set (curl_cffi default)
  - proxies = {http, https} from proxy_resolver (same contract as Archive)

Usage:
  .venv/Scripts/python.exe tools/diagnose_steam_workshop_network.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

# Keep imports out of services.archive — only curl_cffi + proxy resolution.
from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Production constants mirrored from services/archive.py (do not import archive).
WORKSHOP_URL = (
    "https://steamcommunity.com/sharedfiles/filedetails/?id=2465378070"
)
IMPERSONATE = "chrome131"
TIMEOUT = 15.0
HTTP_VERSION = "(not set — production does not pass http_version)"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://steamcommunity.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Connection": "keep-alive",
}


def redact_proxy(url: str) -> str:
    """Mask userinfo / tokens in a proxy URL for safe logging."""
    text = str(url or "").strip()
    if not text:
        return "(none)"
    parsed = urlparse(text)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"***:***@{host}{port}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, "")
        )
    # Query tokens
    if parsed.query and re.search(
        r"(token|secret|key|auth|password)=", parsed.query, re.I
    ):
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", "REDACTED", "")
        )
    return text


def resolve_production_proxy() -> tuple[str | None, dict[str, Any]]:
    """Same contract Archive uses: services.proxy_resolver.resolved_proxy_url()."""
    meta: dict[str, Any] = {
        "source": "none",
        "scheme": "",
        "host": "",
        "port": "",
        "mode": "",
    }
    try:
        from services.proxy_resolver import resolve_proxy, refresh_system_proxy

        refresh_system_proxy()
        resolved = resolve_proxy()
        meta = {
            "source": resolved.source or "none",
            "scheme": resolved.scheme or "",
            "host": resolved.host or "",
            "port": resolved.port or "",
            "mode": resolved.mode or "",
            "enabled": bool(resolved.enabled),
        }
        url = (resolved.url or "").strip() or None
        return url, meta
    except Exception as exc:  # noqa: BLE001
        meta["error"] = str(exc)
        return None, meta


def _curl_error_fields(exc: BaseException) -> tuple[str, str]:
    code = str(getattr(exc, "code", "") or "")
    msg = str(exc)
    # curl_cffi often embeds "curl: (N)" in the message.
    match = re.search(r"curl:\s*\((\d+)\)", msg, re.I)
    if match and not code:
        code = match.group(1)
    if not code:
        code = type(exc).__name__
    return code, msg


def run_get(
    *,
    label: str,
    proxy_url: str | None,
) -> dict[str, Any]:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    out: dict[str, Any] = {
        "label": label,
        "url": WORKSHOP_URL,
        "proxy": redact_proxy(proxy_url or ""),
        "proxy_raw_scheme": urlparse(proxy_url).scheme if proxy_url else "",
        "impersonate": IMPERSONATE,
        "timeout": TIMEOUT,
        "http_version_requested": HTTP_VERSION,
        "elapsed": None,
        "http_status": None,
        "http_version_actual": None,
        "primary_ip": None,
        "response_size": None,
        "exception": None,
        "curl_error_code": None,
        "curl_error_message": None,
        "pass": False,
    }

    t0 = time.perf_counter()
    try:
        # One-shot request (no Session) — isolates transport from Session reuse.
        resp = curl_requests.get(
            WORKSHOP_URL,
            timeout=TIMEOUT,
            impersonate=IMPERSONATE,
            headers=dict(_BROWSER_HEADERS),
            allow_redirects=True,
            proxies=proxies,
        )
        elapsed = time.perf_counter() - t0
        out["elapsed"] = round(elapsed, 3)
        out["http_status"] = int(getattr(resp, "status_code", 0) or 0)
        body = getattr(resp, "content", b"") or b""
        out["response_size"] = len(body)

        # Best-effort HTTP version / primary IP from curl_cffi response.
        http_v = getattr(resp, "http_version", None)
        if http_v is None:
            http_v = getattr(resp, "version", None)
        out["http_version_actual"] = (
            str(http_v) if http_v is not None else "(unavailable)"
        )

        primary_ip = None
        for attr in ("primary_ip", "local_ip", "server_ip"):
            val = getattr(resp, attr, None)
            if val:
                primary_ip = str(val)
                break
        # curl_cffi Response may expose .curl / .request
        if primary_ip is None:
            curl_obj = getattr(resp, "curl", None)
            if curl_obj is not None:
                for attr in ("primary_ip", "LOCAL_IP", "PRIMARY_IP"):
                    val = getattr(curl_obj, attr, None)
                    if callable(val):
                        try:
                            val = val()
                        except Exception:  # noqa: BLE001
                            val = None
                    if val:
                        primary_ip = str(val)
                        break
        out["primary_ip"] = primary_ip or "(unavailable)"
        out["pass"] = 200 <= int(out["http_status"]) < 400
        return out
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - t0
        out["elapsed"] = round(elapsed, 3)
        code, msg = _curl_error_fields(exc)
        out["exception"] = type(exc).__name__
        out["curl_error_code"] = code
        out["curl_error_message"] = msg
        out["pass"] = False
        return out


def print_result(result: dict[str, Any]) -> None:
    print()
    print("=" * 60)
    print(f"TEST: {result['label']}")
    print("=" * 60)
    print(f"URL: {result['url']}")
    print(f"Proxy: {result['proxy']}")
    print(f"Impersonate: {result['impersonate']}")
    print(f"Timeout: {result['timeout']}")
    print(f"HTTP version: {result['http_version_requested']}")
    print()
    print(f"Elapsed: {result['elapsed']}")
    print(f"HTTP status: {result['http_status']}")
    print(f"HTTP version actually used: {result['http_version_actual']}")
    print(f"Primary IP: {result['primary_ip']}")
    print(f"Response size: {result['response_size']}")
    print()
    print(f"Exception: {result['exception']}")
    print(f"curl error code: {result['curl_error_code']}")
    print(f"curl error message: {result['curl_error_message']}")
    print(f"RESULT: {'PASS' if result['pass'] else 'FAIL'}")


def main() -> int:
    import curl_cffi

    print("STEAM WORKSHOP NETWORK DIAGNOSIS (temp script)")
    print(f"curl_cffi version: {curl_cffi.__version__}")
    print(f"Target: {WORKSHOP_URL}")
    print(f"Impersonate (production mirror): {IMPERSONATE}")
    print(f"Timeout (production mirror): {TIMEOUT}")
    print(f"HTTP version (production): {HTTP_VERSION}")

    proxy_url, meta = resolve_production_proxy()
    print()
    print("Resolved production proxy (redacted):")
    print(f"  Proxy: {redact_proxy(proxy_url or '')}")
    print(f"  Mode: {meta.get('mode')}")
    print(f"  Source: {meta.get('source')}")
    print(f"  Scheme: {meta.get('scheme')}")
    print(f"  Host: {meta.get('host')}")
    print(f"  Port: {meta.get('port')}")
    if proxy_url:
        scheme = (urlparse(proxy_url).scheme or "").lower()
        print(f"  URL scheme literal: {scheme}")
        if scheme in {"socks5", "socks5h", "socks"}:
            print(
                "  Test C note: production builds socks5 (not socks5h) "
                "via proxy_resolver.parse_proxy_url / Windows LAN detect."
            )

    # Test A — production proxy + impersonate
    a = run_get(label="A — production configuration", proxy_url=proxy_url)
    print_result(a)

    # Test B — direct (no proxy)
    b = run_get(label="B — direct connection (no proxy)", proxy_url=None)
    print_result(b)

    # Test C — proxy connectivity (same as A when proxy present)
    if proxy_url:
        c = dict(a)
        c["label"] = "C — proxy connectivity (same as A)"
        print_result(c)
    else:
        print()
        print("=" * 60)
        print("TEST: C — proxy connectivity")
        print("=" * 60)
        print("RESULT: NOT AVAILABLE (no proxy resolved)")

    print()
    print("SUMMARY")
    print(f"  Test A: {'PASS' if a['pass'] else 'FAIL'}")
    print(f"  Test B: {'PASS' if b['pass'] else 'FAIL'}")
    if proxy_url:
        print(f"  Test C: {'PASS' if a['pass'] else 'FAIL'}")
    else:
        print("  Test C: NOT AVAILABLE")
    return 0 if a["pass"] or b["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
