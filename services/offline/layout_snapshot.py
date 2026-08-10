"""Layout-preserving offline snapshots (DOM + primary CSS, not full site mirrors).

Nexus: browser-first (Playwright ``domcontentloaded`` → ``page.content()`` →
LayoutSnapshotProcessor). Never uses requests/httpx as the primary fetch.

GitHub: HTTP layout download first; optional browser backup; styled fallback.

Outputs under ``.info/offline/``:
  index.html, assets/ (CSS), metadata.json
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup, Tag

from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    normalize_platform,
)

logger = logging.getLogger(__name__)

DEFAULT_INDEX_NAME = "index.html"
DEFAULT_ASSETS_DIR = "assets"
DEFAULT_TIMEOUT = 30
MAX_CSS_ASSETS = 40
MAX_IMAGE_ASSETS = 3
MAX_ASSET_BYTES = 8 * 1024 * 1024

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_REMOVE_TAGS = frozenset(
    {
        "script",
        "noscript",
        "iframe",
        "object",
        "embed",
        "template",
    }
)

_TRACKING_ATTR_MARKERS = (
    "data-tracking",
    "data-analytics",
    "data-gtm",
    "data-ga",
    "data-ad",
    "data-ads",
)

_TRACKING_CLASS_RE = re.compile(
    r"(analytics|tracking|gtm|google-analytics|googletagmanager|"
    r"facebook|hotjar|segment|mixpanel|snowplow|adsbygoogle|ad-container)",
    re.IGNORECASE,
)

_TRACKING_HREF_RE = re.compile(
    r"(google-analytics|googletagmanager|facebook\.net|hotjar|"
    r"segment\.io|doubleclick|adservice|clarity\.ms|newrelic)",
    re.IGNORECASE,
)

_CF_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-chl",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
    "cloudflare",
)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}
_AVATAR_HINT_RE = re.compile(r"(avatar|logo|icon|thumb|cover|og.?image)", re.I)

FetchFunc = Callable[[str], tuple[str, int, str]]


@dataclass
class LayoutProcessResult:
    """Outcome of ``LayoutSnapshotProcessor.process``."""

    success: bool
    html_path: Path
    error: str | None = None


@dataclass
class LayoutSnapshotResult:
    """Outcome of layout / browser / styled-fallback snapshot."""

    success: bool
    html_path: Path
    asset_count: int = 0
    error: str | None = None
    backend: str = "layout"  # layout | browser | fallback
    used_fallback: bool = False
    failure_reason: str | None = None


class LayoutSnapshotProcessor:
    """
    Clean rendered HTML into a layout-oriented offline ``index.html``.

    Keeps CSS ``<link rel=stylesheet>``; removes scripts / tracking.
    """

    def __init__(self, *, index_name: str = DEFAULT_INDEX_NAME) -> None:
        self.index_name = index_name or DEFAULT_INDEX_NAME

    def process(
        self,
        html_text: str,
        output_dir: Path | str,
        *,
        page_url: str = "",
    ) -> LayoutProcessResult:
        del page_url
        target = Path(output_dir)
        index_path = target / self.index_name
        text = str(html_text or "").strip()
        if not text:
            return LayoutProcessResult(
                success=False, html_path=index_path, error="Empty HTML"
            )
        try:
            target.mkdir(parents=True, exist_ok=True)
            cleaned = self.clean_html(text)
            if not cleaned.strip():
                return LayoutProcessResult(
                    success=False,
                    html_path=index_path,
                    error="Layout processor produced empty HTML",
                )
            tmp = target / f".{self.index_name}.tmp"
            tmp.write_text(cleaned, encoding="utf-8")
            tmp.replace(index_path)
            return LayoutProcessResult(success=True, html_path=index_path, error=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LayoutSnapshotProcessor failed: %s", exc)
            return LayoutProcessResult(
                success=False, html_path=index_path, error=str(exc)
            )

    def clean_html(self, html_text: str) -> str:
        soup = BeautifulSoup(str(html_text or ""), "html.parser")

        for tag_name in _REMOVE_TAGS:
            for node in list(soup.find_all(tag_name)):
                node.decompose()

        for node in list(soup.find_all(True)):
            if not isinstance(node, Tag):
                continue
            if self._is_tracking_node(node):
                node.decompose()
                continue
            for attr in list(node.attrs):
                if str(attr).lower().startswith("on"):
                    del node.attrs[attr]

        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            rel = " ".join(link.get("rel") or []).lower()
            href = str(link.get("href") or "")
            if _TRACKING_HREF_RE.search(href):
                link.decompose()
                continue
            if "stylesheet" in rel or rel in {"", "alternate stylesheet"}:
                continue
            if rel in {"icon", "shortcut icon", "apple-touch-icon"}:
                continue
            if any(
                token in rel
                for token in ("preload", "modulepreload", "prefetch", "preconnect")
            ):
                as_attr = str(link.get("as") or "").lower()
                if as_attr == "script" or "module" in rel:
                    link.decompose()
                    continue
                link.decompose()

        for base in list(soup.find_all("base")):
            if isinstance(base, Tag):
                base.decompose()

        out = str(soup)
        if not out.lstrip().lower().startswith("<!doctype"):
            out = "<!DOCTYPE html>\n" + out
        return out

    @staticmethod
    def _is_tracking_node(node: Tag) -> bool:
        for attr in _TRACKING_ATTR_MARKERS:
            if node.has_attr(attr):
                return True
        classes = node.get("class") or []
        class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
        if class_text and _TRACKING_CLASS_RE.search(class_text):
            return True
        node_id = str(node.get("id") or "")
        if node_id and _TRACKING_CLASS_RE.search(node_id):
            return True
        return False


def process_layout_html(
    html_text: str,
    output_dir: Path | str,
    **kwargs: Any,
) -> LayoutProcessResult:
    return LayoutSnapshotProcessor().process(html_text, output_dir, **kwargs)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def detect_challenge_html(html_text: str, status_code: int = 200) -> bool:
    """True when response looks like Cloudflare / bot challenge."""
    if int(status_code or 0) in {401, 403, 429, 503}:
        low = str(html_text or "").lower()
        if any(m in low for m in _CF_MARKERS) or "cf-" in low:
            return True
        if int(status_code) in {403, 429}:
            # Hard block without body still counts as anti-bot for Nexus/GitHub.
            return True
    low = str(html_text or "").lower()
    if "just a moment" in low or "checking your browser" in low:
        return True
    if "cf-chl" in low or "challenge-platform" in low:
        return True
    return False


def detect_bot_challenge(html_text: str, status_code: int = 200) -> bool:
    """Alias for Nexus browser-first challenge detection."""
    return detect_challenge_html(html_text, status_code)


def _safe_asset_name(url: str, *, fallback: str = "asset") -> str:
    path = unquote(urlparse(url).path or "")
    name = Path(path).name or fallback
    name = re.sub(r"[^\w.\-]+", "_", name)
    if not name or name in {".", ".."}:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        name = f"{fallback}_{digest}"
    if len(name) > 80:
        stem, suffix = Path(name).stem[:60], Path(name).suffix[:20]
        name = f"{stem}{suffix}"
    return name


def _write_metadata(
    output_dir: Path,
    *,
    provider: str,
    source_url: str,
    title: str = "",
) -> None:
    meta = {
        "provider": provider,
        "source_url": source_url,
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "title": title or "",
    }
    path = output_dir / "metadata.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


class LayoutSnapshotDownloader:
    """
    HTTP layout snapshot: HTML DOM + CSS (P0) + optional limited images.

    Ignores JS bundles, fonts, tracking, and third-party widgets.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        platform: str = "",
        download_images: bool = True,
        max_images: int = MAX_IMAGE_ASSETS,
        fetch_func: FetchFunc | None = None,
        headers: dict[str, str] | None = None,
        processor: LayoutSnapshotProcessor | None = None,
    ) -> None:
        self._owns_session = session is None and fetch_func is None
        self.session = session or (None if fetch_func else requests.Session())
        self.timeout = float(timeout)
        self.platform = normalize_platform(platform) if platform else ""
        self.download_images = bool(download_images)
        self.max_images = max(0, int(max_images))
        self.fetch_func = fetch_func
        self.headers = dict(headers or {"User-Agent": _USER_AGENT})
        self._processor = processor or LayoutSnapshotProcessor()

    def close(self) -> None:
        if self._owns_session and self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> LayoutSnapshotDownloader:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def download(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
        target = Path(output_dir)
        index_path = target / DEFAULT_INDEX_NAME
        page_url = str(url or "").strip()
        if not page_url:
            return LayoutSnapshotResult(
                success=False, html_path=index_path, error="Empty URL", backend="layout"
            )

        try:
            html_text, status, final_url = self._fetch_html(page_url)
        except Exception as exc:  # noqa: BLE001
            return LayoutSnapshotResult(
                success=False,
                html_path=index_path,
                error=str(exc),
                failure_reason=str(exc),
                backend="layout",
            )

        if detect_challenge_html(html_text, status):
            return LayoutSnapshotResult(
                success=False,
                html_path=index_path,
                error="BLOCKED_BY_ANTI_BOT",
                failure_reason="BLOCKED_BY_ANTI_BOT",
                backend="layout",
            )
        if status >= 400 or not (html_text or "").strip():
            reason = f"HTTP {status}" if status else "Empty HTML"
            return LayoutSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="layout",
            )

        base = final_url or page_url
        try:
            target.mkdir(parents=True, exist_ok=True)
            assets_dir = target / DEFAULT_ASSETS_DIR
            if assets_dir.exists():
                shutil.rmtree(assets_dir, ignore_errors=True)
            assets_dir.mkdir(parents=True, exist_ok=True)
            images_dir = assets_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)

            cleaned = self._processor.clean_html(html_text)
            soup = BeautifulSoup(cleaned, "html.parser")
            asset_count = 0
            asset_count += self._localize_css(soup, base, assets_dir)
            asset_count += self._handle_images(soup, base, images_dir)

            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            out_html = str(soup)
            if not out_html.lstrip().lower().startswith("<!doctype"):
                out_html = "<!DOCTYPE html>\n" + out_html

            tmp = target / f".{DEFAULT_INDEX_NAME}.tmp"
            tmp.write_text(out_html, encoding="utf-8")
            tmp.replace(index_path)
            _write_metadata(
                target,
                provider="layout_snapshot",
                source_url=page_url,
                title=title,
            )
            return LayoutSnapshotResult(
                success=True,
                html_path=index_path,
                asset_count=asset_count,
                backend="layout",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Layout snapshot failed for %s: %s", page_url, exc)
            return LayoutSnapshotResult(
                success=False,
                html_path=index_path,
                error=str(exc),
                failure_reason=str(exc),
                backend="layout",
            )

    def _fetch_html(self, url: str) -> tuple[str, int, str]:
        if self.fetch_func is not None:
            text, status, final = self.fetch_func(url)
            return str(text or ""), int(status or 0), str(final or url)

        assert self.session is not None
        # Attempt 1: plain session headers; attempt 2: explicit UA.
        last_exc: Exception | None = None
        for attempt_headers in (self.headers, {**self.headers, "User-Agent": _USER_AGENT}):
            try:
                resp = self.session.get(
                    url,
                    headers=attempt_headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                encoding = resp.encoding or "utf-8"
                try:
                    text = resp.content.decode(encoding, errors="replace")
                except Exception:  # noqa: BLE001
                    text = resp.text or ""
                return text, int(resp.status_code), str(resp.url or url)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("HTTP fetch failed")

    def _localize_css(self, soup: BeautifulSoup, base: str, assets_dir: Path) -> int:
        count = 0
        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            rel = " ".join(link.get("rel") or []).lower()
            href = str(link.get("href") or "").strip()
            if "stylesheet" not in rel and not href.lower().endswith(".css"):
                continue
            if not href or href.startswith("data:"):
                continue
            if _TRACKING_HREF_RE.search(href):
                link.decompose()
                continue
            abs_url = urljoin(base, href)
            if count >= MAX_CSS_ASSETS:
                # Keep structure; leave remote href (may 404 offline — allowed).
                continue
            local = self._download_asset(abs_url, assets_dir, fallback="style.css")
            if local:
                link["href"] = f"./assets/{local}"
                count += 1
        return count

    def _handle_images(self, soup: BeautifulSoup, base: str, images_dir: Path) -> int:
        count = 0
        imgs = [n for n in soup.find_all("img") if isinstance(n, Tag)]
        for img in imgs:
            src = str(img.get("src") or img.get("data-src") or "").strip()
            if not src or src.startswith("data:"):
                self._placeholder_img(img)
                continue
            abs_url = urljoin(base, src)
            classes = " ".join(img.get("class") or [])
            alt = str(img.get("alt") or "")
            is_priority = bool(_AVATAR_HINT_RE.search(classes + " " + alt + " " + src))

            if not self.download_images:
                self._placeholder_img(img)
                continue
            if count >= self.max_images and not is_priority:
                self._placeholder_img(img)
                continue
            if count >= self.max_images and is_priority and count >= self.max_images:
                self._placeholder_img(img)
                continue
            # Prefer avatars/logos when budget is tight.
            if count >= self.max_images:
                self._placeholder_img(img)
                continue
            local = self._download_asset(
                abs_url, images_dir, fallback="image.png", subdir_rel="images"
            )
            if local:
                img["src"] = f"./assets/images/{Path(local).name}"
                count += 1
            else:
                self._placeholder_img(img)
        return count

    @staticmethod
    def _placeholder_img(img: Tag) -> None:
        img["src"] = (
            "data:image/svg+xml,"
            "%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='80'%3E"
            "%3Crect fill='%23ddd' width='120' height='80'/%3E"
            "%3Ctext x='50%25' y='50%25' dominant-baseline='middle' "
            "text-anchor='middle' fill='%23888' font-size='12'%3Eimage%3C/text%3E"
            "%3C/svg%3E"
        )
        classes = img.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        else:
            classes = list(classes)
        if "smm-img-placeholder" not in classes:
            classes.append("smm-img-placeholder")
        img["class"] = classes

    def _download_asset(
        self,
        url: str,
        dest_dir: Path,
        *,
        fallback: str,
        subdir_rel: str = "",
    ) -> str | None:
        del subdir_rel
        name = _safe_asset_name(url, fallback=fallback)
        # Ensure extension for CSS/images when missing.
        suffix = Path(name).suffix.lower()
        if not suffix:
            guess = mimetypes.guess_extension("") or ""
            del guess
            if "css" in fallback or url.lower().endswith(".css"):
                name = f"{name}.css"
            else:
                name = f"{name}.bin"
        dest = dest_dir / name
        # Avoid collisions.
        if dest.exists():
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            dest = dest_dir / f"{Path(name).stem}_{digest}{Path(name).suffix}"
            name = dest.name

        try:
            if self.fetch_func is not None and url.startswith("http"):
                # Only HTML fetch_func is wired; asset fetch uses session when possible.
                pass
            if self.session is None:
                # fetch_func-only mode without session: skip binary assets.
                if self.fetch_func is not None:
                    try:
                        text, status, _ = self.fetch_func(url)
                        if status >= 400:
                            return None
                        data = text.encode("utf-8") if isinstance(text, str) else text
                        if len(data) > MAX_ASSET_BYTES:
                            return None
                        dest.write_bytes(data if isinstance(data, bytes) else bytes(data))
                        return name
                    except Exception:  # noqa: BLE001
                        return None
                return None

            resp = self.session.get(
                url, headers=self.headers, timeout=self.timeout, stream=True
            )
            if resp.status_code >= 400:
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if not Path(name).suffix:
                if "css" in ctype:
                    name = f"{Path(name).stem}.css"
                    dest = dest_dir / name
                elif "png" in ctype:
                    name = f"{Path(name).stem}.png"
                    dest = dest_dir / name
            dest.write_bytes(data)
            return name
        except Exception as exc:  # noqa: BLE001
            logger.debug("Asset download skipped %s: %s", url, exc)
            return None


# ---------------------------------------------------------------------------
# Styled fallbacks (not plain text)
# ---------------------------------------------------------------------------


def write_github_fallback(
    output_dir: Path | str,
    *,
    source_url: str,
    reason: str = "",
    title: str = "",
) -> Path:
    """GitHub-styled offline fallback: header, title, tabs, README + files cards."""
    target = Path(output_dir)
    assets = target / DEFAULT_ASSETS_DIR
    assets.mkdir(parents=True, exist_ok=True)
    css_name = "github_fallback.css"
    (assets / css_name).write_text(
        """
:root { --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --link:#2f81f7; --accent:#238636; }
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
.Header{background:#010409;border-bottom:1px solid var(--border);padding:12px 24px;display:flex;gap:12px;align-items:center}
.Header .mark{font-weight:700;color:#fff}
.wrap{max-width:1012px;margin:0 auto;padding:24px 16px 48px}
.repo-title{font-size:20px;font-weight:600;margin:0 0 16px}
.repo-title a{color:var(--link);text-decoration:none}
.tabs{display:flex;gap:8px;border-bottom:1px solid var(--border);margin-bottom:16px;padding-bottom:0}
.tabs .tab{padding:8px 12px;color:var(--muted);border-bottom:2px solid transparent}
.tabs .tab.active{color:var(--text);border-bottom-color:var(--accent)}
.grid{display:grid;grid-template-columns:1fr 280px;gap:16px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:16px}
.card h2{margin:0 0 12px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.markdown-body{line-height:1.6}
.meta{font-size:13px;color:var(--muted)}
.banner{background:#3d2a00;border:1px solid #9e6a03;color:#f0d9a8;padding:10px 12px;border-radius:6px;margin-bottom:16px;font-size:13px}
.files li{padding:6px 0;border-bottom:1px solid var(--border);list-style:none}
.files{margin:0;padding:0}
""".strip(),
        encoding="utf-8",
    )

    repo = (title or "").strip()
    if not repo:
        parts = urlparse(source_url).path.strip("/").split("/")
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
        else:
            repo = source_url or "repository"
    owner, _, name = repo.partition("/")
    notice = reason or "Remote page unavailable — showing styled offline fallback."

    index = target / DEFAULT_INDEX_NAME
    html_text = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="smm-offline-provider" content="github-fallback">
<title>{_esc(repo)} — GitHub Offline</title>
<link rel="stylesheet" href="./assets/{css_name}">
</head>
<body>
  <header class="Header"><span class="mark">GitHub</span><span class="meta">Offline snapshot</span></header>
  <div class="wrap">
    <div class="banner">{_esc(notice)}</div>
    <h1 class="repo-title"><a href="{_esc(source_url)}">{_esc(owner)}</a> / {_esc(name or repo)}</h1>
    <nav class="tabs" aria-label="Repository">
      <span class="tab active">Code</span>
      <span class="tab">Issues</span>
      <span class="tab">Pull requests</span>
      <span class="tab">Actions</span>
    </nav>
    <div class="grid">
      <section class="card readme">
        <h2>README</h2>
        <article class="markdown-body">
          <p>Offline fallback for <strong>{_esc(repo)}</strong>.</p>
          <p>Source: <a href="{_esc(source_url)}">{_esc(source_url)}</a></p>
          <p class="meta">Stars / Forks metadata was unavailable for this snapshot.</p>
        </article>
      </section>
      <aside class="card">
        <h2>About</h2>
        <p class="meta">Repository offline page (layout-preserving fallback).</p>
        <h2>Files</h2>
        <ul class="files">
          <li>README.md</li>
          <li>.gitignore</li>
          <li>LICENSE</li>
        </ul>
      </aside>
    </div>
  </div>
</body>
</html>
"""
    tmp = target / ".index.html.tmp"
    tmp.write_text(html_text, encoding="utf-8")
    tmp.replace(index)
    _write_metadata(
        target, provider="github_fallback", source_url=source_url, title=repo
    )
    return index


def write_nexus_fallback(
    output_dir: Path | str,
    *,
    source_url: str,
    reason: str = "",
    title: str = "",
    description: str = "",
    author: str = "",
    mod_id: str = "",
    external_id: str = "",
    files: list[str] | None = None,
    cover_href: str = "",
    notes: str = "",
) -> Path:
    """Nexus-styled offline fallback: hero title, description | metadata, files."""
    target = Path(output_dir)
    assets = target / DEFAULT_ASSETS_DIR
    assets.mkdir(parents=True, exist_ok=True)
    css_name = "nexus_fallback.css"
    (assets / css_name).write_text(
        """
:root{--bg:#121212;--panel:#1d1d1d;--border:#2c2c2c;--text:#f0f0f0;--muted:#9a9a9a;--accent:#da8c2d;--link:#7eb8da}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;background:var(--bg);color:var(--text)}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
.hero{background:linear-gradient(120deg,#1a2332,#241810);border:1px solid var(--border);border-radius:8px;padding:20px 24px;margin-bottom:16px}
.hero h1{margin:0 0 8px;font-size:26px;color:var(--accent)}
.banner{background:#3d2a12;border:1px solid #c47a20;color:#f0d9a8;padding:10px 12px;border-radius:6px;margin-bottom:16px;font-size:13px}
.layout{display:grid;grid-template-columns:1fr 300px;gap:16px}
@media(max-width:860px){.layout{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px}
.panel h2{margin:0 0 12px;font-size:15px;color:var(--link);border-bottom:1px solid var(--border);padding-bottom:6px}
.desc{line-height:1.55;white-space:pre-wrap}
.meta-row{display:grid;grid-template-columns:90px 1fr;gap:6px;margin:6px 0;font-size:13px}
.k{color:var(--muted)}
.files{list-style:none;margin:0;padding:0}
.files li{padding:8px 0;border-bottom:1px solid var(--border)}
.cover{width:112px;height:112px;object-fit:cover;border-radius:8px;border:1px solid var(--border);float:right;margin:0 0 8px 12px}
a{color:var(--link)}
""".strip(),
        encoding="utf-8",
    )

    title_text = (title or "").strip() or "Nexus Mod"
    notice = "远程页面受Cloudflare保护，已生成本地信息页"
    if reason:
        notice = f"{notice}（{reason}）"
    desc = (description or "").strip() or (
        "Mod description was not available from the remote page. "
        "This structured offline page preserves Nexus-style layout."
    )
    author_text = (author or "").strip() or "—"
    id_value = (external_id or mod_id or "").strip() or "—"
    file_rows = list(files or [])
    if file_rows:
        files_html = "".join(f"<li>{_esc(name)}</li>" for name in file_rows)
    else:
        files_html = (
            "<li>Main files (unavailable offline)</li>"
            "<li>Optional files (unavailable offline)</li>"
        )
    cover_html = (
        f'<img class="cover" src="{_esc(cover_href)}" alt="cover">' if cover_href else ""
    )
    notes_html = (
        f'<h2>备注</h2><div class="desc">{_esc(notes)}</div>'
        if (notes or "").strip()
        else ""
    )

    index = target / DEFAULT_INDEX_NAME
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="smm-offline-provider" content="nexus-fallback">
<title>{_esc(title_text)} — Nexus Offline</title>
<link rel="stylesheet" href="./assets/{css_name}">
</head>
<body>
  <div class="wrap">
    <div class="banner">{_esc(notice)}</div>
    <header class="hero">
      {cover_html}
      <h1>{_esc(title_text)}</h1>
      <div class="meta-row"><span class="k">Source</span><span><a href="{_esc(source_url)}">{_esc(source_url)}</a></span></div>
    </header>
    <div class="layout">
      <section class="panel">
        <h2>Description</h2>
        <div class="desc">{_esc(desc)}</div>
        <h2>Requirements</h2>
        <p class="desc">Requirements were not captured for this offline snapshot.</p>
        <h2>Files</h2>
        <ul class="files">
          {files_html}
        </ul>
        {notes_html}
      </section>
      <aside class="panel">
        <h2>Metadata</h2>
        <div class="meta-row"><span class="k">Author</span><span>{_esc(author_text)}</span></div>
        <div class="meta-row"><span class="k">Platform</span><span>Nexus Mods</span></div>
        <div class="meta-row"><span class="k">Mod ID</span><span><code>{_esc(id_value)}</code></span></div>
        <div class="meta-row"><span class="k">Status</span><span>Offline fallback</span></div>
      </aside>
    </div>
  </div>
</body>
</html>
"""
    tmp = target / ".index.html.tmp"
    tmp.write_text(html_text, encoding="utf-8")
    tmp.replace(index)
    _write_metadata(
        target, provider="nexus_fallback", source_url=source_url, title=title_text
    )
    return index


# ---------------------------------------------------------------------------
# Platform providers
# ---------------------------------------------------------------------------


def _browser_css_only_snapshot(
    url: str,
    output_dir: Path,
    *,
    browser_provider: Any | None = None,
) -> LayoutSnapshotResult:
    """Playwright primary path: ``page.content()`` + LayoutSnapshotProcessor."""
    index_path = output_dir / DEFAULT_INDEX_NAME

    if browser_provider is not None:
        snap = browser_provider.snapshot(url, output_dir)
        success = bool(getattr(snap, "success", False))
        html_path = Path(getattr(snap, "html_path", index_path))
        error = getattr(snap, "error", None)
        # Never keep challenge pages from an injected provider.
        if success and html_path.is_file():
            try:
                body = html_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            if detect_challenge_html(body):
                try:
                    html_path.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.info(
                    "[NEXUS_OFFLINE] stage=browser status=fail detail=challenge_not_saved"
                )
                return LayoutSnapshotResult(
                    success=False,
                    html_path=index_path,
                    error="BLOCKED_BY_ANTI_BOT",
                    failure_reason="BLOCKED_BY_ANTI_BOT",
                    backend="browser",
                )
        return LayoutSnapshotResult(
            success=success and html_path.is_file(),
            html_path=html_path,
            asset_count=int(getattr(snap, "asset_count", 0) or 0),
            error=str(error) if error else None,
            failure_reason=str(error) if error else None,
            backend="browser",
            used_fallback=bool(getattr(snap, "used_fallback", False)),
        )

    try:
        from services.offline.browser_snapshot.playwright_capture import (
            PlaywrightCapture,
            PlaywrightCaptureError,
            detect_bot_challenge,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[NEXUS_OFFLINE] stage=browser status=fail detail=import")
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error=f"Browser unavailable: {exc}",
            failure_reason=str(exc),
            backend="browser",
        )

    capture = PlaywrightCapture(
        timeout_ms=30_000,
        render_wait_ms=5_000,
        wait_until="domcontentloaded",
    )
    try:
        page = capture.capture(url)
    except PlaywrightCaptureError as exc:
        code = exc.code or str(exc)
        logger.info("[NEXUS_OFFLINE] stage=browser status=fail detail=%s", code)
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error=code,
            failure_reason=code,
            backend="browser",
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[NEXUS_OFFLINE] stage=browser status=fail detail=%s", exc)
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error=str(exc),
            failure_reason=str(exc),
            backend="browser",
        )

    html_text = page.html or ""
    if detect_bot_challenge(html_text) or detect_challenge_html(html_text):
        logger.info(
            "[NEXUS_OFFLINE] stage=browser status=fail detail=BLOCKED_BY_ANTI_BOT"
        )
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error="BLOCKED_BY_ANTI_BOT",
            failure_reason="BLOCKED_BY_ANTI_BOT",
            backend="browser",
        )

    processed = LayoutSnapshotProcessor().process(
        html_text, output_dir, page_url=page.page_url or url
    )
    if not processed.success or not processed.html_path.is_file():
        err = processed.error or "Layout processor failed"
        logger.info("[NEXUS_OFFLINE] stage=browser status=fail detail=%s", err)
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error=err,
            failure_reason=err,
            backend="browser",
        )

    try:
        saved = processed.html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        saved = ""
    if detect_challenge_html(saved) or detect_bot_challenge(saved):
        try:
            processed.html_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info(
            "[NEXUS_OFFLINE] stage=browser status=fail detail=challenge_not_saved"
        )
        return LayoutSnapshotResult(
            success=False,
            html_path=index_path,
            error="BLOCKED_BY_ANTI_BOT",
            failure_reason="BLOCKED_BY_ANTI_BOT",
            backend="browser",
        )

    if not (output_dir / "metadata.json").is_file():
        _write_metadata(
            output_dir, provider="nexus_browser", source_url=url, title=""
        )
    logger.info("[NEXUS_OFFLINE] stage=browser status=success")
    return LayoutSnapshotResult(
        success=True,
        html_path=processed.html_path,
        asset_count=0,
        backend="browser",
    )


class GitHubSnapshotProvider:
    """
    Browser-first GitHub repository snapshot (Playwright ``page.content()`` only).

    Delegates to :class:`services.offline.github_browser_snapshot.GitHubBrowserSnapshot`.
    Does not generate fallback / summary pages.
    """

    def __init__(
        self,
        *,
        downloader: LayoutSnapshotDownloader | None = None,
        allow_browser_backup: bool = True,
        browser_provider: Any | None = None,
        title: str = "",
        capture_func: Any | None = None,
    ) -> None:
        del downloader, allow_browser_backup, browser_provider
        self.title = title
        self._capture_func = capture_func

    def snapshot(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
        from services.offline.github_browser_snapshot import GitHubBrowserSnapshot

        with GitHubBrowserSnapshot(
            capture_func=self._capture_func, title=self.title
        ) as provider:
            result = provider.snapshot(url, output_dir)

        return LayoutSnapshotResult(
            success=bool(result.success and result.html_path.is_file()),
            html_path=result.html_path,
            asset_count=0,
            error=result.error,
            failure_reason=result.error,
            backend="playwright",
            used_fallback=False,
        )


class NexusSnapshotProvider:
    """
    Browser-first Nexus offline snapshot (never requests/httpx as primary).

    1. Playwright ``domcontentloaded`` + ``page.content()``
    2. :class:`LayoutSnapshotProcessor`
    3. Optional injected HTTP downloader only when browser is disabled (tests)
       or after browser failure when a downloader was explicitly provided
    4. Styled Nexus fallback
    """

    enable_legacy_fallback: bool = False

    def __init__(
        self,
        *,
        downloader: LayoutSnapshotDownloader | None = None,
        allow_browser_backup: bool = True,
        browser_provider: Any | None = None,
        title: str = "",
        description: str = "",
        author: str = "",
        prefer_browser: bool = True,
    ) -> None:
        self._downloader = downloader
        # Historical flag: False skips browser entirely (test injection).
        self.allow_browser_backup = bool(allow_browser_backup)
        self.prefer_browser = bool(prefer_browser)
        self._browser_provider = browser_provider
        self.title = title
        self.description = description
        self.author = author
        self.enable_legacy_fallback = False

    def snapshot(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
        target = Path(output_dir)
        page_url = str(url or "").strip()
        errors: list[str] = []
        use_browser = self.allow_browser_backup and self.prefer_browser

        # Stage: browser first (Nexus must not lead with requests/httpx).
        if use_browser:
            browser = _browser_css_only_snapshot(
                page_url, target, browser_provider=self._browser_provider
            )
            if browser.success and browser.html_path.is_file() and not browser.used_fallback:
                return browser
            if browser.error:
                errors.append(browser.error)

        # HTTP layout only when browser disabled (tests) or an explicit downloader
        # was injected — never invent a default HTTP-first path for Nexus.
        if (not use_browser) or (self._downloader is not None):
            dl = self._downloader or LayoutSnapshotDownloader(platform=PLATFORM_NEXUS)
            owns = self._downloader is None
            try:
                result = dl.download(page_url, target)
            finally:
                if owns:
                    dl.close()
            if result.success and result.html_path.is_file():
                try:
                    body = result.html_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    body = ""
                if detect_challenge_html(body):
                    try:
                        result.html_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    errors.append("BLOCKED_BY_ANTI_BOT")
                else:
                    return result
            errors.append(result.error or result.failure_reason or "layout failed")

        reason = errors[-1] if errors else "snapshot failed"
        index = write_nexus_fallback(
            target,
            source_url=page_url,
            reason=reason,
            title=self.title,
            description=self.description,
            author=self.author,
        )
        logger.info("[NEXUS_OFFLINE] stage=fallback status=success detail=%s", reason)
        return LayoutSnapshotResult(
            success=True,
            html_path=index,
            backend="fallback",
            used_fallback=True,
            error=reason,
            failure_reason=reason,
            asset_count=1,
        )


def run_layout_offline_snapshot(
    *,
    source_url: str,
    output_dir: Path | str,
    platform: str,
    layout_provider: Any | None = None,
    title: str = "",
) -> tuple[LayoutSnapshotResult, str]:
    """
    Run layout snapshot for one Mod page.

    Returns ``(result, offline_status)``.
    - Success → archived
    - Nexus styled fallback → failed (page still openable)
    - GitHub never writes a summary fallback — Playwright DOM only
    """
    plat = normalize_platform(platform)
    target = Path(output_dir)
    page_url = str(source_url or "").strip()
    provider = layout_provider
    if provider is None:
        if plat == PLATFORM_GITHUB:
            provider = GitHubSnapshotProvider(title=title)
        else:
            provider = NexusSnapshotProvider(title=title)

    result = provider.snapshot(page_url, target)
    if result.success and result.html_path.is_file() and not result.used_fallback:
        return result, OFFLINE_STATUS_ARCHIVED
    if result.success and result.html_path.is_file() and result.used_fallback:
        return result, OFFLINE_STATUS_FAILED

    # GitHub: fail hard — do not invent a summary / LOGIN_REQUIRED page.
    if plat == PLATFORM_GITHUB:
        return result, OFFLINE_STATUS_FAILED

    reason = result.error or result.failure_reason or "snapshot failed"
    index = write_nexus_fallback(
        target, source_url=page_url, reason=reason, title=title
    )
    return (
        LayoutSnapshotResult(
            success=True,
            html_path=index,
            backend="fallback",
            used_fallback=True,
            error=reason,
            failure_reason=reason,
            asset_count=1,
        ),
        OFFLINE_STATUS_FAILED,
    )
