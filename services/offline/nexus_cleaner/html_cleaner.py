"""Remove ads / login shells / empty dynamic chrome from Nexus offline HTML."""

from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

# Tokens that mark removable chrome (matched against id/class/role).
_AD_TOKENS = (
    "ad",
    "ads",
    "advert",
    "advertisement",
    "banner",
    "sponsor",
    "sponsored",
    "promo",
    "dfp",
    "gpt-",
    "adsense",
)
_LOGIN_TOKENS = (
    "login",
    "sign-in",
    "signin",
    "sign_in",
    "register",
    "authentication",
    "auth-modal",
    "auth_modal",
    "account-popup",
)
# Keep these even if empty-looking — core Mod reading areas.
_PRESERVE_TOKENS = (
    "mod-page",
    "mod_page",
    "modpage",
    "mod-header",
    "mod_header",
    "mod-title",
    "mod_title",
    "mod-description",
    "mod_description",
    "description",
    "requirements",
    "requirement",
    "files",
    "file-list",
    "file_list",
    "changelog",
    "change-log",
    "installation",
    "install",
    "author",
    "uploader",
    "article",
    "main",
    "content",
    "summary",
)


def _is_live(node: Tag) -> bool:
    """True if *node* is still attached (not decomposed / orphaned)."""
    return isinstance(node, Tag) and getattr(node, "attrs", None) is not None


def _tokens_of(node: Tag) -> str:
    if not _is_live(node):
        return ""
    parts: list[str] = []
    for attr in ("id", "class", "role", "data-testid", "aria-label"):
        val = node.get(attr)
        if val is None:
            continue
        if isinstance(val, list):
            parts.extend(str(x) for x in val)
        else:
            parts.append(str(val))
    return " ".join(parts).casefold()


def _matches_any(haystack: str, needles: Iterable[str]) -> bool:
    for needle in needles:
        n = needle.casefold()
        if n in haystack:
            # Avoid matching "download" as "ad" etc. — require word-ish boundary for short tokens.
            if len(n) <= 3:
                if re.search(rf"(^|[^a-z0-9]){re.escape(n)}([^a-z0-9]|$)", haystack):
                    return True
            else:
                return True
    return False


def _is_preserved(node: Tag) -> bool:
    return _matches_any(_tokens_of(node), _PRESERVE_TOKENS)


def _visible_text(node: Tag) -> str:
    return " ".join(node.stripped_strings)


def _has_media(node: Tag) -> bool:
    return bool(node.find(["img", "picture", "video", "svg", "canvas", "source"]))


def _has_meaningful_child(node: Tag) -> bool:
    for child in node.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                return True
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in {"br", "hr", "wbr"}:
            continue
        if name in {"script", "style", "noscript", "template", "svg"}:
            # svg counts as media elsewhere; empty svg-less shells still skip here
            if name == "svg":
                return True
            continue
        if _visible_text(child) or _has_media(child) or child.find(True):
            return True
    return False


def _is_empty_shell(node: Tag) -> bool:
    if _is_preserved(node):
        return False
    if _visible_text(node):
        return False
    if _has_media(node):
        return False
    return not _has_meaningful_child(node)


def clean_html(html_text: str) -> str:
    """
    Strip ads, login chrome, iframes, scripts, and empty dynamic shells.

    Preserves Mod title / description / files / requirements regions.
    """
    soup = BeautifulSoup(str(html_text or ""), "html.parser")

    # 1) Drop scripts (layout uses CSS / inline style only offline).
    for node in list(soup.find_all("script")):
        node.decompose()

    # 2) Drop iframes — cannot load offline.
    for node in list(soup.find_all("iframe")):
        node.decompose()

    # 3) Drop noscript / template chrome often wrapping empty widgets.
    for node in list(soup.find_all(["noscript", "template"])):
        if _is_empty_shell(node) or not _visible_text(node):
            node.decompose()

    # 4) Ads / login containers by class/id tokens.
    for node in list(soup.find_all(True)):
        if not _is_live(node) or node.name in {"html", "head", "body"}:
            continue
        if _is_preserved(node):
            continue
        tokens = _tokens_of(node)
        if _matches_any(tokens, _AD_TOKENS) or _matches_any(tokens, _LOGIN_TOKENS):
            node.decompose()

    # 5) Empty gallery / widget shells (no text, no images).
    # Walk deepest-first so nested empties collapse outward.
    changed = True
    rounds = 0
    while changed and rounds < 8:
        changed = False
        rounds += 1
        for node in list(reversed(soup.find_all(True))):
            if not _is_live(node):
                continue
            if node.name in {"html", "head", "body", "main", "article"}:
                continue
            if node.name in {"img", "picture", "video", "source", "svg", "path", "br", "hr"}:
                continue
            if _is_empty_shell(node):
                # Prefer removing known empty widget names; also generic empty div/section.
                tokens = _tokens_of(node)
                if (
                    _matches_any(
                        tokens,
                        (
                            "gallery",
                            "carousel",
                            "slider",
                            "widget",
                            "modal",
                            "popup",
                            "overlay",
                            "placeholder",
                            "skeleton",
                            "spinner",
                            "loading",
                            "react-",
                            "vue-",
                        ),
                    )
                    or node.name in {"div", "section", "aside", "span", "ul", "ol", "li"}
                ):
                    node.decompose()
                    changed = True

    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out
