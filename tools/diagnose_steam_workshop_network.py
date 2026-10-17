#!/usr/bin/env python3
"""Temporary Steam Workshop network diagnosis (read-only).

Does NOT import or mutate Archive business logic.
Mirrors production curl_cffi knobs observed in services/archive.py:
  - impersonate = chrome131
  - timeout = 15
  - verify = not set (curl_cffi default)
  - HTTP version = not set (curl_cffi default)
  - proxies = {http, https} from proxy_resolver (same contract as Archive)
  - client = curl_cffi Session (HTML path) and one-shot get (isolation)

Usage:
  .venv/Scripts/python.exe tools/diagnose_steam_workshop_network.py
  .venv/Scripts/python.exe tools/diagnose_steam_workshop_network.py --url URL
"""

from __future__ import annotations

import argparse
import re
import socket
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
DEFAULT_WORKSHOP_URL = (
    "https://steamcommunity.com/sharedfiles/filedetails/?id=3636741836"
)
IMPERSONATE = "chrome131"
TIMEOUT = 15.0
HTTP_VERSION = "(not set — production does not pass http_version)"
VERIFY = "(not set — production does not pass verify; curl_cffi default)"

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
    match = re.search(r"curl:\s*\((\d+)\)", msg, re.I)
    if match and not code:
        code = match.group(1)
    if not code:
        code = type(exc).__name__
    return code, msg


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {
                "ok": True,
                "elapsed": round(time.perf_counter() - t0, 3),
                "error": None,
            }
    except OSError as exc:
        return {
            "ok": False,
            "elapsed": round(time.perf_counter() - t0, 3),
            "error": str(exc),
        }


def probe_http_connect(
    proxy_url: str,
    target_host: str = "steamcommunity.com",
    target_port: int = 443,
    timeout: float = 8.0,
) -> dict[str, Any]:
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    port = int(parsed.port or 0)
    out: dict[str, Any] = {
        "ok": False,
        "elapsed": None,
        "status_line": None,
        "error": None,
    }
    if not host or not port:
        out["error"] = "proxy host/port missing"
        return out
    request = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        "\r\n"
    ).encode("ascii")
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        out["elapsed"] = round(time.perf_counter() - t0, 3)
        out["error"] = str(exc)
        return out
    try:
        sock.settimeout(timeout)
        sock.sendall(request)
        data = sock.recv(256)
        out["elapsed"] = round(time.perf_counter() - t0, 3)
        line = data.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        out["status_line"] = line
        out["ok"] = line.startswith("HTTP/") and " 200 " in f" {line} "
        if not out["ok"]:
            out["error"] = line or "empty CONNECT response"
        return out
    except OSError as exc:
        out["elapsed"] = round(time.perf_counter() - t0, 3)
        out["error"] = str(exc)
        return out
    finally:
        try:
            sock.close()
        except OSError:
            pass


def run_get(
    *,
    label: str,
    url: str,
    proxy_url: str | None,
    impersonate: str | None,
    use_session: bool,
) -> dict[str, Any]:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    kwargs: dict[str, Any] = {
        "timeout": TIMEOUT,
        "headers": dict(_BROWSER_HEADERS),
        "allow_redirects": True,
        "proxies": proxies,
    }
    if impersonate:
        kwargs["impersonate"] = impersonate

    out: dict[str, Any] = {
        "label": label,
        "url": url,
        "proxy": redact_proxy(proxy_url or ""),
        "client": "curl_cffi.Session" if use_session else "curl_cffi.requests.get",
        "impersonate": impersonate or "(not set)",
        "verify": VERIFY,
        "timeout": TIMEOUT,
        "http_version_requested": HTTP_VERSION,
        "elapsed": None,
        "http_status": None,
        "http_version_actual": None,
        "response_size": None,
        "exception": None,
        "curl_error_code": None,
        "curl_error_message": None,
        "pass": False,
    }

    session = None
    t0 = time.perf_counter()
    try:
        if use_session:
            session = curl_requests.Session()
            resp = session.get(url, **kwargs)
        else:
            resp = curl_requests.get(url, **kwargs)
        elapsed = time.perf_counter() - t0
        out["elapsed"] = round(elapsed, 3)
        out["http_status"] = int(getattr(resp, "status_code", 0) or 0)
        body = getattr(resp, "content", b"") or b""
        out["response_size"] = len(body)
        http_v = getattr(resp, "http_version", None)
        if http_v is None:
            http_v = getattr(resp, "version", None)
        out["http_version_actual"] = (
            str(http_v) if http_v is not None else "(unavailable)"
        )
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
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def print_result(result: dict[str, Any]) -> None:
    print()
    print("=" * 60)
    print(f"TEST: {result['label']}")
    print("=" * 60)
    print(f"URL: {result['url']}")
    print(f"Proxy: {result['proxy']}")
    print(f"Client: {result['client']}")
    print(f"Impersonate: {result['impersonate']}")
    print(f"Verify: {result['verify']}")
    print(f"Timeout: {result['timeout']}")
    print(f"HTTP version: {result['http_version_requested']}")
    print()
    print(f"Elapsed: {result['elapsed']}")
    print(f"HTTP status: {result['http_status']}")
    print(f"HTTP version actually used: {result['http_version_actual']}")
    print(f"Response size: {result['response_size']}")
    print()
    print(f"Exception: {result['exception']}")
    print(f"curl error code: {result['curl_error_code']}")
    print(f"curl error message: {result['curl_error_message']}")
    print(f"RESULT: {'PASS' if result['pass'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    import curl_cffi

    parser = argparse.ArgumentParser(description="Steam Workshop network diagnosis")
    parser.add_argument("--url", default=DEFAULT_WORKSHOP_URL)
    args = parser.parse_args(argv)
    url = str(args.url or "").strip() or DEFAULT_WORKSHOP_URL

    print("STEAM WORKSHOP NETWORK DIAGNOSIS (temp script)")
    print(f"curl_cffi version: {curl_cffi.__version__}")
    print(f"Target: {url}")
    print(f"Client (production HTML): curl_cffi.Session")
    print(f"Impersonate (production mirror): {IMPERSONATE}")
    print(f"Timeout (production mirror): {TIMEOUT}")
    print(f"Verify (production): {VERIFY}")
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
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = int(parsed.port or 0)
        tcp = probe_tcp(host, port) if host and port else {"ok": False, "error": "missing"}
        print()
        print("=" * 60)
        print("TEST: P — proxy TCP listen")
        print("=" * 60)
        print(f"Endpoint: {host}:{port}")
        print(f"Elapsed: {tcp.get('elapsed')}")
        print(f"Error: {tcp.get('error')}")
        print(f"RESULT: {'PASS' if tcp.get('ok') else 'FAIL'}")

        connect = probe_http_connect(proxy_url)
        print()
        print("=" * 60)
        print("TEST: Q — HTTP CONNECT steamcommunity.com:443")
        print("=" * 60)
        print(f"Status line: {connect.get('status_line')}")
        print(f"Elapsed: {connect.get('elapsed')}")
        print(f"Error: {connect.get('error')}")
        print(f"RESULT: {'PASS' if connect.get('ok') else 'FAIL'}")

    a = run_get(
        label="A — production Session + proxy + impersonate",
        url=url,
        proxy_url=proxy_url,
        impersonate=IMPERSONATE,
        use_session=True,
    )
    print_result(a)

    b = run_get(
        label="B — direct Session + impersonate (no proxy)",
        url=url,
        proxy_url=None,
        impersonate=IMPERSONATE,
        use_session=True,
    )
    print_result(b)

    c = run_get(
        label="C — one-shot get + proxy + impersonate",
        url=url,
        proxy_url=proxy_url,
        impersonate=IMPERSONATE,
        use_session=False,
    )
    print_result(c)

    d = run_get(
        label="D — Session + proxy, impersonate unset",
        url=url,
        proxy_url=proxy_url,
        impersonate=None,
        use_session=True,
    )
    print_result(d)

    print()
    print("SUMMARY")
    print(f"  Test A production Session+proxy+chrome131: {'PASS' if a['pass'] else 'FAIL'}")
    print(f"  Test B direct Session+chrome131: {'PASS' if b['pass'] else 'FAIL'}")
    print(f"  Test C one-shot proxy+chrome131: {'PASS' if c['pass'] else 'FAIL'}")
    print(f"  Test D Session+proxy no impersonate: {'PASS' if d['pass'] else 'FAIL'}")
    return 0 if a["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
