"""Collapse tall empty placeholder regions that break offline layout."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from services.offline.nexus_cleaner.html_cleaner import (
    _has_media,
    _is_preserved,
    _visible_text,
)

_HEIGHT_RE = re.compile(
    r"(?:^|;)\s*(?:min-)?height\s*:\s*(\d+(?:\.\d+)?)\s*px",
    re.IGNORECASE,
)
_MIN_EMPTY_HEIGHT_PX = 200


def _style_height_px(style: str) -> float | None:
    best: float | None = None
    for match in _HEIGHT_RE.finditer(str(style or "")):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if best is None or value > best:
            best = value
    return best


def _attr_height_px(node: Tag) -> float | None:
    raw = node.get("height")
    if raw is None:
        return None
    text = str(raw).strip().lower().removesuffix("px")
    try:
        return float(text)
    except ValueError:
        return None


def _is_tall_empty(node: Tag) -> bool:
    if _is_preserved(node):
        return False
    if node.name in {"html", "head", "body", "main", "article", "img", "svg"}:
        return False
    height = _style_height_px(str(node.get("style") or ""))
    if height is None:
        height = _attr_height_px(node)
    if height is None or height < _MIN_EMPTY_HEIGHT_PX:
        return False
    if _visible_text(node):
        return False
    if _has_media(node):
        return False
    # Nested text/media already covered; allow trivial spacer children.
    return True


def _hide_node(node: Tag) -> None:
    style = str(node.get("style") or "").strip().rstrip(";")
    hide = "display:none !important;max-height:0 !important;overflow:hidden !important;"
    node["style"] = f"{style};{hide}" if style else hide
    node["data-smm-offline-hidden"] = "1"


def optimize_layout(html_text: str) -> str:
    """
    Hide tall empty placeholders (``height > 200px`` with no content).

    Does not convert the page into a text summary — only suppresses dead space.
    """
    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    for node in list(soup.find_all(True)):
        if not isinstance(node, Tag) or getattr(node, "attrs", None) is None:
            continue
        if _is_tall_empty(node):
            _hide_node(node)

    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out
