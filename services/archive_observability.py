"""Structured archive lifecycle logs and failure classification."""

from __future__ import annotations

import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
NETWORK_FAILURE = "NETWORK_FAILURE"
STEAM_FAILURE = "STEAM_FAILURE"
DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
CONFIG_FAILURE = "CONFIG_FAILURE"
APPLICATION_FAILURE = "APPLICATION_FAILURE"


def classify_archive_error(exc: BaseException | str, *, proxy: str = "") -> str:
    """Map archive exceptions. curl (28) is NETWORK, never APPLICATION."""
    text = str(exc or "")
    lowered = text.lower()
    code = ""
    if hasattr(exc, "code"):
        code = str(getattr(exc, "code") or "")
    combined = f"{code} {text} {lowered}"
    if (
        "(28)" in combined
        or "curl: (28)" in lowered
        or "timed out" in lowered
        or "timeout" in lowered
        or "operation timed out" in lowered
    ):
        return NETWORK_FAILURE
    if "proxy" in lowered and ("fail" in lowered or "refused" in lowered or "unable" in lowered):
        return CONFIG_FAILURE if proxy else ENVIRONMENT_FAILURE
    if any(
        token in lowered
        for token in ("dns", "getaddrinfo", "name or service not known", "resolve")
    ):
        return ENVIRONMENT_FAILURE
    if "429" in combined or "rate limit" in lowered:
        return STEAM_FAILURE
    if any(token in lowered for token in ("impersonate", "curl_cffi", "module", "import")):
        return DEPENDENCY_FAILURE
    if "http" in lowered and any(s in combined for s in ("401", "403", "404", "500", "502", "503")):
        return STEAM_FAILURE
    if "config" in lowered or "qsettings" in lowered:
        return CONFIG_FAILURE
    if "connection" in lowered or "network" in lowered or "curl: (" in lowered:
        return NETWORK_FAILURE
    return APPLICATION_FAILURE


def curl_code_from_error(exc: BaseException | str) -> str:
    text = str(exc or "")
    for token in ("(28)", "(7)", "(6)", "(35)", "(56)", "(52)"):
        if token in text:
            return token.strip("()")
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "28"
    return ""


def log_archive_start(
    *,
    mod_id: str,
    url: str,
    source: str = "steam_workshop",
    proxy: str = "",
    timeout: float | None = None,
    impersonate: str = "",
) -> None:
    logger.info(
        "[ARCHIVE_START] mod_id=%s url=%s source=%s proxy=%s timeout=%s impersonate=%s",
        mod_id,
        url,
        source,
        proxy or "(direct)",
        timeout if timeout is not None else "",
        impersonate,
    )


def log_archive_connect(
    *,
    host: str,
    resolved_ip: str = "",
    proxy: str = "",
    elapsed_ms: float = 0.0,
) -> None:
    logger.info(
        "[ARCHIVE_CONNECT] host=%s resolved_ip=%s proxy=%s elapsed_ms=%.1f",
        host,
        resolved_ip or "(unresolved)",
        proxy or "(direct)",
        elapsed_ms,
    )


def log_archive_success(
    *,
    status: int | str,
    bytes_count: int = 0,
    elapsed_ms: float = 0.0,
) -> None:
    logger.info(
        "[ARCHIVE_SUCCESS] status=%s bytes=%s elapsed_ms=%.1f",
        status,
        bytes_count,
        elapsed_ms,
    )


def log_archive_failure(
    *,
    error_type: str,
    curl_code: str = "",
    http_status: str = "",
    elapsed_ms: float = 0.0,
    proxy: str = "",
    host: str = "",
    retry_count: int = 0,
    error: str = "",
) -> None:
    logger.warning(
        "[ARCHIVE_FAILURE] error_type=%s curl_code=%s http_status=%s "
        "elapsed_ms=%.1f proxy=%s host=%s retry_count=%s error=%s",
        error_type,
        curl_code or "",
        http_status or "",
        elapsed_ms,
        proxy or "(direct)",
        host,
        retry_count,
        error,
    )


def log_archive_stub(*, reason: str, stub_bytes: int = 0) -> None:
    logger.warning("[ARCHIVE_STUB] reason=%s stub_bytes=%s", reason, stub_bytes)


def resolve_host_ip(url: str) -> tuple[str, str]:
    host = urlparse(url).hostname or ""
    if not host:
        return "", ""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ip = str(infos[0][4][0]) if infos else ""
        return host, ip
    except OSError:
        return host, ""


def connect_probe(url: str, proxy: str = "") -> dict[str, Any]:
    t0 = time.perf_counter()
    host, ip = resolve_host_ip(url)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log_archive_connect(host=host, resolved_ip=ip, proxy=proxy, elapsed_ms=elapsed_ms)
    return {"host": host, "resolved_ip": ip, "elapsed_ms": elapsed_ms, "proxy": proxy}
