"""Parse browser ``.mhtml`` / ``.mht`` saves into offline ``index.html`` + assets."""

from __future__ import annotations

import logging
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

MHTML_SUFFIXES = {".mhtml", ".mht"}

_CID_ATTR_RE = re.compile(
    r"""(?P<attr>\b(?:src|href)\s*=\s*)(?P<q>["'])cid:(?P<cid>[^"']+)(?P=q)""",
    re.IGNORECASE,
)
_CID_URL_RE = re.compile(
    r"""url\(\s*(?P<q>["']?)cid:(?P<cid>[^)"']+)(?P=q)\s*\)""",
    re.IGNORECASE,
)
_HTTP_ATTR_RE = re.compile(
    r"""(?P<attr>\b(?:src|href)\s*=\s*)(?P<q>["'])(?P<url>(?:https?:)?//[^"']+)(?P=q)""",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(
    r"""url\(\s*(?P<q>["']?)(?P<url>(?:https?:)?//[^)"']+)(?P=q)\s*\)""",
    re.IGNORECASE,
)


def is_mhtml_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MHTML_SUFFIXES


def _clean_cid(value: str) -> str:
    text = unquote(str(value or "").strip())
    if text.lower().startswith("cid:"):
        text = text[4:]
    return text.strip().strip("<>").strip()


def _safe_asset_name(name: str, *, index: int, content_type: str) -> str:
    raw = re.sub(r"[^\w.\-]+", "_", (name or "").strip()) or f"part_{index}"
    if raw in {".", ".."}:
        raw = f"part_{index}"
    if "." not in raw:
        subtype = (content_type or "").split("/", 1)[-1].split(";", 1)[0].strip().lower()
        ext_map = {
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "png": ".png",
            "gif": ".gif",
            "webp": ".webp",
            "svg+xml": ".svg",
            "css": ".css",
            "javascript": ".js",
            "x-javascript": ".js",
            "html": ".html",
            "plain": ".txt",
        }
        raw = raw + ext_map.get(subtype, ".bin")
    if len(raw) > 120:
        raw = raw[:100] + Path(raw).suffix[:20]
    return raw


def _part_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        charset = part.get_content_charset() or "utf-8"
        return payload.encode(charset, errors="replace")
    raw = part.get_payload()
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    return b""


def _decode_html_bytes(data: bytes, part: Message) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def rewrite_cid_references(html_text: str, cid_to_href: dict[str, str]) -> str:
    """Rewrite ``cid:…`` attribute / CSS url() refs to local asset paths."""
    if not cid_to_href:
        return html_text

    def _lookup(cid: str) -> str | None:
        key = _clean_cid(cid)
        if key in cid_to_href:
            return cid_to_href[key]
        # Case-insensitive fallback
        low = key.casefold()
        for existing, href in cid_to_href.items():
            if existing.casefold() == low:
                return href
        return None

    def _attr(match: re.Match[str]) -> str:
        href = _lookup(match.group("cid"))
        if not href:
            return match.group(0)
        q = match.group("q")
        return f'{match.group("attr")}{q}{href}{q}'

    def _url(match: re.Match[str]) -> str:
        href = _lookup(match.group("cid"))
        if not href:
            return match.group(0)
        return f"url({href})"

    out = _CID_ATTR_RE.sub(_attr, html_text)
    out = _CID_URL_RE.sub(_url, out)
    return out


def _normalize_location(value: str) -> str:
    text = unquote(str(value or "").strip())
    if text.startswith("//"):
        text = "https:" + text
    return text.split("#", 1)[0]


def rewrite_embedded_urls(html_text: str, url_to_href: dict[str, str]) -> str:
    """
    Rewrite absolute ``http(s)://`` / ``//`` refs that map to embedded MHTML parts.

    Chrome/Edge MHTML saves typically use Content-Location URLs as ``src`` / ``url()``,
    not ``cid:``. Only known embedded URLs are rewritten; normal page links stay.
    """
    if not url_to_href:
        return html_text

    lookup: dict[str, str] = {}
    for key, href in url_to_href.items():
        norm = _normalize_location(key)
        if norm.startswith(("http://", "https://")):
            lookup.setdefault(norm, href)
            lookup.setdefault(norm.split("?", 1)[0], href)

    if not lookup:
        return html_text

    def _resolve(url: str) -> str | None:
        norm = _normalize_location(url)
        if norm in lookup:
            return lookup[norm]
        bare = norm.split("?", 1)[0]
        return lookup.get(bare)

    def _attr(match: re.Match[str]) -> str:
        href = _resolve(match.group("url"))
        if not href:
            return match.group(0)
        q = match.group("q")
        return f'{match.group("attr")}{q}{href}{q}'

    def _url(match: re.Match[str]) -> str:
        href = _resolve(match.group("url"))
        if not href:
            return match.group(0)
        return f"url({href})"

    out = _HTTP_ATTR_RE.sub(_attr, html_text)
    out = _HTTP_URL_RE.sub(_url, out)
    return out


def extract_mhtml(mhtml_path: Path | str) -> tuple[str, dict[str, bytes], dict[str, str]]:
    """
    Parse an MHTML file.

    Returns ``(html_text, assets_by_name, cid_to_asset_name)``.

    ``cid_to_asset_name`` also includes Content-Location URLs / basenames so Chrome
    MHTML absolute ``src`` values can be rewritten offline.
    """
    path = Path(mhtml_path)
    data = path.read_bytes()
    if not data.strip():
        raise ValueError("MHTML 文件为空")

    msg = BytesParser(policy=policy.default).parsebytes(data)
    html_text = ""
    assets: dict[str, bytes] = {}
    cid_map: dict[str, str] = {}
    used_names: set[str] = set()
    part_index = 0

    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "application/octet-stream").lower()
        payload = _part_payload(part)
        if not payload:
            continue
        part_index += 1

        if content_type in {"text/html", "application/xhtml+xml"} and not html_text:
            html_text = _decode_html_bytes(payload, part)
            continue

        location = str(part.get("Content-Location") or "").strip()
        loc_basename = ""
        if location:
            loc_basename = Path(unquote(location.split("?")[0])).name

        filename = (
            part.get_filename()
            or part.get_param("name", header="Content-Type")
            or part.get_param("name")
            or loc_basename
            or ""
        )
        name = _safe_asset_name(str(filename or ""), index=part_index, content_type=content_type)
        base = Path(name).stem
        suffix = Path(name).suffix
        candidate = name
        n = 1
        while candidate in used_names:
            candidate = f"{base}_{n}{suffix}"
            n += 1
        used_names.add(candidate)
        assets[candidate] = payload

        cid = _clean_cid(part.get("Content-ID", "") or "")
        if cid:
            cid_map[cid] = candidate
        # Map Content-Location (full URL + basename) for Chrome-style MHTML.
        if location:
            norm = _normalize_location(location)
            cid_map.setdefault(location, candidate)
            cid_map.setdefault(norm, candidate)
            cid_map.setdefault(norm.split("?", 1)[0], candidate)
            if loc_basename:
                cid_map.setdefault(loc_basename, candidate)
                cid_map.setdefault(_clean_cid(loc_basename), candidate)

    if not html_text.strip():
        raise ValueError("MHTML 中未找到 HTML 正文")
    return html_text, assets, cid_map


def import_mhtml_snapshot(
    mhtml_path: Path | str,
    output_dir: Path | str,
) -> tuple[str, int, dict[str, str]]:
    """
    Extract MHTML into ``output_dir/index.html`` (+ ``assets/``).

    Returns ``(html_text, asset_count, cid_to_href)`` before writing index
    (caller writes rewritten HTML). Prefer :func:`store_mhtml_snapshot`.
    """
    html_text, assets, cid_map = extract_mhtml(mhtml_path)
    target = Path(output_dir)
    assets_root = target / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)

    cid_to_href: dict[str, str] = {}
    for cid, name in cid_map.items():
        cid_to_href[cid] = f"./assets/{name}"

    written = 0
    for name, blob in assets.items():
        dest = assets_root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        written += 1

    rewritten = rewrite_cid_references(html_text, cid_to_href)
    rewritten = rewrite_embedded_urls(rewritten, cid_to_href)
    return rewritten, written, cid_to_href


def store_mhtml_snapshot(
    mhtml_path: Path | str,
    output_dir: Path | str,
) -> tuple[Path, int]:
    """Write MHTML into ``output_dir/index.html`` and return ``(index, asset_count)``."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "assets").mkdir(parents=True, exist_ok=True)

    rewritten, asset_count, _cid = import_mhtml_snapshot(mhtml_path, target)
    index = target / "index.html"
    tmp = target / ".index.html.tmp"
    if not rewritten.lstrip().lower().startswith("<!doctype"):
        rewritten = "<!DOCTYPE html>\n" + rewritten
    tmp.write_text(rewritten, encoding="utf-8")
    tmp.replace(index)
    return index, asset_count
