"""mod.io offline capture — Playwright SPA render, then asset localization.

Architecture::

    ModioOfflineProvider
        → Playwright Chromium
        → page.goto(url, wait_until=\"networkidle\")
        → html = page.content()
        → OfflinePageArchiver.archive_rendered_html (asset rewrite + cache)
        → .info/offline/index.html

Isolated from Steam / GitHub / Nexus capture paths.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from services.archive import normalize_page_url

logger = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_MS = 45_000
DEFAULT_RENDER_WAIT_MS = 2_000

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CaptureFunc = Callable[[str], str]


class ModioSnapshotError(RuntimeError):
    """Raised when Playwright cannot produce a usable mod.io DOM."""


def strip_modio_cookie_banner(html_text: str) -> str:
    """
    Remove mod.io cookie consent banner / overlay from rendered HTML.

    Targets the fixed bottom banner that contains
    ``mod.io uses essential cookies…``. Does not strip mod description,
    images, or other page content.
    """
    text = str(html_text or "")
    if not text.strip():
        return text
    markers = (
        "mod.io uses essential cookies",
        "essential cookies to make our site work",
    )
    low = text.lower()
    if not any(m in low for m in markers):
        return text

    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError:
        return text

    soup = BeautifulSoup(text, "html.parser")
    removed = False

    def _class_str(node) -> str:
        classes = getattr(node, "get", lambda *_: None)("class") or []
        if isinstance(classes, str):
            return classes
        return " ".join(str(c) for c in classes)

    for marker in markers:
        matches = soup.find_all(
            string=lambda s, m=marker: isinstance(s, NavigableString)
            and m in str(s).lower()
        )
        for match in matches:
            node = match.parent
            target = None
            cur = node
            for _ in range(12):
                if cur is None or not hasattr(cur, "name"):
                    break
                classes = _class_str(cur)
                if "tw-fixed" in classes or "cookie" in classes.lower():
                    target = cur
                    break
                cur = getattr(cur, "parent", None)
            if target is None:
                continue
            # Prefer the outer fixed banner; keep description/content intact.
            target.decompose()
            removed = True
            break
        if removed:
            break

    if not removed:
        return text
    return str(soup)


def looks_like_rendered_modio_page(html_text: str) -> bool:
    """True when HTML has real mod content (not an empty SPA shell)."""
    text = str(html_text or "")
    if len(text) < 200:
        return False
    low = text.lower()
    markers = (
        "mod.io",
        "description",
        "subscribe",
        "download",
        "mod-page",
        "modpage",
    )
    has_markers = any(m in low for m in markers) or ("<title" in low and "<h1" in low)

    # Empty React/Vue shell with little body text.
    if re.search(r"""id=["'](?:root|app|__next)["']""", low):
        body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.I | re.S)
        body = body_match.group(1) if body_match else text
        # Strip tags for a rough visible-text length check.
        visible = re.sub(r"<[^>]+>", " ", body)
        visible = re.sub(r"\s+", " ", visible).strip()
        if len(visible) < 40 and not has_markers:
            return False
    return has_markers


def validate_modio_dom(html_text: str) -> str | None:
    """Return an error code when *html_text* is not an acceptable offline snapshot."""
    text = str(html_text or "")
    if not text.strip():
        return "EMPTY_HTML"
    if not looks_like_rendered_modio_page(text):
        return "UNRENDERED_SPA_SHELL"
    return None


def capture_modio_page_content(
    url: str,
    *,
    timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
) -> str:
    """
    Open *url* in Playwright Chromium and return rendered ``page.content()``.

    Uses ``networkidle`` so the mod.io SPA finishes client-side rendering.
    """
    page_url = normalize_page_url(url)
    if not page_url:
        raise ModioSnapshotError("Empty URL")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ModioSnapshotError(
            "Playwright is not installed. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc

    html_text = ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=_USER_AGENT,
                locale="en-US",
                java_script_enabled=True,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(int(timeout_ms))
            page.goto(
                page_url,
                wait_until="networkidle",
                timeout=int(timeout_ms),
            )
            if int(render_wait_ms) > 0:
                page.wait_for_timeout(int(render_wait_ms))
            html_text = page.content() or ""
            context.close()
        finally:
            browser.close()

    if not str(html_text).strip():
        raise ModioSnapshotError("Empty HTML from Playwright page.content()")

    code = validate_modio_dom(html_text)
    if code:
        raise ModioSnapshotError(f"mod.io 页面访问失败 ({code})")
    return strip_modio_cookie_banner(str(html_text))
