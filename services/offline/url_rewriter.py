"""Offline HTML URL normalization for file:// viewing.

Rewrites root-relative and page-relative navigation URLs to absolute
``https://`` links so clicking Releases / Issues / etc. opens the real site
instead of ``file:///...``.

Does not download assets and does not convert remote URLs to ``file://``.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

# Attributes rewritten per element.
_ATTR_MAP: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "area": ("href",),
    "form": ("action",),
    "link": ("href",),
    "script": ("src",),
    "img": ("src",),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "iframe": ("src",),
    "embed": ("src",),
    "use": ("href",),
}

_SCHEME_KEEP_PREFIXES = (
    "http://",
    "https://",
    "javascript:",
    "mailto:",
    "tel:",
    "data:",
    "blob:",
    "#",
)


def _normalize_base_url(base_url: str) -> str:
    """Ensure *base_url* is absolute http(s) and ends with ``/`` for urljoin."""
    base = str(base_url or "").strip()
    if not base:
        return ""
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return base if base.endswith("/") else base + "/"
    # Drop fragment; keep path (repo page) with trailing slash for relative joins.
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = path + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_local_asset_ref(value: str) -> bool:
    """True for offline-local asset paths that must stay relative."""
    v = value.strip()
    low = v.lower()
    if low.startswith("./assets/") or low.startswith("assets/"):
        return True
    if low.startswith("../assets/"):
        return True
    if "/assets/" in low and not low.startswith(("http://", "https://", "/")):
        return True
    return False


def rewrite_url(value: str, base_url: str) -> str:
    """
    Rewrite a single URL attribute value against *base_url*.

    Absolute http(s), javascript:, mailto:, data:, and local ``assets/`` refs
    are left unchanged. Root-relative and relative paths become absolute URLs.
    """
    raw = str(value or "").strip()
    if not raw:
        return value

    low = raw.lower()
    for prefix in _SCHEME_KEEP_PREFIXES:
        if low.startswith(prefix):
            return value

    if _is_local_asset_ref(raw):
        return value

    base = _normalize_base_url(base_url)
    if not base:
        return value

    # Protocol-relative: //cdn.example.com/...
    if raw.startswith("//"):
        scheme = urlparse(base).scheme or "https"
        return f"{scheme}:{raw}"

    return urljoin(base, raw)


def rewrite_external_urls(
    html: str,
    base_url: str,
    *,
    tags: Iterable[str] | None = None,
) -> str:
    """
    Rewrite navigation / resource URLs in *html* to absolute http(s) URLs.

    Parameters
    ----------
    html:
        Full HTML document (e.g. Playwright ``page.content()``).
    base_url:
        Page URL used as join base, e.g. ``https://github.com/owner/repo``.
    """
    text = str(html or "")
    if not text.strip() or not str(base_url or "").strip():
        return text

    soup = BeautifulSoup(text, "html.parser")
    tag_filter = {t.lower() for t in tags} if tags is not None else None

    for tag_name, attrs in _ATTR_MAP.items():
        if tag_filter is not None and tag_name not in tag_filter:
            continue
        for node in soup.find_all(tag_name):
            if not isinstance(node, Tag):
                continue
            for attr in attrs:
                if not node.has_attr(attr):
                    continue
                current = node.get(attr)
                if current is None:
                    continue
                # Ignore multi-value attrs (srcset handled lightly below).
                if isinstance(current, list):
                    continue
                rewritten = rewrite_url(str(current), base_url)
                if rewritten != current:
                    node[attr] = rewritten

            # srcset: rewrite each URL token
            if node.has_attr("srcset"):
                srcset = str(node.get("srcset") or "")
                parts: list[str] = []
                for chunk in srcset.split(","):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    bits = chunk.split()
                    if not bits:
                        continue
                    bits[0] = rewrite_url(bits[0], base_url)
                    parts.append(" ".join(bits))
                if parts:
                    node["srcset"] = ", ".join(parts)

    out = str(soup)
    if text.lstrip().lower().startswith("<!doctype") and not out.lstrip().lower().startswith(
        "<!doctype"
    ):
        out = "<!DOCTYPE html>\n" + out
    return out
