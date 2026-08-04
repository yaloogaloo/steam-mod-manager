"""Archive Steam Workshop pages for offline viewing."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

WORKSHOP_PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"
DEFAULT_INDEX_NAME = "index.html"
DEFAULT_ASSETS_DIR = "assets"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Limit runaway downloads on heavily asset-laden Steam pages
MAX_CSS_FILES = 40
MAX_IMAGES = 80
MAX_ASSET_BYTES = 8 * 1024 * 1024  # 8 MiB per asset


class OfflinePageArchiver:
    """
    Download a Workshop item page into ``info/`` as a self-contained
    offline HTML tree (``index.html`` + ``assets/``).

    Relative links are rewritten so the page opens correctly from disk
    in the system browser.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30,
        max_css: int = MAX_CSS_FILES,
        max_images: int = MAX_IMAGES,
    ) -> None:
        self.timeout = timeout
        self.max_css = max_css
        self.max_images = max_images
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._session.headers.setdefault("User-Agent", _USER_AGENT)
        self._session.headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> OfflinePageArchiver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def archive(
        self,
        published_file_id: str | int,
        info_dir: str | Path,
        *,
        overwrite: bool = True,
    ) -> Path:
        """
        Save the Workshop page for *published_file_id* under *info_dir*.

        Returns the path to ``index.html``.
        """
        info_dir = Path(info_dir)
        info_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = info_dir / DEFAULT_ASSETS_DIR
        assets_dir.mkdir(parents=True, exist_ok=True)

        index_path = info_dir / DEFAULT_INDEX_NAME
        if index_path.exists() and not overwrite:
            return index_path

        page_url = WORKSHOP_PAGE_URL.format(id=published_file_id)
        response = self._session.get(page_url, timeout=self.timeout)
        response.raise_for_status()
        # Steam often serves pages as UTF-8; fall back via apparent encoding
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text

        soup = BeautifulSoup(html, "lxml")
        self._strip_noise(soup)
        self._rewrite_and_download_assets(soup, page_url, assets_dir)
        self._inject_offline_banner(soup, published_file_id, page_url)

        index_path.write_text(str(soup), encoding="utf-8")
        logger.info("Archived offline page -> %s", index_path)
        return index_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _strip_noise(self, soup: BeautifulSoup) -> None:
        for selector in (
            "script",
            "iframe",
            "noscript",
            "#global_header",
            "#footer",
            ".responsive_header",
            ".agegate_text_container",
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
        banner["style"] = (
            "background:#1b2838;color:#c7d5e0;padding:10px 16px;"
            "font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
            "border-bottom:2px solid #66c0f4;"
        )
        banner.string = (
            f"Offline archive · Workshop ID {published_file_id} · "
            f"Original: {page_url}"
        )
        body.insert(0, banner)

    def _rewrite_and_download_assets(
        self,
        soup: BeautifulSoup,
        page_url: str,
        assets_dir: Path,
    ) -> None:
        css_count = 0
        img_count = 0
        seen: dict[str, str] = {}  # absolute_url -> relative local path

        # Stylesheets
        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            rel = " ".join(link.get("rel") or []).lower()
            href = link.get("href")
            if "stylesheet" not in rel or not href:
                continue
            if css_count >= self.max_css:
                link.decompose()
                continue
            local = self._download_asset(href, page_url, assets_dir, seen)
            if local:
                link["href"] = local
                css_count += 1
            else:
                link.decompose()

        # Inline style url(...) references are left as-is (often CDN fonts)

        # Images
        for img in list(soup.find_all("img")):
            if not isinstance(img, Tag):
                continue
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            if img_count >= self.max_images:
                continue
            local = self._download_asset(src, page_url, assets_dir, seen)
            if local:
                img["src"] = local
                img_count += 1

        # Open Graph / favicon etc. — optional, skip for size

        # Convert remaining absolute http(s) anchors to stay clickable online
        # (descriptions often link out); leave as-is.

    def _download_asset(
        self,
        raw_url: str,
        page_url: str,
        assets_dir: Path,
        seen: dict[str, str],
    ) -> str | None:
        absolute = urljoin(page_url, raw_url.strip())
        if absolute.startswith("//"):
            absolute = "https:" + absolute

        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            return None

        if absolute in seen:
            return seen[absolute]

        try:
            response = self._session.get(absolute, timeout=self.timeout, stream=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("Asset download failed %s: %s", absolute, exc)
            return None

        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        ext = _guess_extension(parsed.path, content_type)
        digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:12]
        filename = f"{digest}{ext}"
        dest = assets_dir / filename

        try:
            size = 0
            with dest.open("wb") as fh:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_ASSET_BYTES:
                        raise OSError("asset exceeds size limit")
                    fh.write(chunk)
        except OSError as exc:
            logger.debug("Failed writing asset %s: %s", dest, exc)
            if dest.exists():
                dest.unlink(missing_ok=True)
            return None

        # If CSS, rewrite nested url() references to local copies (best-effort)
        if ext == ".css":
            try:
                self._rewrite_css_urls(dest, absolute, assets_dir, seen)
            except OSError:
                pass

        relative = f"{DEFAULT_ASSETS_DIR}/{filename}"
        seen[absolute] = relative
        return relative

    def _rewrite_css_urls(
        self,
        css_path: Path,
        css_url: str,
        assets_dir: Path,
        seen: dict[str, str],
    ) -> None:
        text = css_path.read_text(encoding="utf-8", errors="ignore")

        def replacer(match: re.Match[str]) -> str:
            raw = match.group(1).strip(" '\"")
            if raw.startswith("data:") or raw.startswith("#"):
                return match.group(0)
            local = self._download_asset(raw, css_url, assets_dir, seen)
            if not local:
                return match.group(0)
            # CSS lives in assets/, so peer assets are same-folder
            peer = Path(local).name
            return f"url({peer})"

        rewritten = re.sub(r"url\(([^)]+)\)", replacer, text)
        css_path.write_text(rewritten, encoding="utf-8")


def _guess_extension(path: str, content_type: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".woff2", ".woff", ".ttf"}:
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
    }
    return mapping.get(content_type.lower(), ".bin")
