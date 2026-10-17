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


def _env_proxies() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        out[key] = _text(os.environ.get(key))
    return out


def _steam_http(
    url: str,
    *,
    timeout: float,
    impersonate: str,
    proxies: dict[str, str] | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"curl_cffi import failed: {exc}"}
    try:
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "impersonate": impersonate,
            "allow_redirects": True,
        }
        if proxies:
            kwargs["proxies"] = proxies
        response = curl_requests.get(url, **kwargs)
        return {
            "ok": int(getattr(response, "status_code", 0) or 0) < 400,
            "status": getattr(response, "status_code", None),
            "bytes": len(getattr(response, "content", b"") or b""),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "curl_error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "curl_error": str(exc),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }


def _layer_attempt(
    *,
    name: str,
    url: str,
    host: str,
    timeout: float,
    impersonate: str,
    proxy_url: str = "",
    proxy_source: str = "",
    skip_http: bool = False,
) -> dict[str, Any]:
    from services.proxy_resolver import parse_proxy_url, tcp_listening, probe_proxy_protocol

    parsed = parse_proxy_url(proxy_url) if proxy_url else None
    dns = _dns(host)
    tcp_host = parsed.host if parsed and parsed.host else host
    tcp_port = parsed.port if parsed and parsed.port else 443
    tcp = _tcp(tcp_host, tcp_port, timeout=min(timeout, 5.0))
    tls = {"ok": None, "skipped": True}
    if not proxy_url and dns.get("ok") and tcp.get("ok"):
        tls = _tls(host, timeout=min(timeout, 8.0))
    listening = None
    protocol = ""
    if parsed and parsed.host and parsed.port:
        listening = tcp_listening(parsed.host, parsed.port)
        if listening:
            protocol = probe_proxy_protocol(parsed.host, parsed.port)
    steam_http: dict[str, Any] = {"ok": None, "skipped": True}
    if not skip_http:
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        if tcp.get("ok") or not proxy_url:
            steam_http = _steam_http(
                url, timeout=timeout, impersonate=impersonate, proxies=proxies
            )
        else:
            steam_http = {
                "ok": False,
                "skipped": True,
                "error": "tcp_failed_skip_http",
            }
    from services.archive_observability import classify_failure_layer

    layer = classify_failure_layer(
        {
            "dns": dns if not proxy_url else {"ok": True},
            "tcp": tcp,
            "tls": tls,
            "steam_http": steam_http,
            "curl_cffi_version": "ok",
            "proxy_config": proxy_url or "(direct)",
        }
    )
    if steam_http.get("ok"):
        layer = ""
    return {
        "name": name,
        "proxy_source": proxy_source or ("direct" if not proxy_url else "manual"),
        "proxy_scheme": parsed.scheme if parsed else "",
        "proxy_host": parsed.host if parsed else "",
        "proxy_port": parsed.port if parsed else 0,
        "proxy_url": proxy_url or "(direct)",
        "listening": listening,
        "protocol_probe": protocol,
        "dns": dns,
        "tcp": tcp,
        "tls": tls,
        "http_status": steam_http.get("status"),
        "curl_error": steam_http.get("curl_error") or steam_http.get("error") or "",
        "elapsed_ms": steam_http.get("elapsed_ms") or tcp.get("elapsed_ms"),
        "steam_http": steam_http,
        "failure_layer": layer or None,
        "ok": bool(steam_http.get("ok")),
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
        _get_archive_proxy,
    )
    from services.archive_observability import classify_failure_layer
    from services.proxy_resolver import (
        refresh_system_proxy,
        tcp_listening,
        probe_proxy_protocol,
    )

    try:
        import curl_cffi

        curl_ver = getattr(curl_cffi, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        curl_ver = f"unavailable: {exc}"

    qsettings_proxy = _proxy_from_settings()
    resolved = refresh_system_proxy(probe=True)
    archive_resolved = _get_archive_proxy()
    url = WORKSHOP_PAGE_URL.format(id=mod_id) if mod_id else WORKSHOP_URL.format(id="0")
    host = urlparse(url).hostname or "steamcommunity.com"
    timeout = DEFAULT_TIMEOUT

    candidates = [ep.as_dict() for ep in resolved.candidates]
    for item in candidates:
        if item.get("host") and item.get("port"):
            item["listening"] = tcp_listening(str(item["host"]), int(item["port"]))
            if item["listening"]:
                item["protocol_probe"] = probe_proxy_protocol(
                    str(item["host"]), int(item["port"])
                )

    attempts = [
        _layer_attempt(
            name="A_direct",
            url=url,
            host=host,
            timeout=timeout,
            impersonate=IMPERSONATE,
            proxy_url="",
            proxy_source="direct",
        ),
        _layer_attempt(
            name="B_system_auto",
            url=url,
            host=host,
            timeout=timeout,
            impersonate=IMPERSONATE,
            proxy_url=resolved.url,
            proxy_source=resolved.source or "system",
            skip_http=not resolved.url,
        ),
        _layer_attempt(
            name="C_manual_qsettings",
            url=url,
            host=host,
            timeout=timeout,
            impersonate=IMPERSONATE,
            proxy_url=qsettings_proxy,
            proxy_source="qsettings",
            skip_http=not qsettings_proxy,
        ),
        _layer_attempt(
            name="D_curl_cffi_resolved",
            url=url,
            host=host,
            timeout=timeout,
            impersonate=IMPERSONATE,
            proxy_url=archive_resolved or resolved.url,
            proxy_source="archive_contract",
            skip_http=not (archive_resolved or resolved.url),
        ),
    ]

    dns = _dns(host)
    tcp = _tcp(host)
    tls = _tls(host) if dns.get("ok") and tcp.get("ok") else {"ok": False, "skipped": True}
    steam_direct = next((a["steam_http"] for a in attempts if a["name"] == "A_direct"), {})
    failure_layer = classify_failure_layer(
        {
            "dns": dns,
            "tcp": tcp,
            "tls": tls,
            "steam_http": steam_direct,
            "curl_cffi_version": curl_ver,
            "proxy_config": archive_resolved or resolved.url or "(direct)",
        }
    )
    return {
        "mod_id": mod_id,
        "python_version": sys.version.split()[0],
        "curl_cffi_version": curl_ver,
        "qsettings_proxy_url": qsettings_proxy or "(empty)",
        "proxy_config": archive_resolved or resolved.url or "(empty / direct)",
        "resolved_proxy": resolved.as_dict(),
        "archive_uses_resolved_proxy": bool(archive_resolved)
        and archive_resolved == (resolved.url or archive_resolved),
        "archive_resolved_proxy_url": archive_resolved or "(direct)",
        "impersonate": IMPERSONATE,
        "timeout": timeout,
        "retry": {
            "main_page_retries": MAIN_PAGE_RETRIES,
            "html_429_max_attempts": HTML_429_MAX_ATTEMPTS,
            "html_429_backoff_base_sec": HTML_429_BACKOFF_BASE_SEC,
            "cooldown_sec": ARCHIVE_RETRY_COOLDOWN_SEC,
        },
        "url": url,
        "env_proxies": _env_proxies(),
        "system_proxy_candidates": candidates,
        "dns": dns,
        "tcp": tcp,
        "tls": tls,
        "steam_http": steam_direct,
        "attempts": attempts,
        "failure_layer": failure_layer,
        "old_env": "OLD_ENV_UNKNOWN",
        "current_env": "CURRENT_ENV_BASELINE",
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
