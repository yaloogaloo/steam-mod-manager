"""Playwright Chromium capture — final DOM after JS render."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_RELOAD_TIMEOUT_MS = 20_000
DEFAULT_RENDER_WAIT_MS = 5_000
DEFAULT_WAIT_UNTIL = "domcontentloaded"
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CHROMIUM_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
)

# Structured capture failure codes (do not leak raw Playwright timeout text).
ERROR_BLOCKED_BY_ANTI_BOT = "BLOCKED_BY_ANTI_BOT"
ERROR_LOGIN_REQUIRED = "LOGIN_REQUIRED"
ERROR_NETWORK_TIMEOUT = "NETWORK_TIMEOUT"

_CF_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-chl",
    "cf-browser-verification",
    "attention required | cloudflare",
    "challenge-platform",
)
_LOGIN_MARKERS = (
    'id="login"',
    'name="login"',
    "/login?",
    "sign in to continue",
    "please log in",
    "you need to be logged in",
)


class PlaywrightCaptureError(RuntimeError):
    """Raised when Playwright cannot capture a rendered page."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = (code or message or "").strip() or "CAPTURE_FAILED"


@dataclass
class StylesheetCapture:
    href: str
    text: str
    error: str = ""


@dataclass
class PageCapture:
    """Rendered page HTML plus discovered asset URLs / inline stylesheet text."""

    html: str
    page_url: str
    discovered_urls: list[str] = field(default_factory=list)
    stylesheets: list[StylesheetCapture] = field(default_factory=list)


def detect_bot_challenge(html: str) -> bool:
    """
    Return True when *html* looks like a Cloudflare / bot challenge page.

    Markers: ``Just a moment``, ``Checking your browser``, ``cf-chl``,
    ``challenge``.
    """
    text = str(html or "")
    if not text.strip():
        return False
    low = text.lower()
    if "just a moment" in low:
        return True
    if "checking your browser" in low:
        return True
    if "cf-chl" in low or re.search(r"\bcf-chl\b", low):
        return True
    if "challenges.cloudflare.com" in low:
        return True
    if "cf-browser-verification" in low or "challenge-platform" in low:
        return True
    # Broad "challenge" alone is noisy; require a CF/context companion.
    if "challenge" in low and (
        "cloudflare" in low or "cf-" in low or "captcha" in low or "turnstile" in low
    ):
        return True
    if "captcha" in low and ("cloudflare" in low or "cf-" in low):
        return True
    return False


def looks_like_github_repo_page(html: str) -> bool:
    """True when HTML looks like a rendered public GitHub repository page."""
    low = str(html or "").lower()
    if not low.strip():
        return False
    markers = (
        "readme",
        "repository-content",
        "js-repo-pjax-container",
        "markdown-body",
        "layout-sidebar",
        'data-testid="readme"',
    )
    return any(m in low for m in markers)


def detect_page_block_reason(html: str, page_url: str = "") -> str | None:
    """
    Return a structured error code when the page is an anti-bot / login wall.

    Returns ``None`` when the HTML looks usable.

    Public GitHub repository pages often include a header \"Sign in\" link — that
    must NOT be treated as ``LOGIN_REQUIRED`` when README / repo content is present.
    """
    text = str(html or "")
    low = text.lower()
    url_low = str(page_url or "").lower()

    # GitHub public repo content wins over generic login-link heuristics.
    if "github.com" in url_low or looks_like_github_repo_page(text):
        if looks_like_github_repo_page(text):
            return None

    if detect_bot_challenge(text):
        return ERROR_BLOCKED_BY_ANTI_BOT
    if any(marker in low for marker in _CF_MARKERS):
        return ERROR_BLOCKED_BY_ANTI_BOT
    if re.search(r"\bcf-chl\b", low) or "challenges.cloudflare.com" in low:
        return ERROR_BLOCKED_BY_ANTI_BOT

    if any(marker in low for marker in _LOGIN_MARKERS):
        return ERROR_LOGIN_REQUIRED
    if "/login" in url_low and ("nexusmods.com" in url_low or "github.com" in url_low):
        if "sign in" in low or "log in" in low or "login" in low:
            return ERROR_LOGIN_REQUIRED
    return None


def classify_playwright_exception(exc: BaseException) -> str:
    """Map Playwright / transport errors to structured codes."""
    msg = str(exc or "")
    low = msg.lower()
    if "timeout" in low or "timed out" in low:
        return ERROR_NETWORK_TIMEOUT
    if "net::" in low or "ns_error" in low or "network" in low:
        return ERROR_NETWORK_TIMEOUT
    return msg.strip() or "CAPTURE_FAILED"


def _log_nexus_offline(*, status: str, detail: str = "") -> None:
    extra = f" detail={detail}" if detail else ""
    logger.info("[NEXUS_OFFLINE] stage=browser status=%s%s", status, extra)


class PlaywrightCapture:
    """
    Open *url* in headless Chromium and return the post-render DOM.

    Uses ``domcontentloaded`` (never ``networkidle``) — Nexus keeps long-lived
    requests that would never reach network idle.

    Navigation stages:
    1. ``goto`` with ``domcontentloaded`` (30s)
    2. ``reload`` once (20s)
    3. raise for caller fallback
    """

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        reload_timeout_ms: int = DEFAULT_RELOAD_TIMEOUT_MS,
        render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
        wait_until: str = DEFAULT_WAIT_UNTIL,
        headless: bool = True,
        user_agent: str = CHROME_UA,
        viewport: dict[str, int] | None = None,
        launch_args: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.timeout_ms = int(timeout_ms)
        self.reload_timeout_ms = int(reload_timeout_ms)
        self.render_wait_ms = max(0, int(render_wait_ms))
        # Never allow networkidle as the goto completion condition.
        wait = str(wait_until or DEFAULT_WAIT_UNTIL).strip().lower()
        if wait == "networkidle":
            wait = DEFAULT_WAIT_UNTIL
        self.wait_until = wait
        self.headless = bool(headless)
        self.user_agent = user_agent or CHROME_UA
        self.viewport = dict(viewport or DEFAULT_VIEWPORT)
        self.launch_args = list(launch_args if launch_args is not None else CHROMIUM_LAUNCH_ARGS)

    def capture(self, url: str) -> PageCapture:
        page_url = str(url or "").strip()
        if not page_url:
            raise PlaywrightCaptureError("Empty URL")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            _log_nexus_offline(status="fail", detail="playwright_missing")
            raise PlaywrightCaptureError(
                "Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            ) from exc

        html = ""
        final_url = page_url
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=list(self.launch_args),
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
                    self._navigate_with_stages(page, page_url)

                    if self.render_wait_ms:
                        page.wait_for_timeout(self.render_wait_ms)

                    html = page.content() or ""
                    try:
                        final_url = str(page.url or page_url).strip() or page_url
                    except Exception:  # noqa: BLE001
                        final_url = page_url
                    context.close()
                finally:
                    browser.close()
        except PlaywrightCaptureError:
            _log_nexus_offline(status="fail", detail="capture_error")
            raise
        except Exception as exc:  # noqa: BLE001
            code = classify_playwright_exception(exc)
            logger.warning("Playwright capture failed for %s: %s", page_url, exc)
            _log_nexus_offline(status="fail", detail=code)
            if code == ERROR_NETWORK_TIMEOUT:
                raise PlaywrightCaptureError(
                    ERROR_NETWORK_TIMEOUT, code=ERROR_NETWORK_TIMEOUT
                ) from exc
            raise PlaywrightCaptureError(str(exc), code=code) from exc

        if not str(html).strip():
            _log_nexus_offline(status="fail", detail="empty_html")
            raise PlaywrightCaptureError("Empty HTML from browser")

        block = detect_page_block_reason(html, final_url)
        if block:
            # Never persist Cloudflare / challenge pages.
            _log_nexus_offline(status="fail", detail=block)
            raise PlaywrightCaptureError(block, code=block)

        _log_nexus_offline(status="success")
        return PageCapture(
            html=str(html),
            page_url=final_url,
            discovered_urls=[],
            stylesheets=[],
        )

    def _navigate_with_stages(self, page: Any, page_url: str) -> None:
        """
        Stage1: goto domcontentloaded (timeout_ms).
        Stage2: reload once (reload_timeout_ms).
        Stage3: raise NETWORK_TIMEOUT for caller fallback.
        """
        # Stage 1
        try:
            page.goto(
                page_url,
                wait_until=self.wait_until,
                timeout=self.timeout_ms,
            )
            return
        except Exception as exc:  # noqa: BLE001
            code = classify_playwright_exception(exc)
            logger.info(
                "[NEXUS_OFFLINE] stage=browser status=fail detail=stage1_%s",
                code,
            )
            if code != ERROR_NETWORK_TIMEOUT:
                raise PlaywrightCaptureError(str(exc), code=code) from exc

        # Stage 2 — reload once
        try:
            if hasattr(page, "reload"):
                page.reload(
                    wait_until=self.wait_until,
                    timeout=self.reload_timeout_ms,
                )
            else:
                page.goto(
                    page_url,
                    wait_until=self.wait_until,
                    timeout=self.reload_timeout_ms,
                )
            return
        except Exception as exc:  # noqa: BLE001
            code = classify_playwright_exception(exc)
            logger.info(
                "[NEXUS_OFFLINE] stage=browser status=fail detail=stage2_%s",
                code,
            )

        # Stage 3 — fallback to caller
        logger.info("[NEXUS_OFFLINE] stage=browser status=fail detail=stage3_fallback")
        raise PlaywrightCaptureError(
            ERROR_NETWORK_TIMEOUT, code=ERROR_NETWORK_TIMEOUT
        )
