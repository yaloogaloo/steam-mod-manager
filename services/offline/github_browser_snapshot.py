"""GitHub offline snapshot — Playwright rendered DOM only.

Architecture (sole path)::

    GithubOfflineProvider
        → Playwright Chromium
        → page.goto(url, wait_until=\"domcontentloaded\")
        → page.wait_for_timeout(5000)
        → html = page.content()
        → write .info/offline/index.html

No requests HTML download, no asset crawler, no path rewriting, no fallback /
summary / LOGIN_REQUIRED pages.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_INDEX_NAME = "index.html"
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_RENDER_WAIT_MS = 5_000

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Markers that prove React has rendered a real repository page.
_REPO_MARKERS = (
    "repository-content",
    "js-repo-pjax-container",
    "markdown-body",
    'data-testid="readme"',
    'id="readme"',
    "Box-row",
    "react-directory-filename-column",
    "Layout-sidebar",
)

CaptureFunc = Callable[[str], str]


@dataclass
class GitHubBrowserSnapshotResult:
    """Outcome of one Playwright DOM save."""

    success: bool
    html_path: Path
    error: str | None = None
    backend: str = "playwright"
    source: str = "playwright_page_content"  # must never be "requests"


class GitHubSnapshotError(RuntimeError):
    """Raised when Playwright cannot produce a usable GitHub DOM."""


def looks_like_rendered_github_repo(html_text: str) -> bool:
    """True when HTML contains rendered repo structure (not an empty React shell)."""
    text = str(html_text or "")
    if not text.strip():
        return False
    low = text.lower()
    if not any(m.lower() in low for m in _REPO_MARKERS):
        return False
    # Reject pages whose only body content is an empty #root.
    if re.search(r"""id=["']root["']""", low):
        # Allow if repo markers also present (GitHub may keep #root alongside content).
        if "markdown-body" not in low and "repository-content" not in low:
            return False
    return True


def is_empty_react_shell(html_text: str) -> bool:
    """True when the document is essentially ``<div id=\"root\"></div>``."""
    text = str(html_text or "")
    low = text.lower()
    if not re.search(r"""id=["']root["']""", low):
        return False
    return not looks_like_rendered_github_repo(text)


def validate_github_dom(html_text: str) -> str | None:
    """
    Return an error code when *html_text* is not an acceptable offline snapshot.

    Acceptable pages must include repository identity and README / file-list DOM.
    """
    text = str(html_text or "")
    if not text.strip():
        return "EMPTY_HTML"
    if is_empty_react_shell(text):
        return "UNRENDERED_REACT_SHELL"
    if not looks_like_rendered_github_repo(text):
        return "MISSING_REPO_DOM"
    low = text.lower()
    has_readme = (
        "markdown-body" in low
        or 'id="readme"' in low
        or 'data-testid="readme"' in low
        or "readme" in low
    )
    if not has_readme:
        return "MISSING_README"
    return None


def capture_github_page_content(
    url: str,
    *,
    timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
) -> str:
    """
    Open *url* in Playwright Chromium and return ``page.content()``.

    Uses ``domcontentloaded`` only — never ``networkidle``.
    """
    page_url = str(url or "").strip()
    if not page_url:
        raise GitHubSnapshotError("Empty URL")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GitHubSnapshotError(
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
            # Forbidden: wait_until="networkidle"
            page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=int(timeout_ms),
            )
            page.wait_for_timeout(int(render_wait_ms))
            html_text = page.content() or ""
            context.close()
        finally:
            browser.close()

    if not str(html_text).strip():
        raise GitHubSnapshotError("Empty HTML from Playwright page.content()")
    return str(html_text)


def _write_metadata(output_dir: Path, *, source_url: str, title: str = "") -> None:
    meta = {
        "provider": "github_snapshot",
        "source_url": source_url,
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "browser_dom",
        "source": "playwright_page_content",
        "title": title or "",
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prepare_offline_html(html_text: str) -> str:
    """
    Light cleanup that must not destroy layout.

    - Keep ``<style>`` and ``<link rel=stylesheet>`` as emitted by the browser
    - Do NOT download or rewrite asset URLs
    - Strip ``<script>`` so offline open does not re-hydrate / navigate away
    """
    try:
        from bs4 import BeautifulSoup, Tag
    except ImportError:
        out = str(html_text or "")
        if not out.lstrip().lower().startswith("<!doctype"):
            out = "<!DOCTYPE html>\n" + out
        return out

    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    for tag_name in ("script", "noscript"):
        for node in list(soup.find_all(tag_name)):
            node.decompose()
    for node in list(soup.find_all(True)):
        if not isinstance(node, Tag):
            continue
        for attr in list(node.attrs):
            if str(attr).lower().startswith("on"):
                del node.attrs[attr]
    for base in list(soup.find_all("base")):
        if isinstance(base, Tag):
            base.decompose()

    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out


class GitHubBrowserSnapshot:
    """
    Sole GitHub offline snapshot implementation.

    Saves Playwright ``page.content()`` to ``output_dir/index.html``.
    Does not generate fallback / summary / LOGIN_REQUIRED pages.
    """

    def __init__(
        self,
        *,
        capture_func: CaptureFunc | None = None,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        render_wait_ms: int = DEFAULT_RENDER_WAIT_MS,
        title: str = "",
    ) -> None:
        self._capture_func = capture_func
        self.timeout_ms = int(timeout_ms)
        self.render_wait_ms = int(render_wait_ms)
        self.title = title

    def close(self) -> None:
        return None

    def __enter__(self) -> GitHubBrowserSnapshot:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def snapshot(self, url: str, output_dir: Path | str) -> GitHubBrowserSnapshotResult:
        target = Path(output_dir)
        index_path = target / DEFAULT_INDEX_NAME
        page_url = str(url or "").strip()
        if not page_url:
            return GitHubBrowserSnapshotResult(
                success=False,
                html_path=index_path,
                error="Empty URL",
            )

        try:
            if self._capture_func is not None:
                raw_html = str(self._capture_func(page_url) or "")
            else:
                raw_html = capture_github_page_content(
                    page_url,
                    timeout_ms=self.timeout_ms,
                    render_wait_ms=self.render_wait_ms,
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("GitHub Playwright capture failed for %s: %s", page_url, exc)
            return GitHubBrowserSnapshotResult(
                success=False,
                html_path=index_path,
                error=str(exc),
            )

        err = validate_github_dom(raw_html)
        if err:
            return GitHubBrowserSnapshotResult(
                success=False,
                html_path=index_path,
                error=err,
            )

        try:
            target.mkdir(parents=True, exist_ok=True)
            # Keep browser stylesheets / inline <style>; do not download assets.
            saved = _prepare_offline_html(raw_html)
            # Normalize root/relative hrefs so file:// open jumps to real GitHub URLs.
            from services.offline.url_rewriter import rewrite_external_urls

            saved = rewrite_external_urls(saved, page_url)
            # Re-validate after script strip (structure must remain).
            err2 = validate_github_dom(saved)
            if err2:
                return GitHubBrowserSnapshotResult(
                    success=False,
                    html_path=index_path,
                    error=err2,
                )

            tmp = target / f".{DEFAULT_INDEX_NAME}.tmp"
            tmp.write_text(saved, encoding="utf-8")
            tmp.replace(index_path)
            _write_metadata(target, source_url=page_url, title=self.title)
            # Empty assets dir for directory contract only (no downloads).
            (target / "assets").mkdir(parents=True, exist_ok=True)
            return GitHubBrowserSnapshotResult(
                success=True,
                html_path=index_path,
                backend="playwright",
                source="playwright_page_content",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHub DOM save failed for %s: %s", page_url, exc)
            return GitHubBrowserSnapshotResult(
                success=False,
                html_path=index_path,
                error=str(exc),
            )


def run_github_browser_snapshot(
    *,
    source_url: str,
    output_dir: Path | str,
    snapshot: GitHubBrowserSnapshot | None = None,
    title: str = "",
) -> tuple[GitHubBrowserSnapshotResult, str]:
    """
    Run Playwright snapshot and map to offline status.

    Success → archived. Failure → failed (no HTML fallback page written).
    """
    from core.mod_platform import OFFLINE_STATUS_ARCHIVED, OFFLINE_STATUS_FAILED

    provider = snapshot or GitHubBrowserSnapshot(title=title)
    owns = snapshot is None
    try:
        result = provider.snapshot(source_url, output_dir)
    finally:
        if owns:
            provider.close()

    if result.success and result.html_path.is_file():
        return result, OFFLINE_STATUS_ARCHIVED
    return result, OFFLINE_STATUS_FAILED
