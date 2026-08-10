"""Playwright Chromium backend for Cloudflare / JS-challenge pages."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_WAIT_UNTIL = "domcontentloaded"
DEFAULT_RENDER_WAIT_MS = 5_000
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CHROMIUM_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
)


class BrowserSnapshotError(RuntimeError):
    """Raised when Playwright cannot capture a page."""


class BrowserSnapshotBackend:
    """
    Render a URL in headless Chromium and return the final HTML.

    Uses ``domcontentloaded`` (never ``networkidle``). Prefer
    ``services.offline.browser_snapshot.playwright_capture.PlaywrightCapture``
    for Nexus (staged navigation + challenge detection).
    """

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        wait_until: str = DEFAULT_WAIT_UNTIL,
        headless: bool = True,
        render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
        user_agent: str = CHROME_UA,
        viewport: dict[str, int] | None = None,
    ) -> None:
        self.timeout_ms = int(timeout_ms)
        wait = str(wait_until or DEFAULT_WAIT_UNTIL).strip().lower()
        if wait == "networkidle":
            wait = DEFAULT_WAIT_UNTIL
        self.wait_until = wait
        self.headless = bool(headless)
        self.render_wait_ms = max(0, int(render_wait_ms))
        self.user_agent = user_agent or CHROME_UA
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)

    def capture(self, url: str) -> str:
        """
        Navigate to *url*, wait until ``domcontentloaded``, return ``page.content()``.

        Raises ``BrowserSnapshotError`` on failure or missing Playwright.
        """
        page_url = str(url or "").strip()
        if not page_url:
            raise BrowserSnapshotError("Empty URL")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserSnapshotError(
                "Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            ) from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=list(CHROMIUM_LAUNCH_ARGS),
                )
                try:
                    context = browser.new_context(
                        user_agent=self.user_agent,
                        locale="en-US",
                        java_script_enabled=True,
                        viewport=self.viewport,
                    )
                    page = context.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    page.goto(
                        page_url,
                        wait_until=self.wait_until,
                        timeout=self.timeout_ms,
                    )
                    if self.render_wait_ms:
                        page.wait_for_timeout(self.render_wait_ms)
                    html = page.content() or ""
                    context.close()
                finally:
                    browser.close()
        except BrowserSnapshotError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Browser snapshot failed for %s: %s", page_url, exc)
            raise BrowserSnapshotError(str(exc)) from exc

        if not str(html).strip():
            raise BrowserSnapshotError("Empty HTML from browser")
        return str(html)


def capture_with_browser(url: str, **kwargs: Any) -> str:
    """Convenience wrapper around ``BrowserSnapshotBackend.capture``."""
    return BrowserSnapshotBackend(**kwargs).capture(url)
