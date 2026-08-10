"""Browser snapshot wait strategy, Cloudflare detection, Nexus fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import OFFLINE_STATUS_FAILED, PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME
from services.offline.browser_snapshot.manager import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
)
from services.offline.browser_snapshot.playwright_capture import (
    DEFAULT_WAIT_UNTIL,
    ERROR_BLOCKED_BY_ANTI_BOT,
    ERROR_LOGIN_REQUIRED,
    ERROR_NETWORK_TIMEOUT,
    PlaywrightCapture,
    PlaywrightCaptureError,
    classify_playwright_exception,
    detect_page_block_reason,
)
from services.offline.nexus_manual import NexusManualOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_wait.db")
    yield manager
    DatabaseManager.reset_instance()


def test_default_wait_until_is_domcontentloaded_not_networkidle() -> None:
    cap = PlaywrightCapture()
    assert cap.wait_until == "domcontentloaded"
    assert DEFAULT_WAIT_UNTIL == "domcontentloaded"
    # Explicit networkidle is coerced away.
    coerced = PlaywrightCapture(wait_until="networkidle")
    assert coerced.wait_until == "domcontentloaded"


def test_goto_uses_domcontentloaded_and_skips_networkidle() -> None:
    """Simulate Playwright page: networkidle would never finish; DCL succeeds."""
    page_url = "https://www.nexusmods.com/palworld/mods/96"
    html = (
        "<!DOCTYPE html><html><body>"
        "<h1>Nexus Mod Page</h1>"
        "</body></html>"
    )

    class FakePage:
        def __init__(self) -> None:
            self.goto_kwargs: dict[str, Any] = {}
            self.wait_calls: list[str] = []
            self._timeout = 60_000

        def set_default_timeout(self, ms: int) -> None:
            self._timeout = ms

        def goto(self, url: str, **kwargs: Any) -> None:
            self.goto_kwargs = dict(kwargs)
            wait = kwargs.get("wait_until")
            if wait == "networkidle":
                raise TimeoutError("Page.goto: Timeout 60000ms exceeded. waiting until networkidle")
            if wait != "domcontentloaded":
                raise AssertionError(f"unexpected wait_until={wait!r}")
            # success for domcontentloaded

        def wait_for_timeout(self, ms: int) -> None:
            self.wait_calls.append(f"timeout:{ms}")

        def wait_for_function(self, expr: str, **kwargs: Any) -> None:
            self.wait_calls.append(f"fn:{expr}")

        def evaluate(self, script: str) -> Any:
            if "readyState" in script and "outerHTML" not in script:
                return "complete"
            return {
                "html": html,
                "urls": [],
                "sheets": [],
                "pageUrl": page_url,
                "readyState": "complete",
            }

        def content(self) -> str:
            return html

        @property
        def url(self) -> str:
            return page_url

    class FakeContext:
        def __init__(self, page: FakePage) -> None:
            self._page = page

        def new_page(self) -> FakePage:
            return self._page

        def close(self) -> None:
            return None

    class FakeBrowser:
        def __init__(self, page: FakePage) -> None:
            self._page = page
            self.context_kwargs: dict[str, Any] = {}

        def new_context(self, **kwargs: Any) -> FakeContext:
            self.context_kwargs = dict(kwargs)
            return FakeContext(self._page)

        def close(self) -> None:
            return None

    page = FakePage()
    browser = FakeBrowser(page)

    class FakeChromium:
        def launch(self, **kwargs: Any) -> FakeBrowser:
            return browser

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        def __enter__(self) -> FakePlaywright:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright package not installed in test env")

    from unittest.mock import patch

    with patch("playwright.sync_api.sync_playwright", MagicMock(return_value=FakePlaywright())):
        result = PlaywrightCapture(timeout_ms=20_000, render_wait_ms=4_000).capture(page_url)

    assert page.goto_kwargs.get("wait_until") == "domcontentloaded"
    assert page.goto_kwargs.get("timeout") == 20_000
    assert any(c.startswith("timeout:4000") for c in page.wait_calls)
    assert browser.context_kwargs.get("java_script_enabled") is True
    assert browser.context_kwargs.get("viewport") == {"width": 1280, "height": 900}
    assert "Chrome/" in str(browser.context_kwargs.get("user_agent") or "")
    assert "Nexus Mod Page" in result.html


def test_cloudflare_page_detected() -> None:
    html = """<!DOCTYPE html><html><body>
    <h1>Just a moment...</h1>
    <div id="cf-chl-widget">Checking your browser</div>
    </body></html>"""
    assert detect_page_block_reason(html) == ERROR_BLOCKED_BY_ANTI_BOT


def test_login_page_detected() -> None:
    html = """<!DOCTYPE html><html><body>
    <form id="login">Please log in</form>
    </body></html>"""
    assert detect_page_block_reason(html) == ERROR_LOGIN_REQUIRED


def test_timeout_exception_classified() -> None:
    assert (
        classify_playwright_exception(
            TimeoutError("Page.goto: Timeout 60000ms exceeded. waiting until networkidle")
        )
        == ERROR_NETWORK_TIMEOUT
    )


def test_capture_raises_blocked_on_cf_html() -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/1"
    cf_html = "<html><body>Just a moment... cf-chl challenge</body></html>"

    class FakePage:
        def set_default_timeout(self, ms: int) -> None:
            return None

        def goto(self, url: str, **kwargs: Any) -> None:
            assert kwargs.get("wait_until") == "domcontentloaded"

        def wait_for_timeout(self, ms: int) -> None:
            return None

        def wait_for_function(self, expr: str, **kwargs: Any) -> None:
            return None

        def evaluate(self, script: str) -> Any:
            if "readyState" in script and "outerHTML" not in script:
                return "complete"
            return {
                "html": cf_html,
                "urls": [],
                "sheets": [],
                "pageUrl": page_url,
                "readyState": "complete",
            }

        def content(self) -> str:
            return cf_html

        @property
        def url(self) -> str:
            return page_url

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

        def close(self) -> None:
            return None

    class FakeBrowser:
        def new_context(self, **kwargs: Any) -> FakeContext:
            return FakeContext()

        def close(self) -> None:
            return None

    class FakeChromium:
        def launch(self, **kwargs: Any) -> FakeBrowser:
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

        def __enter__(self) -> FakePlaywright:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright package not installed in test env")

    from unittest.mock import patch

    with patch("playwright.sync_api.sync_playwright", MagicMock(return_value=FakePlaywright())):
        with pytest.raises(PlaywrightCaptureError) as ei:
            PlaywrightCapture().capture(page_url)
    assert ei.value.code == ERROR_BLOCKED_BY_ANTI_BOT


def test_nexus_failure_raises_blocked_without_fallback(
    tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="96",
        source_url="https://www.nexusmods.com/palworld/mods/96",
        title="Blocked Mod",
        app_id=1623730,
        game_name="Palworld",
    )
    folder = lib / "Palworld" / "Blocked Mod"
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    (folder / INFO_DIR_NAME / "mod.json").write_text(
        json.dumps({"published_file_id": info.mod_id, "title": "Blocked Mod"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="手动导入"):
        NexusManualOfflineProvider().update_offline_page(
            info.mod_id, managed_path=folder, library_root=lib
        )
    assert not (folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
    refreshed = db.get_mod_display_info(info.mod_id)
    assert refreshed is not None
    assert refreshed.offline_status == OFFLINE_STATUS_FAILED


def test_nexus_provider_is_manual_import_not_browser_scrape() -> None:
    """Default Nexus offline path is local HTML import (no CDP / Playwright goto)."""
    provider = NexusManualOfflineProvider()
    assert provider.get_provider_name() == "nexus_manual_import"
    assert not hasattr(provider, "_browser_snapshot") or getattr(
        provider, "_browser_snapshot", None
    ) is None
