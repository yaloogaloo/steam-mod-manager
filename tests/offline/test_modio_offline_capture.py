"""mod.io offline capture — Playwright runtime regression tests."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import OFFLINE_STATUS_ARCHIVED, PLATFORM_MODIO
from services import archive as archive_mod
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.offline.modio import ModioOfflineProvider, _map_modio_error
from services.offline.modio_browser_snapshot import (
    MODIO_SNAPSHOT_USER_MESSAGES,
    ModioSnapshotError,
    ModioSnapshotKind,
    _launch_chromium_browser,
    capture_modio_page_content,
    probe_modio_playwright_runtime,
    reset_modio_playwright_runtime_cache,
    validate_modio_dom,
)

_RENDERED_MODIO_HTML = """<!DOCTYPE html>
<html><head>
<title>Better Inventory UI for Baldur's Gate 3 - mod.io</title>
<link rel="stylesheet" href="https://cdn.example/modio.css">
</head><body>
<div id="root">
  <h1>Better Inventory UI</h1>
  <section class="description"><h2>Description</h2><p>Inventory UI mod.</p></section>
  <button>Subscribe</button>
</div>
</body></html>
"""

_OLD_FAKE_BROWSER_MSG = (
    "Playwright 浏览器未安装。请在终端运行: playwright install chromium"
)

_EXAMPLE_URL = "https://mod.io/g/baldursgate3/m/example-mod"


@pytest.fixture(autouse=True)
def _reset_runtime_cache() -> None:
    reset_modio_playwright_runtime_cache()
    yield
    reset_modio_playwright_runtime_cache()


@pytest.fixture(autouse=True)
def _isolate_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    cache_root = tmp_path / "asset_cache"
    cache_root.mkdir(exist_ok=True)
    monkeypatch.setattr(archive_mod, "asset_cache_dir", lambda: cache_root)
    archive_mod.reset_asset_cache_stats()


def _patch_sync_playwright(monkeypatch: pytest.MonkeyPatch, fake_pw: MagicMock) -> None:
    class _Ctx:
        def __enter__(self):
            return fake_pw

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _Ctx())


def _mock_browser_page(page: MagicMock) -> MagicMock:
    browser = MagicMock()
    context = MagicMock()
    context.new_page.return_value = page
    browser.new_context.return_value = context
    return browser


def test_playwright_runtime_can_launch() -> None:
    """Real Playwright package + Chromium binary + launch."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright package not installed")

    with sync_playwright() as playwright:
        path = str(playwright.chromium.executable_path or "")
        assert path
        assert Path(path).is_file(), f"chromium executable missing: {path}"
        browser = _launch_chromium_browser(playwright)
        browser.close()


def test_probe_browser_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "/fake/missing/chrome.exe"
    fake_pw.chromium.launch.side_effect = OSError(
        "Executable doesn't exist at /fake/missing/chrome.exe"
    )
    _patch_sync_playwright(monkeypatch, fake_pw)

    ready, message = probe_modio_playwright_runtime(force=True)
    assert ready is False
    assert message == MODIO_SNAPSHOT_USER_MESSAGES[ModioSnapshotKind.BROWSER_MISSING]


def test_probe_launch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.side_effect = RuntimeError("spawn EACCES")
    _patch_sync_playwright(monkeypatch, fake_pw)

    ready, message = probe_modio_playwright_runtime(force=True)
    assert ready is False
    assert message.startswith(
        MODIO_SNAPSHOT_USER_MESSAGES[ModioSnapshotKind.LAUNCH_FAILED]
    )
    assert "spawn EACCES" in message


def test_browser_missing_returns_playwright_browser_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "/fake/missing/chrome.exe"
    fake_pw.chromium.launch.side_effect = OSError(
        "Executable doesn't exist at /fake/missing/chrome.exe"
    )
    _patch_sync_playwright(monkeypatch, fake_pw)

    with pytest.raises(ModioSnapshotError) as exc:
        capture_modio_page_content(_EXAMPLE_URL)
    assert exc.value.kind == ModioSnapshotKind.BROWSER_MISSING
    assert exc.value.kind.value == "PLAYWRIGHT_BROWSER_MISSING"
    assert _OLD_FAKE_BROWSER_MSG not in exc.value.user_message


def test_launch_exception_returns_playwright_launch_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.side_effect = RuntimeError("spawn EACCES")
    _patch_sync_playwright(monkeypatch, fake_pw)

    with pytest.raises(ModioSnapshotError) as exc:
        capture_modio_page_content(_EXAMPLE_URL)
    assert exc.value.kind == ModioSnapshotKind.LAUNCH_FAILED
    assert "spawn EACCES" in _map_modio_error(exc.value)
    assert "浏览器未安装" not in exc.value.user_message


def test_page_load_failure_returns_playwright_page_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = MagicMock()
    page.url = _EXAMPLE_URL
    page.content.return_value = ""
    page.goto.side_effect = PlaywrightTimeoutError("navigation timeout")

    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.return_value = _mock_browser_page(page)
    _patch_sync_playwright(monkeypatch, fake_pw)

    with pytest.raises(ModioSnapshotError) as exc:
        capture_modio_page_content(_EXAMPLE_URL)
    assert exc.value.kind == ModioSnapshotKind.PAGE_LOAD_FAILED
    assert exc.value.kind.value == "PLAYWRIGHT_PAGE_LOAD_FAILED"


def test_capture_spa_unrendered(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = (
        "<!DOCTYPE html><html><head><title>mod.io</title></head>"
        '<body><div id="root"></div></body></html>'
    )
    page = MagicMock()
    page.url = _EXAMPLE_URL
    page.content.return_value = shell
    page.goto.return_value = None
    page.wait_for_timeout.return_value = None
    page.wait_for_selector.return_value = None

    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.return_value = _mock_browser_page(page)
    _patch_sync_playwright(monkeypatch, fake_pw)

    assert validate_modio_dom(shell) == "UNRENDERED_SPA_SHELL"
    with pytest.raises(ModioSnapshotError) as exc:
        capture_modio_page_content(_EXAMPLE_URL)
    assert exc.value.kind == ModioSnapshotKind.SPA_UNRENDERED


def test_capture_success_returns_html(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<!DOCTYPE html><html><head><title>Example Mod - mod.io</title></head>"
        '<body><div id="root"><h1>Example Mod</h1>'
        '<section class="description"><p>Body text.</p></section>'
        "<button>Subscribe</button></div></body></html>"
    )
    page = MagicMock()
    page.url = _EXAMPLE_URL
    page.content.return_value = html
    page.goto.return_value = None
    page.wait_for_timeout.return_value = None
    page.wait_for_selector.return_value = MagicMock()

    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.return_value = _mock_browser_page(page)
    _patch_sync_playwright(monkeypatch, fake_pw)

    out = capture_modio_page_content(_EXAMPLE_URL)
    assert "Example Mod" in out
    page.goto.assert_called_once()
    assert page.goto.call_args.kwargs.get("wait_until") == "domcontentloaded"


def test_probe_failure_does_not_block_later_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.offline.modio_browser_snapshot.probe_modio_playwright_runtime",
        lambda **kwargs: (
            False,
            MODIO_SNAPSHOT_USER_MESSAGES[ModioSnapshotKind.BROWSER_MISSING],
        ),
    )

    page = MagicMock()
    page.url = _EXAMPLE_URL
    page.content.return_value = _RENDERED_MODIO_HTML
    page.goto.return_value = None
    page.wait_for_timeout.return_value = None
    page.wait_for_selector.return_value = MagicMock()

    fake_pw = MagicMock()
    fake_pw.chromium.executable_path = "C:/browser/chrome.exe"
    fake_pw.chromium.launch.return_value = _mock_browser_page(page)
    _patch_sync_playwright(monkeypatch, fake_pw)

    out = capture_modio_page_content(_EXAMPLE_URL)
    assert "Better Inventory UI" in out


def test_retry_without_playwright_browsers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent/playwright-cache")
    calls: list[bool] = []

    def fake_open(*, clear_browsers_path: bool = False):
        calls.append(clear_browsers_path)
        if not clear_browsers_path:
            raise ModioSnapshotError(
                ModioSnapshotKind.BROWSER_MISSING,
                detail="Executable doesn't exist",
            )
        browser = MagicMock()
        page = MagicMock()
        page.url = "https://mod.io/g/x/m/y"
        page.content.return_value = _RENDERED_MODIO_HTML
        page.goto.return_value = None
        page.wait_for_timeout.return_value = None
        page.wait_for_selector.return_value = MagicMock()
        context = MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        return MagicMock(), None, browser

    monkeypatch.setattr(
        "services.offline.modio_browser_snapshot._open_chromium_browser",
        fake_open,
    )
    monkeypatch.setattr(
        "services.offline.modio_browser_snapshot._close_chromium_browser",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "services.offline.modio_browser_snapshot.validate_modio_dom",
        lambda _html: None,
    )

    out = capture_modio_page_content(_EXAMPLE_URL)
    assert "Better Inventory UI" in out
    assert calls == [False, True]


def test_provider_maps_runtime_error_without_fake_browser_missing() -> None:
    err = ModioSnapshotError(
        ModioSnapshotKind.LAUNCH_FAILED,
        detail="access denied",
    )
    assert "access denied" in _map_modio_error(err)
    assert "浏览器未安装" not in _map_modio_error(err)


def _register_modio_mod(db: DatabaseManager, lib: Path, url: str) -> tuple[str, Path]:
    folder = lib / "Game" / "better-inventory-ui1"
    info_dir = folder / ".info"
    info_dir.mkdir(parents=True)
    (info_dir / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": "",
                "title": "Better Inventory UI",
                "url": url,
                "source_url": url,
                "source_type": "modio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    row = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="better-inventory-ui1",
        source_url=url,
        title="Better Inventory UI",
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )
    mid = row.mod_id
    data = json.loads((info_dir / "metadata.json").read_text(encoding="utf-8"))
    data["published_file_id"] = mid
    (info_dir / "metadata.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))
    return mid, folder


def _fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "text/css"}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=[b"body{color:#111}"])
    resp.close = MagicMock()
    resp.content = b"body{color:#111}"
    return resp


@pytest.mark.network
def test_live_modio_offline_archive_e2e(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full mod.io offline archive produces .info/offline/index.html."""
    pytest.importorskip("playwright")

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "live_modio_offline.db")
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)

    caplog.set_level(logging.WARNING, logger="services.offline.modio_browser_snapshot")
    caplog.set_level(logging.WARNING, logger="services.offline.modio")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", _fake_asset_get)

    provider = ModioOfflineProvider()
    result1 = provider.update_offline_page(
        mid,
        managed_path=folder,
        library_root=lib,
    )
    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    assert result1.status == OFFLINE_STATUS_ARCHIVED
    assert index.is_file()
    html1 = index.read_text(encoding="utf-8")
    assert len(html1) > 500
    assert "mod.io" in html1.lower() or "inventory" in html1.lower()
    assert _OLD_FAKE_BROWSER_MSG not in html1

    result2 = provider.update_offline_page(
        mid,
        managed_path=folder,
        library_root=lib,
    )
    assert result2.status == OFFLINE_STATUS_ARCHIVED
    assert index.is_file()
    assert len(index.read_text(encoding="utf-8")) > 500

    log_text = caplog.text
    assert "[MODIO_OFFLINE] playwright_start" in log_text
    assert "[MODIO_OFFLINE] chromium_path=" in log_text
    assert "[MODIO_OFFLINE] launch_success" in log_text
    assert "[MODIO_OFFLINE] snapshot_success" in log_text
    assert _OLD_FAKE_BROWSER_MSG not in log_text

    DatabaseManager.reset_instance()
