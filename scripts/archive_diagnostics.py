"""Read-only archive environment diagnostics. Does not mutate production."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _proxy_from_settings() -> str:
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return ""
    settings = QSettings("SteamModManager", "WorkshopLibrary")
    return _text(settings.value("network/proxy_url", "", str))


def _dns(host: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = sorted({str(item[4][0]) for item in infos})
        return {
            "ok": True,
            "host": host,
            "ips": ips,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


def _tcp(host: str, port: int = 443, timeout: float = 5.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "host": host,
                "port": port,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


def _tls(host: str, timeout: float = 8.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as wrapped:
                return {
                    "ok": True,
                    "host": host,
                    "version": wrapped.version(),
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


def _steam_http(url: str, *, timeout: float, impersonate: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"curl_cffi import failed: {exc}"}
    try:
        response = curl_requests.get(
            url,
            timeout=timeout,
            impersonate=impersonate,
            allow_redirects=True,
        )
        return {
            "ok": int(getattr(response, "status_code", 0) or 0) < 400,
            "status": getattr(response, "status_code", None),
            "bytes": len(getattr(response, "content", b"") or b""),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


def collect_archive_diagnostics(*, mod_id: str) -> dict[str, Any]:
    from services.archive import (
        ARCHIVE_RETRY_COOLDOWN_SEC,
        DEFAULT_TIMEOUT,
        HTML_429_BACKOFF_BASE_SEC,
        HTML_429_MAX_ATTEMPTS,
        IMPERSONATE,
        MAIN_PAGE_RETRIES,
        WORKSHOP_PAGE_URL,
    )

    try:
        import curl_cffi

        curl_ver = getattr(curl_cffi, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        curl_ver = f"unavailable: {exc}"

    proxy = _proxy_from_settings()
    url = WORKSHOP_PAGE_URL.format(id=mod_id) if mod_id else WORKSHOP_URL.format(id="0")
    host = urlparse(url).hostname or "steamcommunity.com"
    return {
        "mod_id": mod_id,
        "python_version": sys.version.split()[0],
        "curl_cffi_version": curl_ver,
        "proxy_config": proxy or "(empty / direct)",
        "impersonate": IMPERSONATE,
        "timeout": DEFAULT_TIMEOUT,
        "retry": {
            "main_page_retries": MAIN_PAGE_RETRIES,
            "html_429_max_attempts": HTML_429_MAX_ATTEMPTS,
            "html_429_backoff_base_sec": HTML_429_BACKOFF_BASE_SEC,
            "cooldown_sec": ARCHIVE_RETRY_COOLDOWN_SEC,
        },
        "url": url,
        "dns": _dns(host),
        "tcp": _tcp(host),
        "tls": _tls(host),
        "steam_http": _steam_http(url, timeout=DEFAULT_TIMEOUT, impersonate=IMPERSONATE),
        "note": "Read-only diagnostics. Does not archive, deploy, or mutate identity.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Steam archive diagnostics.")
    parser.add_argument("--mod-id", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = collect_archive_diagnostics(mod_id=str(args.mod_id).strip())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    print(text)
    return 0 if report.get("steam_http", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
