"""Rewrite imported offline HTML asset references into ``./assets/``.

Handles Chrome ``Save As`` pages (``page.html`` + ``page_files/``) and
``file:///`` local URLs without requiring remote downloads.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_ATTR_MAP: dict[str, tuple[str, ...]] = {
    "a": ("href",),
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

_SKIP_SCHEMES = (
    "http://",
    "https://",
    "data:",
    "javascript:",
    "mailto:",
    "tel:",
    "blob:",
    "#",
)


def companion_files_dir(html_path: Path) -> Path | None:
    """
    Chrome/Edge ``Save As`` companion folder: ``Name.html`` → ``Name_files/``.
    """
    stem = html_path.stem
    candidates = (
        html_path.with_name(f"{stem}_files"),
        html_path.with_name(f"{stem}.files"),
        html_path.with_name(f"{stem}_Files"),
    )
    for path in candidates:
        if path.is_dir():
            return path
    return None


def _safe_name(name: str, *, fallback: str = "asset") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip()) or fallback
    if cleaned in {".", ".."}:
        cleaned = fallback
    if len(cleaned) > 120:
        cleaned = cleaned[:100] + Path(cleaned).suffix[:20]
    return cleaned


def _file_url_to_path(url: str) -> Path | None:
    raw = str(url or "").strip()
    if not raw.lower().startswith("file:"):
        return None
    parsed = urlparse(raw)
    path = unquote(parsed.path or "")
    # Windows file:///C:/... → /C:/... 
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    if not path:
        return None
    try:
        p = Path(path)
        return p if p.is_file() else None
    except OSError:
        return None


def _resolve_local_ref(
    value: str,
    *,
    html_dir: Path,
    companion: Path | None,
) -> Path | None:
    raw = str(value or "").strip()
    if not raw or raw.lower().startswith(_SKIP_SCHEMES):
        return None

    file_path = _file_url_to_path(raw)
    if file_path is not None:
        return file_path

    # Relative / absolute filesystem paths (not http)
    if "://" in raw and not raw.lower().startswith("file:"):
        return None

    candidate = Path(raw)
    if candidate.is_file():
        return candidate

    rel = Path(unquote(raw.split("?", 1)[0].split("#", 1)[0]))
    near_html = (html_dir / rel).resolve()
    if near_html.is_file():
        return near_html

    if companion is not None:
        # Common: ./Name_files/style.css or Name_files/style.css
        name = rel.name
        for base in (companion, companion / rel.parent if len(rel.parts) > 1 else companion):
            hit = (base / name).resolve() if name else None
            if hit is not None and hit.is_file():
                return hit
        nested = (companion / rel).resolve()
        if nested.is_file():
            return nested
    return None


def _asset_subdir_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".css"}:
        return "css"
    if suffix in {".js", ".mjs"}:
        return "js"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}:
        return "images"
    return "misc"


def import_companion_tree(companion: Path, assets_root: Path) -> int:
    """Copy an entire ``*_files`` tree under ``assets/`` preserving names."""
    count = 0
    if not companion.is_dir():
        return 0
    for src in companion.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(companion)
        dest = assets_root / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            count += 1
        except OSError as exc:
            logger.debug("skip companion file %s: %s", src, exc)
    return count


def rewrite_imported_html(
    html_text: str,
    *,
    html_path: Path,
    output_dir: Path,
    keep_remote: bool = True,
) -> tuple[str, int]:
    """
    Rewrite local / ``file://`` asset refs to ``./assets/...`` and copy files.

    Returns ``(html, copied_asset_count)``.
    """
    html_dir = html_path.parent
    companion = companion_files_dir(html_path)
    assets_root = Path(output_dir) / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    if companion is not None:
        copied += import_companion_tree(companion, assets_root)

    soup = BeautifulSoup(str(html_text or ""), "html.parser")
    seen: dict[str, str] = {}  # abs path -> relative href

    def localize(value: str) -> str:
        nonlocal copied
        raw = str(value or "").strip()
        if not raw:
            return value
        low = raw.lower()
        if low.startswith(("http://", "https://", "data:", "javascript:", "mailto:", "#")):
            return value

        src = _resolve_local_ref(raw, html_dir=html_dir, companion=companion)
        if src is None:
            # Companion-relative path already copied under assets/files/
            if companion is not None:
                cleaned = unquote(raw.split("?", 1)[0].split("#", 1)[0]).replace("\\", "/")
                stem = companion.name
                for prefix in (f"./{stem}/", f"{stem}/", f"../{stem}/"):
                    if cleaned.startswith(prefix) or cleaned.lower().startswith(prefix.lower()):
                        rel = cleaned[len(prefix) :]
                        return f"./assets/files/{rel}"
                # Bare filename that exists in companion
                only = Path(cleaned).name
                if only and (companion / only).is_file():
                    return f"./assets/files/{only}"
            return value if keep_remote else value

        key = str(src.resolve())
        if key in seen:
            return seen[key]

        sub = _asset_subdir_for(src)
        name = _safe_name(src.name)
        dest_dir = assets_root / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists():
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
            dest = dest_dir / f"{Path(name).stem}_{digest}{Path(name).suffix}"
            name = dest.name
        try:
            shutil.copy2(src, dest)
            copied += 1
        except OSError as exc:
            logger.debug("asset copy failed %s: %s", src, exc)
            return value

        rel_href = f"./assets/{sub}/{name}"
        seen[key] = rel_href
        return rel_href

    for tag_name, attrs in _ATTR_MAP.items():
        for node in soup.find_all(tag_name):
            if not isinstance(node, Tag):
                continue
            for attr in attrs:
                if not node.has_attr(attr):
                    continue
                current = node.get(attr)
                if current is None or isinstance(current, list):
                    continue
                node[attr] = localize(str(current))

            if node.has_attr("srcset"):
                parts: list[str] = []
                for chunk in str(node.get("srcset") or "").split(","):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    bits = chunk.split()
                    if bits:
                        bits[0] = localize(bits[0])
                    parts.append(" ".join(bits))
                if parts:
                    node["srcset"] = ", ".join(parts)

    # If companion folder was imported, rewrite remaining *_files/ relative links.
    if companion is not None:
        stem = companion.name
        for node in soup.find_all(True):
            if not isinstance(node, Tag):
                continue
            for attr in ("href", "src"):
                if not node.has_attr(attr):
                    continue
                val = str(node.get(attr) or "")
                cleaned = val.replace("\\", "/")
                for prefix in (f"./{stem}/", f"{stem}/"):
                    if cleaned.startswith(prefix):
                        node[attr] = f"./assets/files/{cleaned[len(prefix):]}"
                        break

    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out, copied
