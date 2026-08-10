"""Readable offline page snapshots for Nexus / GitHub (not full site clones).

Flow: fetch HTML → parse main content → write static ``index.html`` + ``style.css``.

Does not download JS bundles, ads, tracking widgets, or image assets.
Images keep placeholders only.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    normalize_platform,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_INDEX_NAME = "index.html"
DEFAULT_STYLE_NAME = "style.css"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# HTTP statuses that typically indicate Cloudflare / bot challenge.
CHALLENGE_STATUSES = frozenset({401, 403, 429, 503})

_CF_MARKERS = (
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "just a moment",
    "checking your browser",
    "attention required",
    "cloudflare",
    "cf-ray",
)

_NOISE_TAGS = frozenset(
    {
        "script",
        "noscript",
        "iframe",
        "object",
        "embed",
        "link",
        "style",
        "svg",
    }
)

_NOISE_SELECTORS = (
    "[data-tracking]",
    "[data-ad]",
    ".ads",
    ".ad-container",
    ".advertisement",
    "#onetrust-banner-sdk",
    ".cookie-banner",
    ".login-widget",
    ".premium-banner",
    "nav",
    "footer",
    "header.global-header",
    ".site-header",
    ".Header",
    ".js-header-wrapper",
)


@dataclass
class FileEntry:
    """One file / release row shown on the offline page."""

    name: str
    detail: str = ""
    size: str = ""
    version: str = ""


@dataclass
class ReadablePage:
    """Parsed readable content extracted from a remote page."""

    title: str = ""
    author: str = ""
    description_html: str = ""
    files: list[FileEntry] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str = ""
    requirements: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    source_url: str = ""
    platform: str = ""
    raw_sections: dict[str, str] = field(default_factory=dict)


@dataclass
class ReadableSnapshotResult:
    """Outcome of ``ReadableSnapshotProvider.snapshot``."""

    success: bool
    html_path: Path
    css_path: Path | None = None
    error: str | None = None
    backend: str = "readable"  # readable | browser | fallback
    used_fallback: bool = False
    asset_count: int = 0
    failure_reason: str = ""


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _text(node: Tag | NavigableString | None) -> str:
    if node is None:
        return ""
    if isinstance(node, NavigableString):
        return str(node).strip()
    return " ".join(node.stripped_strings)


def _is_cloudflare_challenge(html_text: str, status_code: int = 200) -> bool:
    if status_code in CHALLENGE_STATUSES:
        return True
    lowered = (html_text or "").lower()
    if not lowered:
        return status_code >= 400
    hits = sum(1 for marker in _CF_MARKERS if marker in lowered)
    # Require a couple of markers, or a challenge title with cf-ray.
    if hits >= 2:
        return True
    if "just a moment" in lowered and ("cloudflare" in lowered or "cf-" in lowered):
        return True
    return False


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag_name in _NOISE_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()
    for selector in _NOISE_SELECTORS:
        try:
            for node in soup.select(selector):
                node.decompose()
        except Exception:  # noqa: BLE001
            continue


def _replace_images_with_placeholders(root: Tag) -> None:
    """Keep image presence as placeholders; never download binary assets."""
    for img in list(root.find_all("img")):
        if not isinstance(img, Tag):
            continue
        src = (
            str(img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "")
            .strip()
        )
        alt = str(img.get("alt") or "Image").strip() or "Image"
        placeholder = BeautifulSoup("", "html.parser").new_tag("span")
        placeholder.attrs["class"] = "img-placeholder"
        if src:
            placeholder.attrs["data-src"] = src
            placeholder.attrs["title"] = f"Image not downloaded: {src}"
        placeholder.string = f"[Image: {alt}]"
        img.replace_with(placeholder)
    # Drop srcset / lazy background hooks that would pull remote assets.
    for tag in root.find_all(True):
        if not isinstance(tag, Tag):
            continue
        classes = tag.get("class") or []
        if "img-placeholder" in classes:
            continue
        for attr in ("srcset", "data-srcset", "data-src", "data-lazy-src", "src"):
            if attr in tag.attrs:
                del tag.attrs[attr]


def _sanitize_fragment(node: Tag | None) -> str:
    if node is None:
        return ""
    working = BeautifulSoup(str(node), "html.parser")
    _strip_noise(working)
    root = working.body if working.body else working
    if not isinstance(root, Tag):
        return _esc(_text(node))
    _replace_images_with_placeholders(root)
    for tag in root.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs):
            low = str(attr).lower()
            if low.startswith("on") or low.startswith("data-tracking"):
                del tag.attrs[attr]
    return "".join(str(c) for c in root.contents).strip()


def _first_match(soup: BeautifulSoup, selectors: Sequence[str]) -> Tag | None:
    for selector in selectors:
        try:
            node = soup.select_one(selector)
        except Exception:  # noqa: BLE001
            node = None
        if isinstance(node, Tag):
            return node
    return None


def _collect_list_texts(root: Tag | None, selectors: Sequence[str]) -> list[str]:
    if root is None:
        return []
    found: list[str] = []
    for selector in selectors:
        try:
            nodes = root.select(selector)
        except Exception:  # noqa: BLE001
            continue
        for node in nodes:
            text = _text(node)
            if text and text not in found:
                found.append(text)
    return found


class NexusReadableParser:
    """Extract title / description / files / metadata from a Nexus mod page."""

    TITLE_SELECTORS = (
        "h1",
        ".mod-page h1",
        "[class*='modHeader'] h1",
        "meta[property='og:title']",
    )
    MAIN_SELECTORS = (
        "main",
        "article",
        ".mod-page",
        "#mainContent",
        ".container",
    )
    DESC_SELECTORS = (
        ".mod-description",
        "#mod-description",
        "[class*='Description']",
        ".prose",
        "article .bbcode",
        ".modpage-description",
    )
    AUTHOR_SELECTORS = (
        "a[href*='/users/']",
        ".author",
        "[class*='Uploader'] a",
        "meta[name='author']",
    )
    TAG_SELECTORS = (
        ".tags a",
        ".tag",
        "[class*='Tag'] a",
        "a[href*='/mods/?'][href*='tag']",
    )
    FILE_ROW_SELECTORS = (
        "table tr",
        ".file-row",
        "[class*='FileRow']",
        "li.file",
    )
    REQUIREMENT_SELECTORS = (
        ".requirements li",
        "[class*='Requirement'] li",
        "[class*='requirements'] a",
        "#Requirements li",
    )

    def parse(self, html_text: str, *, source_url: str = "") -> ReadablePage:
        soup = BeautifulSoup(html_text or "", "html.parser")
        page = ReadablePage(source_url=source_url, platform=PLATFORM_NEXUS)

        title_node = _first_match(soup, self.TITLE_SELECTORS)
        if title_node is not None:
            if title_node.name == "meta":
                page.title = str(title_node.get("content") or "").strip()
            else:
                page.title = _text(title_node)
        if not page.title:
            page.title = _text(soup.title) or "Nexus Mod"

        author_node = _first_match(soup, self.AUTHOR_SELECTORS)
        if author_node is not None:
            if author_node.name == "meta":
                page.author = str(author_node.get("content") or "").strip()
            else:
                page.author = _text(author_node)

        main = _first_match(soup, self.MAIN_SELECTORS) or soup.body
        desc = _first_match(soup, self.DESC_SELECTORS)
        if desc is None and isinstance(main, Tag):
            desc = main
        page.description_html = _sanitize_fragment(desc if isinstance(desc, Tag) else None)

        page.tags = _collect_list_texts(soup, self.TAG_SELECTORS)[:30]
        page.requirements = _collect_list_texts(soup, self.REQUIREMENT_SELECTORS)[:40]

        # Version / metadata side panel heuristics.
        for label, value in self._iter_meta_pairs(soup):
            key = label.strip().rstrip(":")
            if not key or not value:
                continue
            page.metadata[key] = value
            low = key.lower()
            if "version" in low and not page.version:
                page.version = value
            if "author" in low and not page.author:
                page.author = value

        page.files = self._extract_files(soup)
        if page.version:
            page.metadata.setdefault("Version", page.version)
        if page.author:
            page.metadata.setdefault("Author", page.author)
        return page

    def _iter_meta_pairs(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                pairs.append((_text(dt), _text(dd)))
        for row in soup.select(".row, .stat, [class*='meta'] li, dl > div"):
            if not isinstance(row, Tag):
                continue
            text = _text(row)
            if ":" in text:
                left, right = text.split(":", 1)
                if left.strip() and right.strip() and len(left) < 40:
                    pairs.append((left.strip(), right.strip()))
        return pairs

    def _extract_files(self, soup: BeautifulSoup) -> list[FileEntry]:
        files: list[FileEntry] = []
        seen: set[str] = set()
        for selector in self.FILE_ROW_SELECTORS:
            try:
                rows = soup.select(selector)
            except Exception:  # noqa: BLE001
                continue
            for row in rows:
                if not isinstance(row, Tag):
                    continue
                # Prefer named file cells.
                name = ""
                for cand in row.select("a, .file-name, td, span"):
                    t = _text(cand)
                    if t and len(t) > 1:
                        name = t
                        break
                if not name:
                    name = _text(row)
                name = name.strip()
                if not name or len(name) > 200:
                    continue
                # Skip obvious UI chrome.
                low = name.lower()
                if low in {"name", "file", "files", "download", "size", "date"}:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                detail_bits = [
                    _text(c)
                    for c in row.select("td, .size, .version, time")
                    if _text(c) and _text(c) != name
                ]
                files.append(
                    FileEntry(
                        name=name,
                        detail=" · ".join(detail_bits[:3]),
                    )
                )
                if len(files) >= 40:
                    return files
        return files


class GithubReadableParser:
    """Extract README, repository info, and release info from a GitHub page."""

    TITLE_SELECTORS = (
        "strong[itemprop='name'] a",
        "[itemprop='name'] a",
        "h1 strong a",
        "h1",
        "meta[property='og:title']",
    )
    README_SELECTORS = (
        "article.markdown-body",
        ".markdown-body",
        "#readme .markdown-body",
        "#readme",
        "article",
    )
    ABOUT_SELECTORS = (
        ".BorderGrid-cell",
        "[data-hpc] .mb-3",
        ".Layout-sidebar",
    )
    RELEASE_SELECTORS = (
        ".release",
        "[data-test-selector='release']",
        ".Box .Link--primary",
        "a[href*='/releases/tag/']",
    )

    def parse(self, html_text: str, *, source_url: str = "") -> ReadablePage:
        soup = BeautifulSoup(html_text or "", "html.parser")
        page = ReadablePage(source_url=source_url, platform=PLATFORM_GITHUB)

        title_node = _first_match(soup, self.TITLE_SELECTORS)
        if title_node is not None:
            if title_node.name == "meta":
                page.title = str(title_node.get("content") or "").strip()
            else:
                page.title = _text(title_node)
        if not page.title:
            # owner/repo from URL
            parts = urlparse(source_url).path.strip("/").split("/")
            if len(parts) >= 2:
                page.title = f"{parts[0]}/{parts[1]}"
            else:
                page.title = _text(soup.title) or "GitHub Repository"

        # Author = repo owner
        parts = urlparse(source_url).path.strip("/").split("/")
        if parts:
            page.author = parts[0]

        readme = _first_match(soup, self.README_SELECTORS)
        page.description_html = _sanitize_fragment(
            readme if isinstance(readme, Tag) else None
        )
        if not page.description_html:
            # Fallback: about description
            about = soup.select_one("[itemprop='about'], .f4.my-3")
            if about is not None:
                page.description_html = f"<p>{_esc(_text(about))}</p>"

        # Topics / tags
        page.tags = _collect_list_texts(
            soup,
            (
                "a.topic-tag",
                "[data-ga-click*='topic']",
                ".topic-tag-link",
            ),
        )[:30]

        # Releases as file-like entries
        for node in soup.select(",".join(self.RELEASE_SELECTORS)):
            if not isinstance(node, Tag):
                continue
            name = _text(node)
            if not name:
                continue
            href = str(node.get("href") or "")
            if "/releases" not in href and "release" not in name.lower():
                # Keep tag links
                if "/tag/" not in href:
                    continue
            if any(f.name == name for f in page.files):
                continue
            page.files.append(FileEntry(name=name, detail=href))
            if len(page.files) >= 20:
                break

        # Repo metadata from sidebar
        for label, value in self._sidebar_pairs(soup):
            page.metadata[label] = value
            if "license" in label.lower():
                page.metadata.setdefault("License", value)
            if label.lower() in {"stars", "star", "watchers", "forks"}:
                page.metadata[label] = value

        page.metadata.setdefault("Repository", page.title)
        if page.author:
            page.metadata.setdefault("Owner", page.author)
        return page

    def _sidebar_pairs(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for h2 in soup.select("h2, h3"):
            label = _text(h2)
            if not label or len(label) > 40:
                continue
            sibling = h2.find_next_sibling()
            value = _text(sibling) if sibling is not None else ""
            if value and value != label:
                pairs.append((label, value[:200]))
        # Stars / forks counters
        for sel, label in (
            ("#repo-stars-counter-star", "Stars"),
            ("#repo-network-counter", "Forks"),
            ("strong[aria-label*='star']", "Stars"),
        ):
            node = soup.select_one(sel)
            if node is not None and _text(node):
                pairs.append((label, _text(node)))
        return pairs


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "nexusmods.com" in host:
        return PLATFORM_NEXUS
    if "github.com" in host or "githubusercontent.com" in host:
        return PLATFORM_GITHUB
    return ""


READABLE_STYLE_CSS = """\
/* Readable offline snapshot — card / section layout (no remote assets) */
:root {
  --bg: #0f1419;
  --panel: #171b22;
  --panel-2: #1c222b;
  --border: #2c3642;
  --text: #d7dee8;
  --muted: #8b97a8;
  --accent: #6cb2eb;
  --accent-2: #3d8fd1;
  --warn: #e6a23c;
  --danger: #e07070;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
  background: linear-gradient(180deg, #121820 0%, var(--bg) 40%);
  color: var(--text);
  line-height: 1.55;
  padding: 28px 16px 48px;
}
.page {
  max-width: 920px;
  margin: 0 auto;
}
.banner {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 12px;
}
.banner.fail {
  color: var(--danger);
  background: rgba(224, 112, 112, 0.08);
  border: 1px solid rgba(224, 112, 112, 0.35);
  border-radius: 6px;
  padding: 10px 12px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 22px 24px;
  margin-bottom: 16px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
}
.header h1 {
  margin: 0 0 8px;
  font-size: 26px;
  font-weight: 650;
  color: var(--accent);
  letter-spacing: 0.01em;
}
.badge {
  display: inline-block;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--accent);
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 4px;
  margin-right: 6px;
}
.meta-grid {
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 8px 12px;
  font-size: 14px;
}
.meta-grid .k { color: var(--muted); }
.meta-grid .v { color: var(--text); word-break: break-word; }
.section-title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 650;
  color: var(--accent);
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}
.content {
  font-size: 14px;
  color: #c5ced9;
}
.content h1, .content h2, .content h3 {
  color: var(--accent-2);
  margin-top: 1.2em;
}
.content pre, .content code {
  background: #0c1016;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.content pre {
  padding: 12px;
  overflow: auto;
  max-height: 420px;
}
.content a { color: var(--accent); }
.img-placeholder {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 120px;
  min-height: 64px;
  padding: 10px 14px;
  margin: 6px 0;
  background: var(--panel-2);
  border: 1px dashed var(--border);
  border-radius: 6px;
  color: var(--muted);
  font-size: 12px;
}
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.tag {
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
}
.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.table th, .table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.table th { color: var(--muted); font-weight: 600; }
.table td.name { color: var(--text); }
.table td.detail { color: var(--muted); font-size: 13px; }
.list { margin: 0; padding-left: 18px; }
.list li { margin: 4px 0; }
.muted { color: var(--muted); font-size: 13px; }
a { color: var(--accent); }
"""


def render_readable_html(page: ReadablePage, *, failure_reason: str = "") -> str:
    """Build unified offline HTML (header / metadata / content / files)."""
    plat = normalize_platform(page.platform) or detect_platform(page.source_url)
    platform_label = {
        PLATFORM_NEXUS: "Nexus Mods",
        PLATFORM_GITHUB: "GitHub",
    }.get(plat, plat or "Remote")
    title = (page.title or "").strip() or "Untitled"
    author = (page.author or "").strip()
    version = (page.version or page.metadata.get("Version") or "").strip()

    meta_rows: list[str] = [
        f'<div class="k">Platform</div><div class="v">{_esc(platform_label)}</div>'
    ]
    if author:
        meta_rows.append(
            f'<div class="k">Author</div><div class="v">{_esc(author)}</div>'
        )
    if version:
        meta_rows.append(
            f'<div class="k">Version</div><div class="v">{_esc(version)}</div>'
        )
    if page.source_url:
        meta_rows.append(
            f'<div class="k">Source</div><div class="v">'
            f'<a href="{_esc(page.source_url)}">{_esc(page.source_url)}</a></div>'
        )
    for key, value in page.metadata.items():
        if key.lower() in {"author", "version", "platform", "source"}:
            continue
        if not value:
            continue
        meta_rows.append(
            f'<div class="k">{_esc(key)}</div><div class="v">{_esc(value)}</div>'
        )

    tags_html = ""
    if page.tags:
        chips = "".join(f'<span class="tag">{_esc(t)}</span>' for t in page.tags)
        tags_html = f'<div class="tags" style="margin-top:12px">{chips}</div>'

    req_html = ""
    if page.requirements:
        items = "".join(f"<li>{_esc(r)}</li>" for r in page.requirements)
        req_html = f"""
    <section class="card">
      <h2 class="section-title">Requirements</h2>
      <ul class="list">{items}</ul>
    </section>"""

    files_html = '<p class="muted">No file list extracted.</p>'
    if page.files:
        rows = []
        for entry in page.files:
            rows.append(
                "<tr>"
                f'<td class="name">{_esc(entry.name)}</td>'
                f'<td class="detail">{_esc(entry.detail or entry.size or entry.version)}</td>'
                "</tr>"
            )
        files_html = (
            '<table class="table"><thead><tr><th>Name</th><th>Detail</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    content = page.description_html.strip() or '<p class="muted">No description.</p>'
    fail_banner = ""
    if failure_reason:
        fail_banner = (
            f'<div class="banner fail">保存失败 / Offline save notice: '
            f"{_esc(failure_reason)}</div>"
        )
    else:
        fail_banner = (
            '<div class="banner">Readable offline page · images not downloaded · '
            "JavaScript disabled</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="smm-offline-provider" content="readable">
<meta name="smm-offline-platform" content="{_esc(plat)}">
<title>{_esc(title)} — Offline</title>
<link rel="stylesheet" href="./{_esc(DEFAULT_STYLE_NAME)}">
</head>
<body>
  <div class="page">
    {fail_banner}
    <header class="card header">
      <h1>{_esc(title)}</h1>
      <span class="badge">{_esc(platform_label)}</span>
      {f'<span class="badge">v{_esc(version)}</span>' if version else ''}
      {tags_html}
    </header>
    <section class="card">
      <h2 class="section-title">Metadata</h2>
      <div class="meta-grid">
        {''.join(meta_rows)}
      </div>
    </section>
    <section class="card">
      <h2 class="section-title">Content</h2>
      <div class="content">
        {content}
      </div>
    </section>
    {req_html}
    <section class="card">
      <h2 class="section-title">Files</h2>
      {files_html}
    </section>
  </div>
</body>
</html>
"""


def write_readable_page(
    output_dir: Path | str,
    page: ReadablePage,
    *,
    failure_reason: str = "",
) -> tuple[Path, Path]:
    """Write ``index.html`` + ``style.css`` under *output_dir*."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    # Avoid leaving previous full-site asset trees as the primary view.
    index = target / DEFAULT_INDEX_NAME
    css = target / DEFAULT_STYLE_NAME
    html_text = render_readable_html(page, failure_reason=failure_reason)
    tmp_html = target / f".{DEFAULT_INDEX_NAME}.tmp"
    tmp_css = target / f".{DEFAULT_STYLE_NAME}.tmp"
    tmp_html.write_text(html_text, encoding="utf-8")
    tmp_css.write_text(READABLE_STYLE_CSS, encoding="utf-8")
    tmp_html.replace(index)
    tmp_css.replace(css)
    return index, css


def write_failure_page(
    output_dir: Path | str,
    *,
    source_url: str,
    platform: str = "",
    reason: str,
    title: str = "Offline page unavailable",
) -> tuple[Path, Path]:
    """Generate a minimal readable fallback page recording the failure reason."""
    page = ReadablePage(
        title=title,
        source_url=source_url,
        platform=normalize_platform(platform) or detect_platform(source_url),
        description_html=(
            "<p>Could not build a readable offline snapshot from the remote page.</p>"
            f"<p><strong>Reason / 失败原因:</strong> {_esc(reason)}</p>"
            "<p class=\"muted\">Cloudflare / 反爬拦截时不会回退到旧版整页抓取。</p>"
        ),
        metadata={"Status": "failed"},
    )
    return write_readable_page(output_dir, page, failure_reason=reason)


class ReadableSnapshotProvider:
    """
    Primary offline path for Nexus / GitHub: readable static HTML, not site clones.

    - Fetches HTML via ``requests`` (injectable session for tests)
    - Parses main content with platform-specific selectors
    - Writes ``index.html`` + local ``style.css``
    - Does **not** download images / JS
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        nexus_parser: NexusReadableParser | None = None,
        github_parser: GithubReadableParser | None = None,
        fetch_func: Callable[[str], tuple[str, int, str]] | None = None,
    ) -> None:
        self._session = session
        self.timeout = float(timeout)
        self._headers = dict(headers or {"User-Agent": _USER_AGENT})
        self._nexus_parser = nexus_parser or NexusReadableParser()
        self._github_parser = github_parser or GithubReadableParser()
        self._fetch_func = fetch_func
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def __enter__(self) -> ReadableSnapshotProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self._headers)
        return self._session

    def fetch_html(self, url: str) -> tuple[str, int, str]:
        """Return ``(html_text, status_code, final_url)``."""
        if self._fetch_func is not None:
            return self._fetch_func(url)
        page_url = str(url or "").strip()
        if not page_url:
            raise ValueError("Empty URL")
        response = self._get_session().get(page_url, timeout=self.timeout)
        text = response.text or ""
        return text, int(response.status_code), str(response.url or page_url)

    def parse_html(
        self,
        html_text: str,
        *,
        source_url: str = "",
        platform: str = "",
    ) -> ReadablePage:
        plat = normalize_platform(platform) or detect_platform(source_url)
        if plat == PLATFORM_GITHUB:
            return self._github_parser.parse(html_text, source_url=source_url)
        # Default / Nexus
        return self._nexus_parser.parse(html_text, source_url=source_url)

    def snapshot(
        self,
        url: str,
        output_dir: Path | str,
        *,
        platform: str = "",
    ) -> ReadableSnapshotResult:
        """Fetch + parse + write readable offline page into *output_dir*."""
        target = Path(output_dir)
        index_path = target / DEFAULT_INDEX_NAME
        page_url = str(url or "").strip()
        if not page_url:
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error="Empty URL",
                failure_reason="Empty URL",
                backend="readable",
            )

        plat = normalize_platform(platform) or detect_platform(page_url)
        try:
            html_text, status, final_url = self.fetch_html(page_url)
        except Exception as exc:  # noqa: BLE001
            reason = f"Fetch failed: {exc}"
            logger.info("Readable snapshot fetch failed for %s: %s", page_url, exc)
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="readable",
            )

        if _is_cloudflare_challenge(html_text, status):
            reason = (
                f"Cloudflare or bot challenge (HTTP {status})"
                if status in CHALLENGE_STATUSES
                else f"Cloudflare or bot challenge detected (HTTP {status})"
            )
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="readable",
            )

        if status >= 400:
            reason = f"HTTP {status} fetching page"
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="readable",
            )

        if not (html_text or "").strip():
            reason = "Empty HTML response"
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="readable",
            )

        try:
            page = self.parse_html(html_text, source_url=final_url or page_url, platform=plat)
            # Require some useful content.
            if not (page.title or page.description_html or page.files):
                reason = "Could not extract main content"
                return ReadableSnapshotResult(
                    success=False,
                    html_path=index_path,
                    error=reason,
                    failure_reason=reason,
                    backend="readable",
                )
            index, css = write_readable_page(target, page)
            return ReadableSnapshotResult(
                success=True,
                html_path=index,
                css_path=css,
                error=None,
                backend="readable",
                used_fallback=False,
                asset_count=0,
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"Parse/render failed: {exc}"
            logger.warning("Readable snapshot render failed for %s: %s", page_url, exc)
            return ReadableSnapshotResult(
                success=False,
                html_path=index_path,
                error=reason,
                failure_reason=reason,
                backend="readable",
            )


def run_readable_offline_snapshot(
    *,
    source_url: str,
    output_dir: Path | str,
    platform: str = "",
    readable_provider: ReadableSnapshotProvider | None = None,
    browser_provider: Any | None = None,
    allow_browser_backup: bool = True,
    allow_legacy_browser_fallback: bool = False,
) -> tuple[ReadableSnapshotResult, str]:
    """
    Readable first → optional BrowserSnapshot backup → always leave a page.

    Returns ``(result, offline_status)``.

    Nexus guidance: ``allow_legacy_browser_fallback=False`` so a Cloudflare /
    browser failure does not fall into the old full-page asset scraper.
    On total failure a readable fallback page is written and status is failed.
    """
    page_url = str(source_url or "").strip()
    target = Path(output_dir)
    plat = normalize_platform(platform) or detect_platform(page_url)

    readable = readable_provider or ReadableSnapshotProvider()
    result = readable.snapshot(page_url, target, platform=plat)
    if result.success and result.html_path.is_file():
        return result, OFFLINE_STATUS_ARCHIVED

    readable_error = result.error or result.failure_reason or "Readable snapshot failed"
    browser_error: str | None = None

    if allow_browser_backup:
        try:
            from services.offline.browser_snapshot.manager import BrowserSnapshotProvider

            browser = browser_provider or BrowserSnapshotProvider(
                enable_legacy_fallback=bool(allow_legacy_browser_fallback)
            )
            browser_result = browser.snapshot(page_url, target)
            if (
                browser_result.success
                and browser_result.html_path.is_file()
                and not browser_result.used_fallback
            ):
                return (
                    ReadableSnapshotResult(
                        success=True,
                        html_path=browser_result.html_path,
                        css_path=None,
                        error=None,
                        backend="browser",
                        used_fallback=False,
                        asset_count=browser_result.asset_count,
                    ),
                    OFFLINE_STATUS_ARCHIVED,
                )
            # Legacy degrade (GitHub may allow): keep page but mark failed.
            if (
                browser_result.success
                and browser_result.html_path.is_file()
                and browser_result.used_fallback
            ):
                note = browser_result.error or (
                    f"Browser snapshot unavailable; used legacy after: {readable_error}"
                )
                return (
                    ReadableSnapshotResult(
                        success=True,
                        html_path=browser_result.html_path,
                        css_path=None,
                        error=note,
                        backend="browser",
                        used_fallback=True,
                        asset_count=browser_result.asset_count,
                        failure_reason=note,
                    ),
                    OFFLINE_STATUS_FAILED,
                )
            browser_error = browser_result.error or "Browser snapshot failed"
        except Exception as exc:  # noqa: BLE001
            browser_error = str(exc)
            logger.info(
                "Browser backup failed for %s: %s", page_url, browser_error
            )

    # Graceful degrade: always write a readable failure page (no WebSnapshot scrape).
    # Prefer the most specific (usually browser) error for status/UI.
    primary_error = browser_error or readable_error
    reason_parts = [readable_error]
    if browser_error and browser_error != readable_error:
        reason_parts.append(f"browser backup: {browser_error}")
    reason = "; ".join(reason_parts)
    index, css = write_failure_page(
        target,
        source_url=page_url,
        platform=plat,
        reason=reason,
    )
    return (
        ReadableSnapshotResult(
            success=True,  # page exists for UI
            html_path=index,
            css_path=css,
            error=primary_error,
            backend="fallback",
            used_fallback=True,
            asset_count=0,
            failure_reason=reason,
        ),
        OFFLINE_STATUS_FAILED,
    )
