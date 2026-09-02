"""Tests: SteamArchiveLimiter, 429 block, cookie, no fast retry."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services import archive as archive_mod
from services.archive import (
    ARCHIVE_BLOCK_AFTER_429_SEC,
    ARCHIVE_MAX_CONCURRENCY,
    ARCHIVE_REQUEST_INTERVAL_SECONDS,
    GLOBAL_ASSET_WORKERS,
    HTML_429_CONSECUTIVE_BEFORE_BLOCK,
    ArchiveRateLimitedError,
    OfflinePageArchiver,
    RATE_LIMIT_USER_MESSAGE,
    SteamArchiveLimiter,
    SteamArchiveRateLimiter,
    SteamArchiveSyncContext,
    _RATE_LIMITED_REASON,
    is_stub_offline_page,
    write_archive_status,
)


URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546"
LIVE_HTML = """<!DOCTYPE html><html><body>
<div id="smm-offline-banner">Offline archive · Workshop ID 3761838546</div>
<div class="workshopItemTitle">Live Mod</div>
</body></html>
"""


def _ok(status: int = 200, *, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        err = Exception(f"HTTP Error {status}: Too Many Requests")
        err.response = resp  # type: ignore[attr-defined]
        resp.raise_for_status.side_effect = err
    resp.text = "<html>" + ("x" * 300) + "</html>"
    resp.charset_encoding = "utf-8"
    return resp


@pytest.fixture(autouse=True)
def _isolate_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    monkeypatch.setattr(archive_mod, "HTML_429_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(archive_mod, "HTML_429_SOFT_COOLDOWN_SEC", 0.0)
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)
    monkeypatch.setattr(
        archive_mod, "_ARCHIVE_SEMAPHORE", threading.Semaphore(ARCHIVE_MAX_CONCURRENCY)
    )


def test_request_interval_constant_browser_like() -> None:
    assert ARCHIVE_REQUEST_INTERVAL_SECONDS >= 3.0
    assert ARCHIVE_MAX_CONCURRENCY == 1
    assert ARCHIVE_BLOCK_AFTER_429_SEC == 30 * 60


def test_throttle_between_concurrent_archive_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two GETs through the global limiter must be spaced by min_interval."""
    lim = SteamArchiveLimiter(min_interval=0.08)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    session = MagicMock()
    session.cookies = {}
    session.get.side_effect = lambda *a, **k: _ok(200)

    t0 = time.perf_counter()
    with OfflinePageArchiver(session=session) as archiver:
        archiver._http_get(URL)
        archiver._http_get(URL + "&x=1")
    elapsed = time.perf_counter() - t0

    assert session.get.call_count == 2
    assert elapsed >= 0.07


def test_429_sets_global_block_no_fast_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "HTML_429_BACKOFF_BASE_SEC", 0.0)

    session = MagicMock()
    session.cookies = {}
    # First HTML_429_CONSECUTIVE_BEFORE_BLOCK-1 are retryable; last arms fuse.
    session.get.side_effect = [
        _ok(429) for _ in range(HTML_429_CONSECUTIVE_BEFORE_BLOCK)
    ] + [_ok(200)]

    with OfflinePageArchiver(session=session) as archiver:
        # Early 429s are retryable (no global block).
        with pytest.raises(Exception) as soft:
            archiver._http_get(URL)
        assert not lim.is_blocked()
        assert "429" in str(soft.value)

        # Burn remaining consecutive budget.
        for _ in range(HTML_429_CONSECUTIVE_BEFORE_BLOCK - 2):
            with pytest.raises(Exception):
                archiver._http_get(URL + "&x=soft")
            assert not lim.is_blocked()

        with pytest.raises(ArchiveRateLimitedError) as ei:
            archiver._http_get(URL + "&x=block")
        assert RATE_LIMIT_USER_MESSAGE in str(ei.value)
        assert lim.is_blocked()
        # While blocked, no further network.
        with pytest.raises(ArchiveRateLimitedError):
            archiver._http_get(URL + "&x=1")

    assert session.get.call_count == HTML_429_CONSECUTIVE_BEFORE_BLOCK
    assert lim.archive_blocked_until > time.time()


def test_single_html_429_resets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    session = MagicMock()
    session.cookies = {}
    session.get.side_effect = [_ok(429), _ok(200), _ok(200)]

    with OfflinePageArchiver(session=session) as archiver:
        with pytest.raises(Exception):
            archiver._http_get(URL)
        assert lim.consecutive_html_429 == 1
        assert not lim.is_blocked()
        archiver._http_get(URL + "&ok=1")
        assert lim.consecutive_html_429 == 0
        archiver._http_get(URL + "&ok=2")

    assert session.get.call_count == 3
    assert not lim.is_blocked()


def test_global_asset_semaphore_caps_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process-wide asset GETs never exceed GLOBAL_ASSET_WORKERS."""
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    current = 0
    peak = 0
    lock = threading.Lock()

    def slow_transport(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        time.sleep(0.05)
        with lock:
            current -= 1
        return _ok(200)

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", slow_transport)

    def worker() -> None:
        with OfflinePageArchiver(session=MagicMock()) as archiver:
            for i in range(4):
                archiver._http_get_asset(
                    f"https://community.akamai.steamstatic.com/{i}.png"
                )

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak <= GLOBAL_ASSET_WORKERS
    assert peak >= 2


def test_asset_http_error_does_not_leak_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404/5xx must release the global asset slot (no hang after N failures)."""
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    def bad_transport(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        return _ok(404)

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", bad_transport)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        for i in range(GLOBAL_ASSET_WORKERS + 3):
            with pytest.raises(Exception):
                archiver._http_get_asset(
                    f"https://community.akamai.steamstatic.com/missing{i}.png",
                    stream=True,
                )
        # If slots leaked, this would block forever.
        monkeypatch.setattr(
            OfflinePageArchiver,
            "_perform_asset_get",
            lambda self, url, kwargs: _ok(200),
        )
        resp = archiver._http_get_asset(
            "https://community.akamai.steamstatic.com/ok.png", stream=True
        )
        resp.close()


def test_429_does_not_overwrite_successful_offline_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ".info"
    info.mkdir()
    index = info / "index.html"
    index.write_text(LIVE_HTML, encoding="utf-8")

    monkeypatch.setattr(archive_mod, "HTML_429_BACKOFF_BASE_SEC", 0.0)

    session = MagicMock()
    session.cookies = {}
    session.get.side_effect = lambda *_a, **_k: _ok(429)

    with OfflinePageArchiver(session=session) as archiver:
        result = archiver.archive("3761838546", info, overwrite=True)

    assert result.path == index
    assert result.outcome in ("failed", "rate_limited")
    assert index.read_text(encoding="utf-8") == LIVE_HTML
    # Transient 429 without global fuse must not wipe a good page.
    assert not is_stub_offline_page(index)


def test_cookie_header_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.cookies = {}
    captured: list[dict[str, Any]] = []

    def capture(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs)
        return _ok(200)

    session.get.side_effect = capture
    cookie = "steamLoginSecure=abc; sessionid=xyz; steamCountry=CN%7C1"

    with OfflinePageArchiver(session=session, steam_cookie=cookie) as archiver:
        archiver._http_get(URL)

    assert captured
    assert captured[0]["headers"]["Cookie"] == cookie


def test_anonymous_when_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.cookies = {}
    captured: list[dict[str, Any]] = []

    def capture(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs)
        return _ok(200)

    session.get.side_effect = capture

    with OfflinePageArchiver(session=session, steam_cookie="") as archiver:
        archiver._http_get(URL)

    assert "Cookie" not in captured[0]["headers"]


def test_log_mod_id_matches_url_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = MagicMock()
    session.cookies = {"sessionid": "abc"}
    session.get.side_effect = lambda *a, **k: _ok(200)

    with caplog.at_level(logging.INFO, logger=archive_mod.logger.name):
        with OfflinePageArchiver(session=session) as archiver:
            archiver._active_mod_id = "3761838546"
            archive_mod._set_tls_mod_id("3761838546")
            archiver._http_get(URL)

    steam_lines = [r.message for r in caplog.records if "[STEAM ARCHIVE]" in r.message]
    assert steam_lines
    assert any("mod_id=3761838546" in line for line in steam_lines)
    assert any("url_id=3761838546" in line for line in steam_lines)


def test_archive_sets_tls_mod_id_for_matching_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = MagicMock()
    session.cookies = {}

    html = "<html>" + ("y" * 300) + '<div id="smm-offline-banner">x</div></html>'

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        resp = _ok(200)
        resp.text = html
        return resp

    session.get.side_effect = fake_get
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_rewrite_and_download_assets",
        lambda *a, **k: None,
    )

    with caplog.at_level(logging.INFO, logger=archive_mod.logger.name):
        with OfflinePageArchiver(session=session) as archiver:
            archiver.archive("3664026608", tmp_path / ".info", overwrite=True)

    main_logs = [
        r.message
        for r in caplog.records
        if "[STEAM ARCHIVE]" in r.message and "url_id=" in r.message
    ]
    assert main_logs
    assert "mod_id=3664026608" in main_logs[0]
    assert "url_id=3664026608" in main_logs[0]


def test_archive_concurrency_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def slow_body(self: OfflinePageArchiver, *a: Any, **k: Any) -> Path:
        nonlocal current, peak
        with lock:
            current += 1
            peak = max(peak, current)
        release.wait(timeout=2)
        with lock:
            current -= 1
        info_dir = Path(a[1])
        out = info_dir / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html>ok</html>", encoding="utf-8")
        return out

    monkeypatch.setattr(OfflinePageArchiver, "_archive_body", slow_body)

    def worker(i: int) -> None:
        info = tmp_path / f"mod{i}" / ".info"
        with OfflinePageArchiver(session=MagicMock()) as archiver:
            archiver.archive(str(1000 + i), info, overwrite=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    with lock:
        assert peak == 1
        assert current == 1
    release.set()
    for t in threads:
        t.join(timeout=3)
    assert peak == 1


def test_session_cookie_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def get(self, url: str, **kwargs: Any) -> MagicMock:
            if "sessionid" not in self.cookies:
                self.cookies["sessionid"] = "steam-test-cookie"
            return _ok(200)

        def close(self) -> None:
            return None

    session = FakeSession()
    with OfflinePageArchiver(session=session) as archiver:
        archiver._http_get(URL)
        archiver._http_get(URL + "&page=2")
    assert session.cookies.get("sessionid") == "steam-test-cookie"


def test_sync_context_shares_one_session() -> None:
    with SteamArchiveSyncContext() as ctx:
        shared = ctx.archiver._session
        assert shared is not None
        assert ctx.archiver._session is shared
    assert ctx.archiver._session is None


def test_write_archive_status_rate_limited(tmp_path: Path) -> None:
    path = write_archive_status(
        tmp_path, reason=_RATE_LIMITED_REASON, published_file_id="1", detail="429"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["archive_failed_reason"] == "rate_limited"


def test_limiter_alias() -> None:
    assert SteamArchiveRateLimiter is SteamArchiveLimiter


def test_asset_get_bypasses_html_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """CDN/asset GETs must not wait on SteamArchiveLimiter spacing."""
    lim = SteamArchiveLimiter(min_interval=5.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    calls: list[str] = []

    def fake_asset_transport(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        calls.append(url)
        return _ok(200)

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_transport)

    t0 = time.perf_counter()
    with OfflinePageArchiver(session=MagicMock()) as archiver:
        archiver._http_get_asset("https://community.akamai.steamstatic.com/a.css")
        archiver._http_get_asset("https://community.akamai.steamstatic.com/b.png")
    elapsed = time.perf_counter() - t0

    assert len(calls) == 2
    assert elapsed < 1.0
    assert not lim.is_blocked()


def test_asset_429_does_not_set_global_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lim = SteamArchiveLimiter(min_interval=0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)

    def fake_asset_transport(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        return _ok(429)

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_transport)

    with OfflinePageArchiver(session=MagicMock()) as archiver:
        with pytest.raises(archive_mod.RequestException):
            archiver._http_get_asset("https://community.akamai.steamstatic.com/x.png")
        # HTML path still allowed — no global cool-off from asset 429.
        assert not lim.is_blocked()
        session = MagicMock()
        session.cookies = {}
        session.get.side_effect = lambda *a, **k: _ok(200)
        archiver._session = session
        archiver._http_get(URL)

    assert not lim.is_blocked()
    assert session.get.call_count == 1
