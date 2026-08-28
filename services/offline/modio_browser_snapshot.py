"""mod.io offline capture — Playwright SPA render, then asset localization."""

from __future__ import annotations

import logging
import os
import re
from enum import Enum
from pathlib import Path
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

_CHROMIUM_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)

CaptureFunc = Callable[[str], str]

# Only cache successful runtime probes — never cache launch failures.
_runtime_launch_ok = False


class ModioSnapshotKind(str, Enum):
    PACKAGE_MISSING = "PLAYWRIGHT_PACKAGE_MISSING"
    BROWSER_MISSING = "PLAYWRIGHT_BROWSER_MISSING"
    LAUNCH_FAILED = "PLAYWRIGHT_LAUNCH_FAILED"
    PAGE_LOAD_FAILED = "PLAYWRIGHT_PAGE_LOAD_FAILED"
    SPA_UNRENDERED = "PLAYWRIGHT_SPA_UNRENDERED"
    PAGE_ACCESS_FAILED = "PLAYWRIGHT_PAGE_ACCESS_FAILED"


MODIO_SNAPSHOT_USER_MESSAGES: dict[ModioSnapshotKind, str] = {
    ModioSnapshotKind.PACKAGE_MISSING: (
        "Playwright Python 包未安装，请运行: pip install playwright"
    ),
    ModioSnapshotKind.BROWSER_MISSING: (
        "Chromium 浏览器未安装，请运行: playwright install chromium"
    ),
    ModioSnapshotKind.LAUNCH_FAILED: "离线网页浏览器启动失败",
    ModioSnapshotKind.PAGE_LOAD_FAILED: "mod.io 页面加载失败",
    ModioSnapshotKind.SPA_UNRENDERED: "mod.io 页面未完成渲染",
    ModioSnapshotKind.PAGE_ACCESS_FAILED: "mod.io 页面访问失败",
}


class ModioSnapshotError(RuntimeError):
    """Raised when Playwright cannot produce a usable mod.io DOM."""

    def __init__(
        self,
        kind: ModioSnapshotKind,
        *,
        detail: str = "",
        from_exc: BaseException | None = None,
    ) -> None:
        self.kind = kind
        self.detail = str(detail or "").strip()
        self.user_message = MODIO_SNAPSHOT_USER_MESSAGES[kind]
        if kind == ModioSnapshotKind.LAUNCH_FAILED and self.detail:
            self.user_message = f"{self.user_message}: {self.detail}"
        super().__init__(self.user_message)
        if from_exc is not None:
            self.__cause__ = from_exc


def strip_modio_cookie_banner(html_text: str) -> str:
    """Remove mod.io cookie consent banner from rendered HTML."""
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

    if re.search(r"""id=["'](?:root|app|__next)["']""", low):
        body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.I | re.S)
        body = body_match.group(1) if body_match else text
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


def _is_browser_executable_missing(exc: BaseException) -> bool:
    """True only when the Chromium binary itself is absent — not generic failures."""
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc or "").lower()
    return (
        "executable doesn't exist" in text
        or "browser has not been found" in text
        or "failed to launch chromium" in text and "executable" in text
    )


def _classify_launch_error(exc: BaseException) -> ModioSnapshotKind:
    if _is_browser_executable_missing(exc):
        return ModioSnapshotKind.BROWSER_MISSING
    return ModioSnapshotKind.LAUNCH_FAILED


def _launch_chromium_browser(playwright) -> object:
    """Launch headless Chromium; log path and real errors."""
    logger.warning("[MODIO_OFFLINE] playwright_start")
    chromium_path = str(playwright.chromium.executable_path or "")
    logger.warning("[MODIO_OFFLINE] chromium_path=%s", chromium_path)
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=list(_CHROMIUM_LAUNCH_ARGS),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MODIO_OFFLINE] launch_failed error=%s", exc)
        kind = _classify_launch_error(exc)
        raise ModioSnapshotError(
            kind,
            detail=str(exc),
            from_exc=exc,
        ) from exc
    logger.warning("[MODIO_OFFLINE] launch_success")
    return browser


def _open_chromium_browser(*, clear_browsers_path: bool = False):
    """
    Enter Playwright and launch Chromium.

    Returns ``(cm, saved_browsers_path, browser)``. Caller must ``browser.close()``
    and ``cm.__exit__``, restoring *saved_browsers_path* when set.
    """
    from playwright.sync_api import sync_playwright

    saved_path: str | None = None
    if clear_browsers_path and os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        saved_path = os.environ.pop("PLAYWRIGHT_BROWSERS_PATH")
        logger.warning(
            "[MODIO_OFFLINE] retry without PLAYWRIGHT_BROWSERS_PATH (was %r)",
            saved_path,
        )
    cm = sync_playwright()
    playwright = cm.__enter__()
    try:
        browser = _launch_chromium_browser(playwright)
        return cm, saved_path, browser
    except Exception:
        cm.__exit__(None, None, None)
        if saved_path is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved_path
        raise


def _close_chromium_browser(cm, saved_path: str | None, browser) -> None:
    if browser is not None:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
    if saved_path is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved_path


def probe_modio_playwright_runtime(*, force: bool = False) -> tuple[bool, str]:
    """
    Optional startup probe. Never blocks later capture attempts on failure.

    Returns ``(ready, user_message_if_not)``.
    """
    global _runtime_launch_ok
    if _runtime_launch_ok and not force:
        return True, ""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        msg = MODIO_SNAPSHOT_USER_MESSAGES[ModioSnapshotKind.PACKAGE_MISSING]
        logger.warning("[MODIO_OFFLINE] runtime: package missing: %s", exc)
        return False, msg

    last_error = ""
    for clear_path in (False, True):
        cm = None
        saved_path = None
        browser = None
        try:
            cm, saved_path, browser = _open_chromium_browser(clear_browsers_path=clear_path)
            browser.close()
            browser = None
            _close_chromium_browser(cm, saved_path, None)
            cm = None
            _runtime_launch_ok = True
            logger.info("[MODIO_OFFLINE] runtime check ok")
            return True, ""
        except ModioSnapshotError as exc:
            last_error = exc.user_message
            logger.warning(
                "[MODIO_OFFLINE] snapshot_failed kind=%s detail=%s",
                exc.kind.value,
                exc.detail or exc,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.warning("[MODIO_OFFLINE] runtime check failed: %s", exc)
        finally:
            _close_chromium_browser(cm, saved_path, browser)

    return False, last_error


def reset_modio_playwright_runtime_cache() -> None:
    """Test helper: clear cached successful runtime probe."""
    global _runtime_launch_ok
    _runtime_launch_ok = False


def _navigate_modio_page(page, page_url: str, *, timeout_ms: int, render_wait_ms: int) -> None:
    """Load mod.io SPA via domcontentloaded + content selector wait."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=int(timeout_ms),
        )
    except PlaywrightTimeoutError as exc:
        raise ModioSnapshotError(
            ModioSnapshotKind.PAGE_LOAD_FAILED,
            detail=str(exc),
            from_exc=exc,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ModioSnapshotError(
            ModioSnapshotKind.PAGE_LOAD_FAILED,
            detail=str(exc),
            from_exc=exc,
        ) from exc

    if int(render_wait_ms) > 0:
        page.wait_for_timeout(int(render_wait_ms))
    try:
        page.wait_for_selector(
            "h1, [class*='mod'], [class*='Mod'], #root",
            timeout=min(20_000, int(timeout_ms)),
        )
    except PlaywrightTimeoutError:
        logger.warning(
            "[MODIO_OFFLINE] content selector timeout url=%s", page_url
        )


def capture_modio_page_content(
    url: str,
    *,
    timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
) -> str:
    """Open *url* in Playwright Chromium and return rendered ``page.content()``."""
    page_url = normalize_page_url(url)
    if not page_url:
        raise ModioSnapshotError(
            ModioSnapshotKind.PAGE_ACCESS_FAILED,
            detail="empty url",
        )

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        raise ModioSnapshotError(
            ModioSnapshotKind.PACKAGE_MISSING,
            detail=str(exc),
            from_exc=exc,
        ) from exc

    html_text = ""
    landed_url = page_url
    last_exc: ModioSnapshotError | None = None

    for clear_path in (False, True):
        cm = None
        saved_path = None
        browser = None
        try:
            cm, saved_path, browser = _open_chromium_browser(clear_browsers_path=clear_path)
            context = browser.new_context(
                user_agent=_USER_AGENT,
                locale="en-US",
                java_script_enabled=True,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(int(timeout_ms))
            _navigate_modio_page(
                page,
                page_url,
                timeout_ms=int(timeout_ms),
                render_wait_ms=int(render_wait_ms),
            )
            landed_url = page.url or page_url
            html_text = page.content() or ""
            context.close()
            browser.close()
            browser = None
            _close_chromium_browser(cm, saved_path, None)
            cm = None
            global _runtime_launch_ok
            _runtime_launch_ok = True
            break
        except ModioSnapshotError as exc:
            last_exc = exc
            if exc.kind == ModioSnapshotKind.BROWSER_MISSING and not clear_path:
                logger.warning(
                    "[MODIO_OFFLINE] browser missing at configured path; "
                    "retry with default ms-playwright location"
                )
                continue
            raise
        finally:
            _close_chromium_browser(cm, saved_path, browser)
    else:
        assert last_exc is not None
        raise last_exc

    if not str(html_text).strip():
        raise ModioSnapshotError(
            ModioSnapshotKind.PAGE_LOAD_FAILED,
            detail="empty html",
        )

    code = validate_modio_dom(html_text)
    if code == "EMPTY_HTML":
        raise ModioSnapshotError(ModioSnapshotKind.PAGE_LOAD_FAILED, detail=code)
    if code == "UNRENDERED_SPA_SHELL":
        raise ModioSnapshotError(ModioSnapshotKind.SPA_UNRENDERED, detail=code)

    logger.warning(
        "[MODIO_OFFLINE] snapshot_success url=%s html_len=%s",
        landed_url,
        len(html_text),
    )
    return strip_modio_cookie_banner(str(html_text))
