"""Generic webpage snapshot downloader for Nexus / GitHub offline pages.

Nexus Mods must use :class:`~services.offline.layout_snapshot.NexusSnapshotProvider`
(browser-first). Do not use ``requests`` / ``httpx`` as the primary fetch for Nexus.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup, Tag
from requests import HTTPError

from services.offline.browser import BrowserSnapshotBackend, BrowserSnapshotError
from services.offline.layout_snapshot import (
    LayoutSnapshotProcessor,
    LayoutSnapshotResult as NexusSnapshotResult,
    NexusSnapshotProvider,
)

logger = logging.getLogger(__name__)

DEFAULT_INDEX_NAME = "index.html"
DEFAULT_ASSETS_DIR = "assets"
DEFAULT_TIMEOUT = 30
MAX_ASSETS = 200
MAX_ASSET_BYTES = 12 * 1024 * 1024

# Phase-1 offline assets — skip JavaScript.
ALLOWED_ASSET_EXTENSIONS = {".css", ".png", ".jpg", ".jpeg", ".webp"}
BROWSER_FALLBACK_STATUSES = frozenset({401, 403, 429})

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_CSS_URL_RE = re.compile(
    r"""url\(\s*(['"]?)([^)'"]+)\1\s*\)""",
    re.IGNORECASE,
)


@dataclass
class SnapshotResult:
    """Outcome of one ``WebSnapshotDownloader.download`` call."""

    success: bool
    html_path: Path
    asset_count: int
    error: str | None = None
    used_browser: bool = False
    backend: str = "http"  # http | browser | layout | fallback
    used_fallback: bool = False


# Re-exported for callers that import Nexus types from this module.
__all__ = [
    "ALLOWED_ASSET_EXTENSIONS",
    "LayoutSnapshotProcessor",
    "NexusSnapshotProvider",
    "NexusSnapshotResult",
    "SnapshotResult",
    "WebSnapshotDownloader",
]


def _guess_extension(url_path: str, content_type: str = "") -> str:
    path = unquote(url_path or "")
    suffix = Path(path.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 8 and suffix != ".php":
        return suffix
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(mime)
        if guessed == ".jpe":
            return ".jpg"
        if guessed:
            return guessed
    return ".bin"


def _safe_filename(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    ext = _guess_extension(parsed.path, content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}{ext}"


def _is_allowed_asset(url: str, content_type: str = "") -> bool:
    """Phase 1: only CSS + png/jpg/jpeg/webp (no .js)."""
    ext = _guess_extension(urlparse(url).path, content_type)
    mime = (content_type or "").split(";", 1)[0].strip().lower()

    if ext in {".js", ".mjs"}:
        return False
    if mime.startswith("application/javascript") or mime.startswith("text/javascript"):
        return False

    if mime:
        if mime == "text/css":
            return True
        if mime in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            return True
        return False

    # Pre-download: allow known extensions, or extension-less CDN URLs.
    if ext in ALLOWED_ASSET_EXTENSIONS:
        return True
    if ext in {"", ".bin"}:
        return True
    return False


class WebSnapshotDownloader:
    """
    Download a remote HTML page and localize linked CSS / images.

    Prefer :class:`NexusSnapshotProvider` for Nexus Mods (browser-first).
    This class remains for GitHub / legacy paths.

    When ``prefer_browser=True``, Playwright runs first (no ``requests`` primary).
    Otherwise on HTTP 401 / 403 / 429 from ``requests``, falls back to Playwright.

    Phase 1 assets: ``.css`` / ``.png`` / ``.jpg`` / ``.jpeg`` / ``.webp``
    (JavaScript is not saved).
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_assets: int = MAX_ASSETS,
        headers: dict[str, str] | None = None,
        browser_backend: BrowserSnapshotBackend | None = None,
        browser_factory: Callable[[], BrowserSnapshotBackend] | None = None,
        enable_browser_fallback: bool = True,
        prefer_browser: bool = False,
    ) -> None:
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.max_assets = max(1, int(max_assets))
        self.headers = dict(headers or {"User-Agent": _USER_AGENT})
        self._browser_backend = browser_backend
        self._browser_factory = browser_factory
        self.enable_browser_fallback = bool(enable_browser_fallback)
        self.prefer_browser = bool(prefer_browser)

    def close(self) -> None:
        if self._owns_session:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> WebSnapshotDownloader:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get_browser(self) -> BrowserSnapshotBackend:
        if self._browser_backend is not None:
            return self._browser_backend
        if self._browser_factory is not None:
            self._browser_backend = self._browser_factory()
            return self._browser_backend
        self._browser_backend = BrowserSnapshotBackend(
            timeout_ms=int(max(self.timeout, 30) * 1000),
        )
        return self._browser_backend

    def download(self, url: str, output_dir: Path | str) -> SnapshotResult:
        """Fetch *url* into *output_dir* and rewrite asset references."""
        target = Path(output_dir)
        index_path = target / DEFAULT_INDEX_NAME
        page_url = str(url or "").strip()
        if not page_url:
            return SnapshotResult(
                success=False,
                html_path=index_path,
                asset_count=0,
                error="Empty URL",
            )

        used_browser = False
        try:
            target.mkdir(parents=True, exist_ok=True)
            assets_dir = target / DEFAULT_ASSETS_DIR
            if assets_dir.exists():
                shutil.rmtree(assets_dir, ignore_errors=True)
            assets_dir.mkdir(parents=True, exist_ok=True)

            html_text, final_url, used_browser = self._fetch_html(page_url)
            if not html_text.strip():
                return SnapshotResult(
                    success=False,
                    html_path=index_path,
                    asset_count=0,
                    error="Empty HTML response",
                    used_browser=used_browser,
                )

            soup = BeautifulSoup(html_text, "html.parser")
            asset_count = self._rewrite_and_download_assets(
                soup, final_url, assets_dir
            )

            # Keep base href relative so file:// opens work offline.
            for base in list(soup.find_all("base")):
                if isinstance(base, Tag):
                    base.decompose()

            tmp = target / f".{DEFAULT_INDEX_NAME}.tmp"
            tmp.write_text(str(soup), encoding="utf-8")
            tmp.replace(index_path)
            return SnapshotResult(
                success=True,
                html_path=index_path,
                asset_count=asset_count,
                error=None,
                used_browser=used_browser,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Snapshot failed for %s: %s", page_url, exc)
            return SnapshotResult(
                success=False,
                html_path=index_path,
                asset_count=0,
                error=str(exc),
                used_browser=used_browser,
            )

    def _fetch_html(self, page_url: str) -> tuple[str, str, bool]:
        """
        Return ``(html, final_url, used_browser)``.

        When ``prefer_browser`` is set, Playwright runs first (Nexus path).
        Otherwise tries ``requests`` first; on 401/403/429 falls back to browser.
        """
        if self.prefer_browser:
            logger.info(
                "[NEXUS_OFFLINE] stage=browser status=start prefer_browser=1 url=%s",
                page_url,
            )
            return self._fetch_html_via_browser(page_url)

        try:
            response = self.session.get(
                page_url,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status in BROWSER_FALLBACK_STATUSES:
                logger.info(
                    "HTTP %s for %s — falling back to browser snapshot",
                    status,
                    page_url,
                )
                return self._fetch_html_via_browser(page_url)

            response.raise_for_status()
            final_url = str(response.url or page_url)
            return (response.text or ""), final_url, False
        except HTTPError as exc:
            status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
            if status in BROWSER_FALLBACK_STATUSES and self.enable_browser_fallback:
                logger.info(
                    "HTTPError %s for %s — falling back to browser snapshot",
                    status,
                    page_url,
                )
                return self._fetch_html_via_browser(page_url)
            raise
        except BrowserSnapshotError:
            raise
        except Exception:
            # Non-HTTP transport failures: do not auto-browser unless enabled
            # and caller wants challenge fallback only for 401/403/429.
            raise

    def _fetch_html_via_browser(self, page_url: str) -> tuple[str, str, bool]:
        if not self.enable_browser_fallback:
            raise BrowserSnapshotError(
                f"Browser fallback disabled; cannot load {page_url}"
            )
        html = self._get_browser().capture(page_url)
        return html, page_url, True

    def _rewrite_and_download_assets(
        self,
        soup: BeautifulSoup,
        page_url: str,
        assets_dir: Path,
    ) -> int:
        seen: dict[str, str] = {}
        downloaded = 0

        # <link href="..."> — stylesheets / icons that are allowed types
        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            href = link.get("href")
            if not href:
                continue
            rel = " ".join(link.get("rel") or []).lower()
            # Prefer stylesheets; still allow image icons if extension matches
            if "stylesheet" not in rel and not _is_allowed_asset(str(href)):
                continue
            if not _is_allowed_asset(str(href), "text/css" if "stylesheet" in rel else ""):
                continue
            local = self._download_asset(str(href), page_url, assets_dir, seen)
            if local:
                link["href"] = local
                downloaded += 1
            if downloaded >= self.max_assets:
                return downloaded

        # <script src="..."> — phase 1: do not download / rewrite JS
        # Leave remote src so online viewing still works; offline skips scripts.

        # <img src="..."> (+ lazy attrs / srcset first candidate)
        for img in list(soup.find_all("img")):
            if not isinstance(img, Tag):
                continue
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
            local: str | None = None
            for raw in candidates:
                if not _is_allowed_asset(raw):
                    continue
                local = self._download_asset(raw, page_url, assets_dir, seen)
                if local:
                    break
            if local:
                img["src"] = local
                for attr in ("data-src", "data-lazy-src", "srcset", "data-srcset"):
                    if attr in img.attrs:
                        del img.attrs[attr]
                downloaded += 1
            if downloaded >= self.max_assets:
                return downloaded

        # Inline style url(...)
        for node in soup.find_all(style=True):
            if not isinstance(node, Tag):
                continue
            style = str(node.get("style") or "")
            if "url(" not in style.lower():
                continue
            rewritten, n = self._rewrite_css_urls(
                style,
                page_url,
                assets_dir,
                seen,
                relative_to_assets=False,
            )
            node["style"] = rewritten
            downloaded += n
            if downloaded >= self.max_assets:
                return downloaded

        return downloaded

    def _download_asset(
        self,
        raw_url: str,
        page_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        *,
        rewrite_css: bool = True,
    ) -> str | None:
        absolute = self._absolutize(raw_url, page_url)
        if absolute is None:
            return None
        if absolute in seen:
            return seen[absolute]
        if not _is_allowed_asset(absolute):
            return None

        try:
            resp = self.session.get(
                absolute,
                headers=self.headers,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )
            resp.raise_for_status()
            content_type = str(resp.headers.get("Content-Type") or "")
            if not _is_allowed_asset(absolute, content_type):
                return None
            filename = _safe_filename(absolute, content_type)
            # Force allowed extension when MIME is clear but path had .bin
            ext = Path(filename).suffix.lower()
            if ext not in ALLOWED_ASSET_EXTENSIONS:
                if "text/css" in content_type.lower():
                    filename = f"{Path(filename).stem}.css"
                elif "image/webp" in content_type.lower():
                    filename = f"{Path(filename).stem}.webp"
                elif "image/png" in content_type.lower():
                    filename = f"{Path(filename).stem}.png"
                elif "image/jpeg" in content_type.lower() or "image/jpg" in content_type.lower():
                    filename = f"{Path(filename).stem}.jpg"
                else:
                    return None

            dest = assets_dir / filename

            # Avoid clobber when hash collision with different ext
            if dest.exists() and absolute not in seen:
                stem = dest.stem
                n = 1
                while dest.exists():
                    dest = assets_dir / f"{stem}_{n}{dest.suffix}"
                    n += 1
                    filename = dest.name

            size = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    logger.debug("Skip oversized asset %s", absolute)
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)

            if rewrite_css and (
                filename.lower().endswith(".css")
                or "text/css" in content_type.lower()
            ):
                try:
                    text = data.decode(resp.encoding or "utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    text = data.decode("utf-8", errors="replace")
                rewritten, _n = self._rewrite_css_urls(
                    text,
                    absolute,
                    assets_dir,
                    seen,
                    rewrite_css=False,
                    relative_to_assets=True,
                )
                dest.write_text(rewritten, encoding="utf-8")
            else:
                dest.write_bytes(data)

            rel = f"{DEFAULT_ASSETS_DIR}/{filename}"
            seen[absolute] = rel
            return rel
        except Exception as exc:  # noqa: BLE001
            logger.debug("Asset download failed %s: %s", absolute, exc)
            return None

    def _rewrite_css_urls(
        self,
        css_text: str,
        base_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        *,
        rewrite_css: bool = False,
        relative_to_assets: bool = True,
    ) -> tuple[str, int]:
        count = 0

        def _repl(match: re.Match[str]) -> str:
            nonlocal count
            quote = match.group(1) or ""
            raw = (match.group(2) or "").strip()
            if not raw or raw.startswith("data:") or raw.startswith("#"):
                return match.group(0)
            if not _is_allowed_asset(raw):
                return match.group(0)
            local = self._download_asset(
                raw, base_url, assets_dir, seen, rewrite_css=rewrite_css
            )
            if not local:
                return match.group(0)
            count += 1
            if relative_to_assets and local.startswith(f"{DEFAULT_ASSETS_DIR}/"):
                href = Path(local).name
            else:
                href = local
            return f"url({quote}{href}{quote})"

        return _CSS_URL_RE.sub(_repl, css_text), count

    @staticmethod
    def _absolutize(raw_url: str, page_url: str) -> str | None:
        text = (raw_url or "").strip()
        if not text or text.startswith("#") or text.lower().startswith("data:"):
            return None
        if text.startswith(("javascript:", "mailto:", "blob:")):
            return None
        try:
            absolute = urljoin(page_url, text)
        except Exception:  # noqa: BLE001
            return None
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            return None
        return absolute
