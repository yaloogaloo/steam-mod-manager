"""Archive real Steam Workshop pages for offline viewing."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup, FeatureNotFound, Tag
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import CurlError, RequestException

from core.models import ModMetadata
from core.paths import asset_cache_dir

logger = logging.getLogger(__name__)

WORKSHOP_PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"
DEFAULT_INDEX_NAME = "index.html"
DEFAULT_ASSETS_DIR = "assets"
DEFAULT_TIMEOUT = 15

# Must match ui/sync_view.py QSettings keys (Sync Center proxy field).
_SETTINGS_ORG = "SteamModManager"
_SETTINGS_APP = "WorkshopLibrary"
_SETTING_PROXY = "network/proxy_url"

# Sidecar under ``.info/`` — not part of ModMetadata / SQLite schema.
_ARCHIVE_ATTEMPT_NAME = "archive_attempt.json"
ARCHIVE_RETRY_COOLDOWN_SEC = 10 * 60

# TLS fingerprint impersonation target for curl_cffi
IMPERSONATE = "chrome131"

# Global Steam archive pacing — HTML page GETs only (not CDN assets).
ARCHIVE_REQUEST_INTERVAL_SECONDS = 3.0
ARCHIVE_MIN_INTERVAL_SEC = ARCHIVE_REQUEST_INTERVAL_SECONDS  # alias
ARCHIVE_MAX_CONCURRENCY = 1
ARCHIVE_BLOCK_AFTER_429_SEC = 30 * 60  # cool-off after consecutive HTML 429s

# Shared across all Mods — caps total CDN/asset GETs process-wide.
GLOBAL_ASSET_WORKERS = 6
# Backward-compatible alias (local pools must not multiply beyond the global cap).
ASSET_DOWNLOAD_WORKERS = GLOBAL_ASSET_WORKERS

# HTML 429: retry current Mod; only consecutive failures trip the global fuse.
HTML_429_MAX_ATTEMPTS = 3
HTML_429_BACKOFF_BASE_SEC = 8.0
# Soft spacing added to the HTML limiter after any 429 (before global fuse).
HTML_429_SOFT_COOLDOWN_SEC = 45.0
# > per-mod attempts so one Mod's retries alone cannot arm the 30-minute fuse.
HTML_429_CONSECUTIVE_BEFORE_BLOCK = 5

MAIN_PAGE_RETRIES = 2
MAX_CSS_FILES = 60
MAX_IMAGES = 120
MAX_ASSET_BYTES = 12 * 1024 * 1024

_GLOBAL_ASSET_SEMAPHORE = threading.Semaphore(GLOBAL_ASSET_WORKERS)

# Global URL → sha256 disk cache under data/asset_cache/ (raw bytes, pre-CSS rewrite).
_ASSET_CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0}
_ASSET_CACHE_STATS_LOCK = threading.Lock()
_CACHE_KEY_LOCKS_GUARD = threading.Lock()
_CACHE_KEY_LOCKS: dict[str, threading.Lock] = {}

_ARCHIVE_STATUS_NAME = "archive_status.json"
_RATE_LIMITED_REASON = "rate_limited"
_SETTINGS_STEAM_COOKIE = "network/steam_cookie"
RATE_LIMIT_USER_MESSAGE = "Steam 当前限流，请稍后重试"

# Per-thread archive context so shared Session workers never mix mod_id logs.
_archive_tls = threading.local()


def reset_asset_cache_stats() -> None:
    """Test / bench helper: clear hit/miss counters."""
    with _ASSET_CACHE_STATS_LOCK:
        _ASSET_CACHE_STATS["hit"] = 0
        _ASSET_CACHE_STATS["miss"] = 0


def get_asset_cache_stats() -> dict[str, int]:
    """Return a snapshot of ``{hit, miss}`` for the global asset cache."""
    with _ASSET_CACHE_STATS_LOCK:
        return {
            "hit": int(_ASSET_CACHE_STATS["hit"]),
            "miss": int(_ASSET_CACHE_STATS["miss"]),
        }


def _asset_cache_key(absolute_url: str) -> str:
    return hashlib.sha256(absolute_url.encode("utf-8")).hexdigest()


def _cache_lock_for(key: str) -> threading.Lock:
    with _CACHE_KEY_LOCKS_GUARD:
        lock = _CACHE_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CACHE_KEY_LOCKS[key] = lock
        return lock


def _find_cached_asset(key: str, preferred_ext: str) -> Path | None:
    cache_root = asset_cache_dir()
    preferred = cache_root / f"{key}{preferred_ext}"
    try:
        if preferred.is_file() and preferred.stat().st_size > 0:
            return preferred
        for path in sorted(cache_root.glob(f"{key}.*")):
            if path.is_file() and path.stat().st_size > 0:
                return path
        bare = cache_root / key
        if bare.is_file() and bare.stat().st_size > 0:
            return bare
    except OSError:
        return None
    return None


def _copy_file_atomic(src: Path, dest: Path) -> None:
    """Copy *src* to *dest* via a same-directory temp file then replace."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dest.stem + "_", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as out_fh, src.open("rb") as in_fh:
            while True:
                chunk = in_fh.read(64 * 1024)
                if not chunk:
                    break
                out_fh.write(chunk)
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ArchiveRateLimitedError(RuntimeError):
    """Raised when Steam archive is globally blocked after HTTP 429."""

    def __init__(self, message: str = RATE_LIMIT_USER_MESSAGE) -> None:
        super().__init__(message)

# curl_cffi: RequestsError is an alias of RequestException; CurlError is separate
# and is NOT a subclass — catch both for network / TLS failures.
_CURL_HTTP_ERRORS: tuple[type[BaseException], ...] = (RequestException, CurlError)
_RequestsError = getattr(curl_requests, "RequestsError", RequestException)
if _RequestsError not in _CURL_HTTP_ERRORS:
    _CURL_HTTP_ERRORS = (_RequestsError, *_CURL_HTTP_ERRORS)

# Proxy attempt may fail when the local proxy is down; these trigger direct fallback.
_PROXY_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    *_CURL_HTTP_ERRORS,
    OSError,
    TimeoutError,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
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


class SteamArchiveLimiter:
    """
    Process-wide single-flight limiter for Steam Workshop **HTML** GETs only.

    Static assets (CSS / images / fonts / CDN) must not use this limiter.

    - Only one HTML request at a time (global lock)
    - Minimum interval between HTML requests
    - HTML 429: count consecutive failures; only after
      ``HTML_429_CONSECUTIVE_BEFORE_BLOCK`` set ``archive_blocked_until``
    """

    def __init__(
        self, min_interval: float = ARCHIVE_REQUEST_INTERVAL_SECONDS
    ) -> None:
        self.min_interval = float(min_interval)
        # RLock: block_for_rate_limit may run while request_slot holds the lock.
        self._lock = threading.RLock()
        self._next_allowed = 0.0
        self.archive_blocked_until: float = 0.0
        self.last_request_timestamp: float | None = None
        self.consecutive_html_429: int = 0

    def is_blocked(self) -> bool:
        return time.time() < float(self.archive_blocked_until or 0.0)

    def blocked_remaining(self) -> float:
        return max(0.0, float(self.archive_blocked_until or 0.0) - time.time())

    def register_html_success(self) -> None:
        """Reset consecutive HTML 429 counter after a successful HTML GET."""
        with self._lock:
            self.consecutive_html_429 = 0

    def register_html_429(self) -> bool:
        """
        Record an HTML 429.

        Applies a soft HTML cool-down (extra limiter spacing) immediately.
        Returns True when the consecutive threshold was reached and the global
        cool-off was armed.
        """
        with self._lock:
            self.consecutive_html_429 += 1
            count = self.consecutive_html_429
            # Pace subsequent HTML without freezing the whole batch yet.
            soft = float(HTML_429_SOFT_COOLDOWN_SEC)
            self._next_allowed = max(
                self._next_allowed, time.monotonic() + soft
            )
            if count >= int(HTML_429_CONSECUTIVE_BEFORE_BLOCK):
                self.archive_blocked_until = time.time() + float(
                    ARCHIVE_BLOCK_AFTER_429_SEC
                )
                logger.warning(
                    "[STEAM ARCHIVE] consecutive HTML 429=%s → "
                    "archive_blocked_until for %.0fs (%s)",
                    count,
                    ARCHIVE_BLOCK_AFTER_429_SEC,
                    RATE_LIMIT_USER_MESSAGE,
                )
                return True
            logger.info(
                "[STEAM ARCHIVE] HTML 429 consecutive=%s/%s "
                "soft_cooldown=%.0fs (no global block yet)",
                count,
                HTML_429_CONSECUTIVE_BEFORE_BLOCK,
                soft,
            )
            return False

    def block_for_rate_limit(
        self, seconds: float = ARCHIVE_BLOCK_AFTER_429_SEC
    ) -> None:
        with self._lock:
            self.archive_blocked_until = time.time() + float(seconds)
            logger.warning(
                "[STEAM ARCHIVE] archive_blocked_until set for %.0fs (%s)",
                seconds,
                RATE_LIMIT_USER_MESSAGE,
            )

    def wait(self) -> float:
        """
        Acquire the single-flight lock, wait for the interval, then release the
        spacing schedule. Caller must still serialize the actual GET via
        ``request_slot`` (or hold the lock themselves).
        """
        if self.is_blocked():
            raise ArchiveRateLimitedError(
                f"{RATE_LIMIT_USER_MESSAGE}（约 {int(self.blocked_remaining())} 秒后可重试）"
            )
        with self._lock:
            now = time.monotonic()
            wait_s = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if wait_s > 0:
            time.sleep(wait_s)
        with self._lock:
            self.last_request_timestamp = time.time()
        return wait_s

    def request_slot(self) -> "threading.Lock":
        """
        Context-style: acquire exclusive lock for one Steam HTTP request.

        Usage::
            with STEAM_ARCHIVE_LIMITER.request_slot():
                ... GET ...
        """
        return _ArchiveRequestSlot(self)

    def reset(self) -> None:
        """Test helper: clear schedule and block."""
        with self._lock:
            self._next_allowed = 0.0
            self.last_request_timestamp = None
            self.archive_blocked_until = 0.0
            self.consecutive_html_429 = 0


class _ArchiveRequestSlot:
    """Context manager: exclusive Steam request + interval wait."""

    def __init__(self, limiter: SteamArchiveLimiter) -> None:
        self._limiter = limiter
        self.wait_seconds = 0.0

    def __enter__(self) -> "_ArchiveRequestSlot":
        if self._limiter.is_blocked():
            raise ArchiveRateLimitedError(
                f"{RATE_LIMIT_USER_MESSAGE}（约 {int(self._limiter.blocked_remaining())} 秒后可重试）"
            )
        self._limiter._lock.acquire()
        try:
            now = time.monotonic()
            wait_s = max(0.0, self._limiter._next_allowed - now)
            self._limiter._next_allowed = (
                max(now, self._limiter._next_allowed) + self._limiter.min_interval
            )
            if wait_s > 0:
                # Sleep while holding the lock so no other thread can GET.
                time.sleep(wait_s)
            self.wait_seconds = wait_s
            self._limiter.last_request_timestamp = time.time()
            return self
        except Exception:
            self._limiter._lock.release()
            raise

    def __exit__(self, *exc: object) -> None:
        self._limiter._lock.release()


# Back-compat alias used by older tests / imports.
SteamArchiveRateLimiter = SteamArchiveLimiter
STEAM_ARCHIVE_LIMITER = SteamArchiveLimiter()
STEAM_ARCHIVE_RATE_LIMITER = STEAM_ARCHIVE_LIMITER
_ARCHIVE_SEMAPHORE = threading.Semaphore(ARCHIVE_MAX_CONCURRENCY)
_CONFIGURE_UNSET: object = object()


def is_archive_globally_blocked() -> bool:
    return STEAM_ARCHIVE_LIMITER.is_blocked()


def _get_steam_cookie() -> str | None:
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    cookie = (settings.value(_SETTINGS_STEAM_COOKIE, "", str) or "").strip()
    return cookie or None


def _session_cookie_count(session: Any) -> int:
    try:
        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return 0
        if hasattr(cookies, "keys"):
            return len(list(cookies.keys()))
        return len(list(cookies))
    except Exception:  # noqa: BLE001
        return 0


def _session_has_sessionid(session: Any) -> bool:
    try:
        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return False
        if hasattr(cookies, "get"):
            return bool(cookies.get("sessionid"))
        return "sessionid" in list(cookies.keys())
    except Exception:  # noqa: BLE001
        return False


def _parse_retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None or raw == "":
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _workshop_id_from_url(url: str) -> str:
    match = re.search(r"[?&]id=(\d+)", url)
    return match.group(1) if match else ""


def _is_rate_limited_error(exc: BaseException) -> bool:
    if isinstance(exc, ArchiveRateLimitedError):
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return (
        "429" in text
        or "too many requests" in text
        or "限流" in str(exc)
        or _RATE_LIMITED_REASON in text
    )


def read_archive_status(info_dir: str | Path) -> dict[str, Any] | None:
    path = Path(info_dir) / _ARCHIVE_STATUS_NAME
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _has_successful_offline_page(index_path: Path) -> bool:
    try:
        return (
            index_path.is_file()
            and index_path.stat().st_size > 0
            and not is_stub_offline_page(index_path)
        )
    except OSError:
        return False


def write_archive_status(
    info_dir: str | Path,
    *,
    reason: str,
    published_file_id: str | int | None = None,
    detail: str | None = None,
) -> Path:
    """Lightweight ``.info/archive_status.json`` (not Mod DB / mod.json)."""
    info_dir = Path(info_dir)
    info_dir.mkdir(parents=True, exist_ok=True)
    path = info_dir / _ARCHIVE_STATUS_NAME
    payload = {
        "archive_failed_reason": reason,
        "published_file_id": str(published_file_id or ""),
        "detail": detail or "",
        "updated_at": time.time(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _tls_mod_id() -> str:
    return str(getattr(_archive_tls, "mod_id", "") or "")


def _set_tls_mod_id(mod_id: str) -> None:
    _archive_tls.mod_id = str(mod_id)


def _parse_workshop_html(html_text: str) -> BeautifulSoup:
    """
    Parse Workshop HTML with ``lxml`` when available; otherwise ``html.parser``.

    Missing ``lxml`` must not fail the whole archive after a successful GET.
    """
    try:
        return BeautifulSoup(html_text, "lxml")
    except FeatureNotFound as exc:
        logger.warning(
            "lxml unavailable for BeautifulSoup (%s); falling back to html.parser",
            exc,
        )
        return BeautifulSoup(html_text, "html.parser")

# Markers written by ``write_fallback_page`` — used to reject stubs as "valid".
_STUB_MARKERS: tuple[str, ...] = (
    "Offline (stub)",
    "未能下载完整的 Steam 创意工坊原网页",
    "未能下载完整的 Steam",
)


def is_stub_offline_page(path: str | Path) -> bool:
    """
    Return True if *path* looks like a failed-archive stub ``index.html``.

    Successful live mirrors contain ``smm-offline-banner`` and do not match
    these markers. Stubs must not block future re-archive attempts.
    """
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size <= 0:
            return False
        # Only need a small prefix; stubs are ~1–2 KB
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    if "smm-offline-banner" in text:
        return False

    lowered = text.lower()
    for marker in _STUB_MARKERS:
        if marker in text:
            return True
    # curl / connection errors embedded in the stub body
    if "curl:" in lowered and (
        "connection was reset" in lowered
        or "recv failure" in lowered
        or "failed to perform" in lowered
    ):
        return True
    if "archive failed" in lowered:
        return True
    return False


def _get_archive_proxy() -> str | None:
    """
    Resolve the Sync Center proxy URL from QSettings.

    Supports ``http://``, ``https://``, and ``socks5://`` URLs as entered by
    the user. Returns ``None`` when unset / blank (direct connect).
    """
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return None
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    url = (settings.value(_SETTING_PROXY, "", str) or "").strip()
    return url or None


def archive_proxies_dict(proxy_url: str | None = None) -> dict[str, str] | None:
    """Build curl_cffi ``proxies={http, https}`` from a URL or QSettings."""
    url = (proxy_url if proxy_url is not None else _get_archive_proxy()) or ""
    url = url.strip()
    if not url:
        return None
    return {"http": url, "https": url}


def _attempt_state_path(info_dir: Path) -> Path:
    return Path(info_dir) / _ARCHIVE_ATTEMPT_NAME


def read_last_archive_attempt(info_dir: str | Path) -> float | None:
    """Unix timestamp of the last archive attempt, if recorded."""
    path = _attempt_state_path(Path(info_dir))
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("last_archive_attempt")
        return float(ts) if ts is not None else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_last_archive_attempt(
    info_dir: str | Path,
    *,
    failed: bool = False,
    when: float | None = None,
) -> None:
    """Persist last archive attempt time (sidecar JSON under ``.info/``)."""
    info_dir = Path(info_dir)
    info_dir.mkdir(parents=True, exist_ok=True)
    ts = float(when if when is not None else time.time())
    payload: dict[str, Any] = {"last_archive_attempt": ts}
    if failed:
        payload["last_archive_failure"] = ts
    else:
        try:
            existing = _attempt_state_path(info_dir)
            if existing.is_file():
                old = json.loads(existing.read_text(encoding="utf-8"))
                if old.get("last_archive_failure") is not None:
                    payload["last_archive_failure"] = old["last_archive_failure"]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    _attempt_state_path(info_dir).write_text(json.dumps(payload), encoding="utf-8")


def is_archive_cooldown_active(
    info_dir: str | Path,
    *,
    now: float | None = None,
    cooldown_sec: float = ARCHIVE_RETRY_COOLDOWN_SEC,
) -> bool:
    """True if a recent archive attempt is still inside the retry cooldown."""
    last = read_last_archive_attempt(info_dir)
    if last is None:
        return False
    current = float(now if now is not None else time.time())
    return (current - last) < float(cooldown_sec)


def ensure_offline_page_nonblocking_probe(info_dir: str | Path) -> bool:
    """
    Return True if ``ensure_offline_page`` can finish without a Steam request.

    Used by the detail UI / tests to prove opening Mod info need not block.
    """
    info_dir = Path(info_dir)
    index = info_dir / DEFAULT_INDEX_NAME
    try:
        if not index.is_file() or index.stat().st_size <= 0:
            return False
        if not is_stub_offline_page(index):
            return True
        return is_archive_cooldown_active(info_dir)
    except OSError:
        return False


class SteamArchiveSyncContext:
    """
    Sync-batch scope: one ``OfflinePageArchiver`` / curl_cffi Session shared
    across all Mods in a single offline-archive run.
    """

    def __init__(
        self,
        *,
        proxies: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        steam_cookie: str | None = None,
    ) -> None:
        self.archiver = OfflinePageArchiver(
            timeout=timeout,
            proxies=proxies,
            steam_cookie=steam_cookie,
        )

    def close(self) -> None:
        self.archiver.close()

    def __enter__(self) -> SteamArchiveSyncContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class OfflinePageArchiver:
    """
    Download the live Steam Workshop item page into ``.info/``.

    Low-frequency, single-flight Steam HTML archive (not a bulk crawler).
    Prefer user-provided browser Cookie when configured.
    """

    def __init__(
        self,
        *,
        session: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_css: int = MAX_CSS_FILES,
        max_images: int = MAX_IMAGES,
        proxies: dict[str, str] | None = None,
        steam_cookie: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_css = max_css
        self.max_images = max_images
        # Explicit non-empty proxies win; otherwise resolve Sync Center QSettings.
        if proxies:
            self._proxies: dict[str, str] | None = dict(proxies)
        else:
            self._proxies = archive_proxies_dict()

        if steam_cookie is not None:
            self._steam_cookie = (steam_cookie or "").strip() or None
        else:
            self._steam_cookie = _get_steam_cookie()

        if session is not None:
            self._session = session
            self._owns_session = False
        else:
            self._session = curl_requests.Session()
            self._owns_session = True
        self._session_lock = threading.Lock()
        self._active_mod_id: str = ""
        self._request_count: int = 0

    def configure(
        self,
        *,
        proxies: dict[str, str] | None | object = _CONFIGURE_UNSET,
        timeout: float | None = None,
        steam_cookie: str | None | object = _CONFIGURE_UNSET,
    ) -> None:
        """Update proxy/timeout/cookie for a batch without replacing the Session."""
        if timeout is not None:
            self.timeout = float(timeout)
        if proxies is not _CONFIGURE_UNSET:
            if isinstance(proxies, dict) and proxies:
                self._proxies = dict(proxies)
            else:
                self._proxies = archive_proxies_dict()
        if steam_cookie is not _CONFIGURE_UNSET:
            if isinstance(steam_cookie, str) and steam_cookie.strip():
                self._steam_cookie = steam_cookie.strip()
            elif steam_cookie is None:
                self._steam_cookie = _get_steam_cookie()
            else:
                self._steam_cookie = None

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None

    def __enter__(self) -> OfflinePageArchiver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request_headers(self) -> dict[str, str]:
        headers = dict(_BROWSER_HEADERS)
        if self._steam_cookie:
            headers["Cookie"] = self._steam_cookie
        return headers

    def _session_get(self, url: str, **kwargs: Any) -> Any:
        """HTML Session GET — used only on the limited HTML path."""
        if self._session is None:
            raise RuntimeError("OfflinePageArchiver session is closed")
        # Exclusive lock is held by SteamArchiveLimiter.request_slot during GET.
        return self._session.get(url, **kwargs)

    def _request_kwargs(
        self,
        *,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> dict[str, Any]:
        return {
            "timeout": self.timeout,
            "impersonate": IMPERSONATE,
            "headers": self._request_headers(),
            "allow_redirects": allow_redirects,
            "stream": stream,
        }

    def _http_get(
        self,
        url: str,
        *,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> Any:
        """
        HTML page GET via curl_cffi Session + ``SteamArchiveLimiter``.

        Only for steamcommunity ``filedetails`` (and other HTML) fetches.
        Single-flight + >=3s spacing. A single HTML 429 is retryable; only
        consecutive failures arm the 30-minute global block.
        Static assets must use ``_http_get_asset`` instead.
        """
        kwargs = self._request_kwargs(
            stream=stream, allow_redirects=allow_redirects
        )

        mod_id = _tls_mod_id() or self._active_mod_id or "-"
        url_id = _workshop_id_from_url(url) or "-"

        with STEAM_ARCHIVE_LIMITER.request_slot() as slot:
            t0 = time.monotonic()
            response = self._perform_get(url, kwargs)
            elapsed = time.monotonic() - t0
            self._request_count += 1

            status = getattr(response, "status_code", None)
            cookie_count = _session_cookie_count(self._session)
            has_sessionid = _session_has_sessionid(self._session) or bool(
                self._steam_cookie and "sessionid" in self._steam_cookie.lower()
            )
            last_ts = STEAM_ARCHIVE_LIMITER.last_request_timestamp

            logger.info(
                "[STEAM ARCHIVE] mod_id=%s url_id=%s session_id存在=%s "
                "cookie数量=%s last_request_timestamp=%s wait_seconds=%.3f "
                "last_status=%s request_count=%s elapsed=%.3f url=%s",
                mod_id,
                url_id,
                has_sessionid,
                cookie_count,
                last_ts if last_ts is not None else "None",
                slot.wait_seconds,
                status,
                self._request_count,
                elapsed,
                url,
            )

            if status == 429:
                if STEAM_ARCHIVE_LIMITER.register_html_429():
                    raise ArchiveRateLimitedError(
                        f"{RATE_LIMIT_USER_MESSAGE}（已暂停 Steam 离线抓取 "
                        f"{ARCHIVE_BLOCK_AFTER_429_SEC // 60} 分钟）"
                    )
                # Transient — caller (_fetch_main_html) retries with backoff.
                err = RequestException(f"HTTP Error 429 (retryable): {url}")
                setattr(err, "response", response)
                raise err
            STEAM_ARCHIVE_LIMITER.register_html_success()
            return response

    def _http_get_asset(
        self,
        url: str,
        *,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> Any:
        """
        Static asset GET (CSS / images / fonts / CDN).

        Does **not** use ``SteamArchiveLimiter`` and never sets
        ``archive_blocked_until``. HTTP 429 fails this asset only.

        All Mods share ``_GLOBAL_ASSET_SEMAPHORE`` (``GLOBAL_ASSET_WORKERS``).
        The slot is held until the response is fully consumed / closed so
        streaming bodies cannot exceed the process-wide cap.
        """
        kwargs = self._request_kwargs(
            stream=stream, allow_redirects=allow_redirects
        )
        _GLOBAL_ASSET_SEMAPHORE.acquire()
        released = False

        def _release() -> None:
            nonlocal released
            if not released:
                released = True
                _GLOBAL_ASSET_SEMAPHORE.release()

        try:
            t0 = time.monotonic()
            response = self._perform_asset_get(url, kwargs)
            elapsed = time.monotonic() - t0
            status = getattr(response, "status_code", None)
            logger.debug(
                "[ARCHIVE ASSET] status=%s elapsed=%.3f url=%s",
                status,
                elapsed,
                url,
            )
            # Non-success: release immediately so a 404/5xx cannot leak a slot.
            if status is not None and int(status) >= 400:
                if status == 429:
                    logger.info(
                        "[ARCHIVE ASSET] 429 soft-fail (no global block) url=%s",
                        url,
                    )
                err = RequestException(f"HTTP Error {status} for asset: {url}")
                setattr(err, "response", response)
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                raise err

            if stream:
                # Keep the slot until caller finishes reading (close / finalize).
                original_close = getattr(response, "close", None)

                def _close_and_release(*_a: object, **_k: object) -> None:
                    try:
                        if callable(original_close):
                            original_close()
                    finally:
                        _release()

                response.close = _close_and_release  # type: ignore[method-assign]
                return response

            _release()
            return response
        except Exception:
            _release()
            raise

    def _perform_get(self, url: str, kwargs: dict[str, Any]) -> Any:
        """One HTML Session GET with optional proxy → direct fallback."""
        if self._proxies:
            proxy_label = (
                self._proxies.get("https")
                or self._proxies.get("http")
                or "<set>"
            )
            logger.info("[ARCHIVE] proxy=%s", proxy_label)
            try:
                response = self._session_get(
                    url, **kwargs, proxies=self._proxies
                )
                logger.info("[ARCHIVE] proxy success")
                return response
            except _PROXY_TRANSPORT_ERRORS as exc:
                logger.info("[ARCHIVE] proxy failed")
                logger.info(
                    "[ARCHIVE] proxy failed, fallback direct (%s)",
                    exc,
                )

        response = self._session_get(url, **kwargs)
        logger.info("[ARCHIVE] direct success")
        return response

    def _perform_asset_get(self, url: str, kwargs: dict[str, Any]) -> Any:
        """
        Concurrent-safe asset GET (module-level curl_cffi, not HTML Session).

        Proxy → direct fallback mirrors the HTML path without the limiter.
        """
        if self._proxies:
            try:
                return curl_requests.get(
                    url, **kwargs, proxies=self._proxies
                )
            except _PROXY_TRANSPORT_ERRORS as exc:
                logger.debug(
                    "[ARCHIVE ASSET] proxy failed, fallback direct (%s)",
                    exc,
                )
        return curl_requests.get(url, **kwargs)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def archive(
        self,
        published_file_id: str | int,
        info_dir: str | Path,
        *,
        overwrite: bool = True,
        metadata: ModMetadata | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> Path:
        """
        Mirror the real Workshop page under *info_dir*.

        Returns path to ``index.html``. On total HTML fetch failure, writes a
        minimal stub (not a JSON-templated facsimile of the Workshop UI).

        Steam HTML GETs are single-flight via ``SteamArchiveLimiter``
        (not ``net_workers``). *on_status*: ``start`` / ``ok`` / ``fail`` /
        ``rate_limited``.
        """
        _ARCHIVE_SEMAPHORE.acquire()
        prev_mod = _tls_mod_id()
        try:
            mod_id = str(published_file_id)
            self._active_mod_id = mod_id
            _set_tls_mod_id(mod_id)
            self._request_count = 0
            if on_status is not None:
                try:
                    on_status("start")
                except Exception:  # noqa: BLE001
                    pass
            path = self._archive_body(
                published_file_id,
                info_dir,
                overwrite=overwrite,
                metadata=metadata,
            )
            if on_status is not None:
                try:
                    status = read_archive_status(info_dir)
                    if status and status.get("archive_failed_reason") == _RATE_LIMITED_REASON:
                        on_status("rate_limited")
                    elif _has_successful_offline_page(Path(path)):
                        on_status("ok")
                    else:
                        on_status("fail")
                except Exception:  # noqa: BLE001
                    pass
            return path
        finally:
            _set_tls_mod_id(prev_mod)
            _ARCHIVE_SEMAPHORE.release()

    def _archive_body(
        self,
        published_file_id: str | int,
        info_dir: str | Path,
        *,
        overwrite: bool = True,
        metadata: ModMetadata | None = None,
    ) -> Path:
        info_dir = Path(info_dir)
        info_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = info_dir / DEFAULT_ASSETS_DIR
        assets_dir.mkdir(parents=True, exist_ok=True)

        index_path = info_dir / DEFAULT_INDEX_NAME
        if index_path.exists() and not overwrite:
            return index_path

        if is_archive_globally_blocked():
            return self._handle_rate_limited(
                info_dir,
                index_path,
                published_file_id,
                metadata=metadata,
                error=RATE_LIMIT_USER_MESSAGE,
            )

        write_last_archive_attempt(info_dir, failed=False)
        page_url = WORKSHOP_PAGE_URL.format(id=published_file_id)
        # Guard against parameter / URL drift in logs and fetches.
        url_id = _workshop_id_from_url(page_url)
        mod_id = str(published_file_id)
        if url_id and url_id != mod_id:
            logger.error(
                "[STEAM ARCHIVE] mod_id/url_id mismatch mod_id=%s url_id=%s url=%s",
                mod_id,
                url_id,
                page_url,
            )
            page_url = WORKSHOP_PAGE_URL.format(id=mod_id)

        try:
            t_html0 = time.monotonic()
            html_text = self._fetch_main_html(page_url)
            html_elapsed = time.monotonic() - t_html0
            soup = _parse_workshop_html(html_text)
            self._strip_noise(soup)
            t_assets0 = time.monotonic()
            asset_stats_raw = self._rewrite_and_download_assets(
                soup, page_url, assets_dir
            )
            asset_stats: dict[str, int] = (
                asset_stats_raw if isinstance(asset_stats_raw, dict) else {}
            )
            assets_elapsed = time.monotonic() - t_assets0
            self._inject_offline_banner(soup, published_file_id, page_url)
            self._write_atomic(index_path, str(soup))
            # Clear prior rate-limit marker on success.
            status_path = info_dir / _ARCHIVE_STATUS_NAME
            try:
                if status_path.is_file():
                    status_path.unlink()
            except OSError:
                pass
            logger.info(
                "Archived live Workshop page -> %s "
                "(html=%.2fs assets=%.2fs unique_assets=%s "
                "top_ok=%s top_fail=%s workers=%s cache=%s)",
                index_path,
                html_elapsed,
                assets_elapsed,
                asset_stats.get("unique", 0),
                asset_stats.get("ok", 0),
                asset_stats.get("fail", 0),
                GLOBAL_ASSET_WORKERS,
                get_asset_cache_stats(),
            )
            return index_path
        except Exception as exc:  # noqa: BLE001
            write_last_archive_attempt(info_dir, failed=True)
            # Global fuse (consecutive HTML 429) → rate_limited status + keep page.
            # A single Mod's retryable 429 exhaustion must NOT mark the whole queue.
            if isinstance(exc, ArchiveRateLimitedError) or (
                _is_rate_limited_error(exc) and is_archive_globally_blocked()
            ):
                return self._handle_rate_limited(
                    info_dir,
                    index_path,
                    published_file_id,
                    metadata=metadata,
                    error=str(exc),
                )
            logger.warning(
                "Live Workshop archive failed for %s (%s); writing minimal stub",
                published_file_id,
                exc,
            )
            if _has_successful_offline_page(index_path):
                logger.warning(
                    "Keeping existing offline page for %s after transient failure",
                    published_file_id,
                )
                return index_path
            return self.write_fallback_page(
                info_dir,
                published_file_id,
                metadata=metadata,
                error=str(exc),
            )

    def _handle_rate_limited(
        self,
        info_dir: Path,
        index_path: Path,
        published_file_id: str | int,
        *,
        metadata: ModMetadata | None,
        error: str,
    ) -> Path:
        """
        On HTTP 429: record ``archive_failed_reason=rate_limited``.

        Never overwrite an existing successful offline page with a stub.
        """
        write_archive_status(
            info_dir,
            reason=_RATE_LIMITED_REASON,
            published_file_id=published_file_id,
            detail=error,
        )
        if _has_successful_offline_page(index_path):
            logger.warning(
                "Rate limited for %s; keeping existing offline page %s",
                published_file_id,
                index_path,
            )
            return index_path

        logger.warning(
            "Rate limited for %s; writing lightweight status (no success page to keep)",
            published_file_id,
        )
        # No usable page yet — write a minimal stub so callers still get index.html.
        return self.write_fallback_page(
            info_dir,
            published_file_id,
            metadata=metadata,
            error=f"{_RATE_LIMITED_REASON}: {error}",
        )

    def ensure_offline_page(
        self,
        info_dir: str | Path,
        published_file_id: str | int,
        *,
        metadata: ModMetadata | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> Path:
        """
        Guarantee ``index.html`` exists.

        Successful cached pages are returned without a Steam request.
        Stub pages inside the retry cooldown are returned as-is (no network).
        Global 429 block returns without issuing another GET.
        """
        info_dir = Path(info_dir)
        info_dir.mkdir(parents=True, exist_ok=True)
        index_path = info_dir / DEFAULT_INDEX_NAME
        if index_path.is_file():
            try:
                if index_path.stat().st_size > 0 and not is_stub_offline_page(index_path):
                    if on_status is not None:
                        try:
                            on_status("ok")
                        except Exception:  # noqa: BLE001
                            pass
                    return index_path
                if (
                    index_path.stat().st_size > 0
                    and is_stub_offline_page(index_path)
                    and is_archive_cooldown_active(info_dir)
                ):
                    logger.info(
                        "Stub offline page for %s still in cooldown; skipping re-archive",
                        published_file_id,
                    )
                    return index_path
            except OSError:
                pass
        if is_archive_globally_blocked():
            if on_status is not None:
                try:
                    on_status("rate_limited")
                except Exception:  # noqa: BLE001
                    pass
            return self._handle_rate_limited(
                info_dir,
                index_path,
                published_file_id,
                metadata=metadata,
                error=RATE_LIMIT_USER_MESSAGE,
            )
        return self.archive(
            published_file_id,
            info_dir,
            overwrite=True,
            metadata=metadata,
            on_status=on_status,
        )

    def write_fallback_page(
        self,
        info_dir: str | Path,
        published_file_id: str | int,
        *,
        metadata: ModMetadata | None = None,
        error: str | None = None,
    ) -> Path:
        """Minimal stub when the real Workshop page cannot be downloaded."""
        info_dir = Path(info_dir)
        info_dir.mkdir(parents=True, exist_ok=True)

        title = "Steam Workshop"
        if metadata and metadata.title:
            title = metadata.title
        workshop = WORKSHOP_PAGE_URL.format(id=published_file_id)
        err = html.escape(error or "network error")
        title_e = html.escape(title)
        mid = html.escape(str(published_file_id))
        url_e = html.escape(workshop)

        stub = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_e} — Offline (stub)</title>
<style>
  body {{
    margin: 0; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    background: #1b2838; color: #c7d5e0; padding: 40px 24px; line-height: 1.5;
  }}
  .box {{
    max-width: 640px; margin: 0 auto; background: #171a21;
    border: 1px solid #2c4054; border-radius: 8px; padding: 24px;
  }}
  h1 {{ color: #66c0f4; font-size: 20px; margin: 0 0 12px; }}
  p {{ margin: 8px 0; color: #8f98a0; font-size: 14px; }}
  a {{ color: #66c0f4; }}
  code {{ color: #acb2b8; }}
</style>
</head>
<body>
  <div class="box">
    <h1>{title_e}</h1>
    <p>未能下载完整的 Steam 创意工坊原网页（网络超时或被拦截）。</p>
    <p>Mod ID: <code>{mid}</code></p>
    <p>原因: <code>{err}</code></p>
    <p>联网后可重新同步，或在浏览器打开：
       <a href="{url_e}">{url_e}</a></p>
  </div>
</body>
</html>
"""
        index_path = info_dir / DEFAULT_INDEX_NAME
        self._write_atomic(index_path, stub)
        logger.info("Wrote minimal offline stub -> %s", index_path)
        return index_path

    # ------------------------------------------------------------------
    # Fetch / parse
    # ------------------------------------------------------------------

    def _fetch_main_html(self, page_url: str) -> str:
        last_exc: Exception | None = None
        max_attempts = max(int(MAIN_PAGE_RETRIES), int(HTML_429_MAX_ATTEMPTS))
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._http_get(page_url, allow_redirects=True)
                response.raise_for_status()
                # curl_cffi exposes charset_encoding; requests uses apparent_encoding
                encoding = (
                    getattr(response, "charset_encoding", None)
                    or getattr(response, "apparent_encoding", None)
                    or "utf-8"
                )
                response.encoding = encoding
                text = response.text or ""
                if len(text) < 200:
                    raise RuntimeError("Workshop HTML response too short")
                return text
            except ArchiveRateLimitedError:
                # Global fuse armed (consecutive HTML 429 threshold).
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if STEAM_ARCHIVE_LIMITER.is_blocked():
                    raise ArchiveRateLimitedError(RATE_LIMIT_USER_MESSAGE) from exc
                if _is_rate_limited_error(exc):
                    # Single / early HTML 429 — backoff and retry this Mod.
                    if attempt < max_attempts:
                        delay = float(HTML_429_BACKOFF_BASE_SEC) * (2 ** (attempt - 1))
                        logger.info(
                            "[STEAM ARCHIVE] HTML 429 retry %s/%s after %.1fs url=%s",
                            attempt,
                            max_attempts,
                            delay,
                            page_url,
                        )
                        time.sleep(delay)
                        continue
                    # Exhausted retries without global block — fail this Mod only.
                    raise
                logger.debug(
                    "Workshop HTML fetch attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                if attempt < max_attempts:
                    time.sleep(0.6 * attempt)
        assert last_exc is not None
        raise last_exc

    def _strip_noise(self, soup: BeautifulSoup) -> None:
        """Drop scripts / embeds; keep Steam layout + CSS for authentic look."""
        for selector in (
            "script",
            "iframe",
            "noscript",
            ".agegate_text_container",
            "#cookiePrefPopup",
        ):
            for node in soup.select(selector):
                node.decompose()

    def _inject_offline_banner(
        self,
        soup: BeautifulSoup,
        published_file_id: str | int,
        page_url: str,
    ) -> None:
        body = soup.body
        if body is None:
            return
        banner = soup.new_tag("div")
        banner["id"] = "smm-offline-banner"
        banner["style"] = (
            "background:#1b2838;color:#c7d5e0;padding:10px 16px;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
            "border-bottom:2px solid #66c0f4;position:relative;z-index:99999;"
        )
        banner.string = (
            f"Offline archive · Workshop ID {published_file_id} · "
            f"Mirrored from {page_url}"
        )
        body.insert(0, banner)

    # ------------------------------------------------------------------
    # Asset localization
    # ------------------------------------------------------------------

    def _rewrite_and_download_assets(
        self,
        soup: BeautifulSoup,
        page_url: str,
        assets_dir: Path,
    ) -> dict[str, int]:
        """
        Localize CSS / fonts / images.

        Uses a per-Mod ThreadPoolExecutor for scheduling, but every asset GET
        acquires ``_GLOBAL_ASSET_SEMAPHORE`` (``GLOBAL_ASSET_WORKERS``). All Mods
        therefore share one process-wide cap of 6 CDN downloads — never 3×6.
        Soup mutations happen on this thread only after futures complete.
        """
        css_count = 0
        img_count = 0
        font_count = 0
        max_fonts = 40
        seen: dict[str, str] = {}
        seen_lock = threading.Lock()
        stats = {"ok": 0, "fail": 0}
        stats_lock = threading.Lock()

        css_jobs: list[tuple[Tag, str]] = []
        font_jobs: list[tuple[Tag, str]] = []
        img_jobs: list[tuple[Tag, list[str]]] = []

        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            rel = " ".join(link.get("rel") or []).lower()
            href = link.get("href")
            if not href:
                continue
            as_attr = (link.get("as") or "").lower()

            if "stylesheet" in rel or as_attr == "style":
                if css_count >= self.max_css:
                    link.decompose()
                    continue
                css_jobs.append((link, str(href)))
                css_count += 1
                continue

            if "font" in rel or as_attr == "font":
                if font_count >= max_fonts:
                    continue
                font_jobs.append((link, str(href)))
                font_count += 1
                continue

        for img in list(soup.find_all("img")):
            if not isinstance(img, Tag):
                continue
            if img_count >= self.max_images:
                break

            candidates: list[str] = []
            for attr in ("src", "data-src", "data-lazy-src"):
                val = img.get(attr)
                if val:
                    candidates.append(str(val))
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                first = str(srcset).split(",")[0].strip().split()[0]
                if first:
                    candidates.append(first)
            if not candidates:
                continue
            img_jobs.append((img, candidates))
            img_count += 1

        def _tracked_download(raw: str, base: str) -> str | None:
            local = self._download_asset(
                raw, base, assets_dir, seen, seen_lock=seen_lock
            )
            with stats_lock:
                if local:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
            return local

        def _download_first(candidates: list[str]) -> str | None:
            for raw in candidates:
                local = _tracked_download(raw, page_url)
                if local:
                    return local
            return None

        # Pool size matches the global cap; excess threads just wait on the
        # semaphore when other Mods are also downloading.
        workers = max(1, int(GLOBAL_ASSET_WORKERS))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            css_futs = {
                pool.submit(_tracked_download, href, page_url): (link, href)
                for link, href in css_jobs
            }
            font_futs = {
                pool.submit(_tracked_download, href, page_url): (link, href)
                for link, href in font_jobs
            }
            img_futs = {
                pool.submit(_download_first, candidates): (img, candidates)
                for img, candidates in img_jobs
            }

            for fut in as_completed(css_futs):
                link, href = css_futs[fut]
                local = fut.result()
                if local:
                    link["href"] = local
                else:
                    link["href"] = urljoin(page_url, href)

            for fut in as_completed(font_futs):
                link, _href = font_futs[fut]
                local = fut.result()
                if local:
                    link["href"] = local

            for fut in as_completed(img_futs):
                img, _candidates = img_futs[fut]
                local = fut.result()
                if local:
                    img["src"] = local
                    for attr in (
                        "data-src",
                        "data-lazy-src",
                        "srcset",
                        "data-srcset",
                    ):
                        if attr in img.attrs:
                            del img.attrs[attr]

        # Inline style url(...) on elements (best-effort, limited)
        for node in soup.find_all(style=True):
            if not isinstance(node, Tag):
                continue
            style = str(node.get("style") or "")
            if "url(" not in style.lower():
                continue
            node["style"] = self._rewrite_css_url_refs(
                style,
                page_url,
                assets_dir,
                seen,
                download=True,
                seen_lock=seen_lock,
            )

        stats["unique"] = len(seen)
        return stats

    def _download_asset(
        self,
        raw_url: str,
        page_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        *,
        seen_lock: threading.Lock | None = None,
    ) -> str | None:
        absolute = self._absolutize(raw_url, page_url)
        if absolute is None:
            return None

        lock = seen_lock
        if lock is not None:
            with lock:
                if absolute in seen:
                    return seen[absolute]
        elif absolute in seen:
            return seen[absolute]

        parsed = urlparse(absolute)
        # Mod-local filename keeps sha1[:16] (unchanged href/src rewrite contract).
        ext_guess = _guess_extension(parsed.path, "")
        digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:16]
        cache_key = _asset_cache_key(absolute)

        # Serialize per-URL so concurrent Mods share one download into the cache.
        with _cache_lock_for(cache_key):
            if lock is not None:
                with lock:
                    if absolute in seen:
                        return seen[absolute]
            elif absolute in seen:
                return seen[absolute]

            ext = ext_guess
            filename = f"{digest}{ext}"
            dest = assets_dir / filename
            cached = _find_cached_asset(cache_key, ext_guess)

            if cached is not None:
                # Prefer cached suffix when URL had no useful extension.
                if cached.suffix and (not ext or ext == ".bin"):
                    ext = cached.suffix.lower()
                    filename = f"{digest}{ext}"
                    dest = assets_dir / filename
                try:
                    if not dest.exists():
                        _copy_file_atomic(cached, dest)
                    with _ASSET_CACHE_STATS_LOCK:
                        _ASSET_CACHE_STATS["hit"] += 1
                    logger.debug("Asset cache HIT %s -> %s", absolute, cached.name)
                except OSError as exc:
                    logger.debug("Asset cache copy failed %s: %s", cached, exc)
                    return None
            else:
                response: Any = None
                try:
                    response = self._http_get_asset(absolute, stream=True)
                    response.raise_for_status()
                except _CURL_HTTP_ERRORS as exc:
                    logger.debug("Asset download failed %s: %s", absolute, exc)
                    close = (
                        getattr(response, "close", None)
                        if response is not None
                        else None
                    )
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            pass
                    return None

                content_type = (
                    (response.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                )
                ext = _guess_extension(parsed.path, content_type)
                filename = f"{digest}{ext}"
                dest = assets_dir / filename
                cache_path = asset_cache_dir() / f"{cache_key}{ext}"

                try:
                    if not dest.exists():
                        self._stream_to_file(response, dest)
                    else:
                        response.close()
                    # Store raw bytes (before CSS localization) for reuse.
                    if not cache_path.exists():
                        _copy_file_atomic(dest, cache_path)
                    with _ASSET_CACHE_STATS_LOCK:
                        _ASSET_CACHE_STATS["miss"] += 1
                    logger.debug(
                        "Asset cache MISS %s -> %s", absolute, cache_path.name
                    )
                except (*_CURL_HTTP_ERRORS, OSError) as exc:
                    logger.debug("Failed writing asset %s: %s", dest, exc)
                    dest.unlink(missing_ok=True)
                    return None

            if ext == ".css":
                try:
                    self._localize_css_file(
                        dest, absolute, assets_dir, seen, seen_lock=lock
                    )
                except OSError:
                    pass

            relative = f"./{DEFAULT_ASSETS_DIR}/{filename}"
            if lock is not None:
                with lock:
                    existing = seen.get(absolute)
                    if existing:
                        return existing
                    seen[absolute] = relative
            else:
                seen[absolute] = relative
            return relative

    def _stream_to_file(self, response: Any, dest: Path) -> None:
        """Write via temp file then replace — avoids partial files under concurrency.

        Uses ``response.iter_content()`` without ``chunk_size``: curl_cffi accepts
        the arg for requests compatibility but ignores it and emits a warning.
        Streaming still requires ``stream=True`` on the GET (see ``_download_asset``).
        """
        assets_dir = dest.parent
        assets_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=dest.stem + "_", dir=str(assets_dir))
        tmp_path = Path(tmp_name)
        try:
            size = 0
            with open(fd, "wb") as fh:
                # Prefer iter_content (curl_cffi streaming API); fall back to
                # reading ``content`` if a Response lacks the iterator.
                if hasattr(response, "iter_content"):
                    chunks = response.iter_content()
                else:
                    body = response.content or b""
                    chunks = [body] if body else []
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_ASSET_BYTES:
                        raise OSError("asset exceeds size limit")
                    fh.write(chunk)
            tmp_path.replace(dest)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _localize_css_file(
        self,
        css_path: Path,
        css_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        *,
        seen_lock: threading.Lock | None = None,
    ) -> None:
        text = css_path.read_text(encoding="utf-8", errors="ignore")
        rewritten = self._rewrite_css_url_refs(
            text,
            css_url,
            assets_dir,
            seen,
            download=True,
            peer_relative=True,
            seen_lock=seen_lock,
        )
        self._write_atomic(css_path, rewritten)

    def _rewrite_css_url_refs(
        self,
        text: str,
        base_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        *,
        download: bool,
        peer_relative: bool = False,
        seen_lock: threading.Lock | None = None,
    ) -> str:
        def replacer(match: re.Match[str]) -> str:
            raw = match.group(1).strip(" '\"")
            if raw.startswith("data:") or raw.startswith("#"):
                return match.group(0)
            if not download:
                return match.group(0)
            local = self._download_asset(
                raw, base_url, assets_dir, seen, seen_lock=seen_lock
            )
            if not local:
                return match.group(0)
            if peer_relative:
                return f"url({Path(local).name})"
            return f"url({local})"

        return re.sub(r"url\(([^)]+)\)", replacer, text)

    @staticmethod
    def _absolutize(raw_url: str, page_url: str) -> str | None:
        raw = unquote(raw_url.strip())
        if not raw or raw.startswith("data:") or raw.startswith("javascript:"):
            return None
        absolute = urljoin(page_url, raw)
        if absolute.startswith("//"):
            absolute = "https:" + absolute
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            return None
        return absolute

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".html", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise


def backfill_offline_pages(target_root: str | Path) -> int:
    """
    For every managed Mod missing ``.info/index.html``, attempt a live Workshop
    mirror (then minimal stub on failure). Returns number of pages created.
    """
    from .file_ops import ModFileManager

    manager = ModFileManager(target_root)
    created = 0
    with OfflinePageArchiver() as archiver:
        for folder in manager.list_managed_mods():
            info = manager.ensure_info_dir(folder)
            index = info / DEFAULT_INDEX_NAME
            if index.is_file():
                continue
            meta = manager.load_metadata(folder)
            pub_id = (
                meta.published_file_id
                if meta and meta.published_file_id
                else folder.name
            )
            path = archiver.archive(pub_id, info, overwrite=True, metadata=meta)
            if meta:
                meta.offline_page_path = str(path)
                manager.save_metadata(meta, folder)
            created += 1
    return created


def _guess_extension(path: str, content_type: str) -> str:
    suffix = Path(unquote(path)).suffix.lower().split("?")[0]
    if suffix in {
        ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
        ".woff2", ".woff", ".ttf", ".eot",
    }:
        return suffix
    mapping = {
        "text/css": ".css",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "font/woff2": ".woff2",
        "font/woff": ".woff",
        "application/font-woff": ".woff",
    }
    return mapping.get(content_type.lower(), ".bin")
