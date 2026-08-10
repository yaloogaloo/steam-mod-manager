"""Download page assets and rewrite HTML/CSS/JS references to local paths."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup, Tag

from services.offline.browser_snapshot.playwright_capture import PageCapture

logger = logging.getLogger(__name__)

DEFAULT_INDEX_NAME = "index.html"
DEFAULT_ASSETS_DIR = "assets"
DEFAULT_MANIFEST_NAME = "snapshot_manifest.json"
MAX_ASSETS = 400
MAX_ASSET_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 30

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_CSS_URL_RE = re.compile(
    r"""url\(\s*(['"]?)([^)'"]+)\1\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*(['"]?)([^)'"]+)\1\s*\)|(['"])([^'"]+)\3)\s*;""",
    re.IGNORECASE,
)
_JS_FROM_RE = re.compile(
    r"""((?:import|export)\s+(?:[\s\S]*?\s+from\s+|))(['"])([^'"]+)\2""",
    re.MULTILINE,
)
_JS_IMPORT_CALL_RE = re.compile(
    r"""import\s*\(\s*(['"])([^'"]+)\1\s*\)""",
    re.MULTILINE,
)


@dataclass
class ManifestEntry:
    url: str
    local: str
    type: str  # css / js / image / font / other
    status: str  # success / failed / skipped


@dataclass
class RewriteResult:
    success: bool
    html_path: Path
    manifest_path: Path
    asset_count: int
    entries: list[ManifestEntry] = field(default_factory=list)
    error: str | None = None


def _guess_extension(url_path: str, content_type: str = "") -> str:
    path = unquote(url_path or "")
    suffix = Path(path.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 10 and suffix not in {".php", ".asp", ".aspx"}:
        return suffix
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(mime)
        if guessed == ".jpe":
            return ".jpg"
        if guessed:
            return guessed
    return ".bin"


def _asset_type(ext: str, content_type: str = "") -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    ext = (ext or "").lower()
    if ext == ".css" or mime == "text/css":
        return "css"
    if ext in {".js", ".mjs", ".cjs"} or "javascript" in mime:
        return "js"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".avif"} or mime.startswith(
        "image/"
    ):
        return "image"
    if ext in {".woff", ".woff2", ".ttf", ".otf", ".eot"} or "font" in mime:
        return "font"
    return "other"


def _safe_filename(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    ext = _guess_extension(parsed.path, content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    # Prefer a readable stem from the URL when short.
    stem = Path(unquote(parsed.path)).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:40].strip("._")
    if stem and stem.lower() not in {"index", "main", "app", "bundle"}:
        return f"{stem}_{digest[:8]}{ext}"
    return f"{digest}{ext}"


def _parse_srcset(value: str) -> list[str]:
    urls: list[str] = []
    for part in str(value or "").split(","):
        token = part.strip().split()
        if token:
            urls.append(token[0])
    return urls


class ResourceRewriter:
    """
    Localize HTML / CSS / JS asset references under ``output_dir/assets/``.

    Writes ``index.html`` + ``snapshot_manifest.json``.
    Paths are rewritten as ``./assets/<file>`` so ``file://`` open works.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_assets: int = MAX_ASSETS,
        headers: dict[str, str] | None = None,
        css_only: bool = False,
    ) -> None:
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.max_assets = max(1, int(max_assets))
        self.headers = dict(headers or {"User-Agent": _USER_AGENT})
        self.css_only = bool(css_only)

    def close(self) -> None:
        if self._owns_session:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> ResourceRewriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def rewrite(
        self,
        capture: PageCapture | str,
        output_dir: Path | str,
        *,
        page_url: str = "",
        extra_urls: Iterable[str] | None = None,
    ) -> RewriteResult:
        target = Path(output_dir)
        index_path = target / DEFAULT_INDEX_NAME
        manifest_path = target / DEFAULT_MANIFEST_NAME
        assets_dir = target / DEFAULT_ASSETS_DIR

        if isinstance(capture, PageCapture):
            html_text = capture.html
            base_url = capture.page_url or page_url
            seed_urls = list(capture.discovered_urls)
            sheet_texts = {
                (s.href or "").strip(): s.text
                for s in capture.stylesheets
                if (s.href or "").strip() and (s.text or "").strip()
            }
        else:
            html_text = str(capture or "")
            base_url = page_url
            seed_urls = []
            sheet_texts = {}

        if extra_urls:
            seed_urls.extend(str(u) for u in extra_urls if str(u or "").strip())

        if not html_text.strip():
            return RewriteResult(
                success=False,
                html_path=index_path,
                manifest_path=manifest_path,
                asset_count=0,
                error="Empty HTML",
            )
        if not base_url:
            return RewriteResult(
                success=False,
                html_path=index_path,
                manifest_path=manifest_path,
                asset_count=0,
                error="Empty page URL",
            )

        try:
            target.mkdir(parents=True, exist_ok=True)
            if assets_dir.exists():
                shutil.rmtree(assets_dir, ignore_errors=True)
            assets_dir.mkdir(parents=True, exist_ok=True)

            seen: dict[str, str] = {}  # absolute url -> ./assets/name
            entries: list[ManifestEntry] = []

            # Prefer in-browser stylesheet bodies when available (CORS-safe).
            for href, text in sheet_texts.items():
                absolute = self._absolutize(href, base_url)
                if not absolute or absolute in seen:
                    continue
                local = self._store_text_asset(
                    absolute,
                    text,
                    assets_dir,
                    seen,
                    entries,
                    content_type="text/css",
                    rewrite_nested=True,
                    page_url=base_url,
                )
                if local and len(seen) >= self.max_assets:
                    break

            soup = BeautifulSoup(html_text, "html.parser")
            for base in list(soup.find_all("base")):
                if isinstance(base, Tag):
                    base.decompose()

            # Layout mode: strip scripts entirely before rewrite.
            if self.css_only:
                for node in list(soup.find_all("script")):
                    node.decompose()

            self._rewrite_html_tree(
                soup,
                base_url,
                assets_dir,
                seen,
                entries,
            )

            # Also fetch any remaining discovered URLs not yet localized.
            for raw in seed_urls:
                if len(seen) >= self.max_assets:
                    break
                if self.css_only:
                    low = str(raw).lower()
                    if ".css" not in low and "stylesheet" not in low:
                        continue
                absolute = self._absolutize(raw, base_url)
                if not absolute or absolute in seen:
                    continue
                self._download_asset(
                    absolute,
                    base_url,
                    assets_dir,
                    seen,
                    entries,
                    rewrite_nested=True,
                )

            tmp = target / f".{DEFAULT_INDEX_NAME}.tmp"
            tmp.write_text(str(soup), encoding="utf-8")
            tmp.replace(index_path)

            self._write_manifest(manifest_path, entries)
            ok_count = sum(1 for e in entries if e.status == "success")
            return RewriteResult(
                success=True,
                html_path=index_path,
                manifest_path=manifest_path,
                asset_count=ok_count,
                entries=entries,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Resource rewrite failed: %s", exc)
            try:
                self._write_manifest(manifest_path, entries if "entries" in locals() else [])
            except Exception:  # noqa: BLE001
                pass
            return RewriteResult(
                success=False,
                html_path=index_path,
                manifest_path=manifest_path,
                asset_count=0,
                entries=entries if "entries" in locals() else [],
                error=str(exc),
            )

    def _rewrite_html_tree(
        self,
        soup: BeautifulSoup,
        page_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        entries: list[ManifestEntry],
    ) -> None:
        # <link href> — stylesheets; non-CSS links dropped in css_only mode
        for link in list(soup.find_all("link")):
            if not isinstance(link, Tag):
                continue
            href = link.get("href")
            if not href:
                continue
            rel = " ".join(link.get("rel") or []).lower()
            if self.css_only and "stylesheet" not in rel:
                link.decompose()
                continue
            local = self._download_asset(
                str(href), page_url, assets_dir, seen, entries, rewrite_nested=True
            )
            if local:
                link["href"] = local

        # <script> — already stripped when css_only; otherwise localize
        if not self.css_only:
            for script in list(soup.find_all("script")):
                if not isinstance(script, Tag):
                    continue
                src = script.get("src")
                if not src:
                    raw_js = script.string
                    if raw_js and ("import" in str(raw_js) or "from" in str(raw_js)):
                        rewritten = self._rewrite_js_text(
                            str(raw_js),
                            page_url,
                            assets_dir,
                            seen,
                            entries,
                        )
                        script.clear()
                        script.append(rewritten)
                    continue
                local = self._download_asset(
                    str(src), page_url, assets_dir, seen, entries, rewrite_nested=True
                )
                if local:
                    script["src"] = local

        # Media tags
        for tag_name in ("img", "source", "video", "audio", "embed", "iframe"):
            for node in list(soup.find_all(tag_name)):
                if not isinstance(node, Tag):
                    continue
                if self.css_only:
                    if tag_name == "img":
                        ph = soup.new_tag("span")
                        ph["class"] = "smm-img-placeholder"
                        ph.string = f"[Image: {node.get('alt') or 'media'}]"
                        node.replace_with(ph)
                    continue
                for attr in ("src", "data-src", "data-lazy-src", "poster"):
                    val = node.get(attr)
                    if not val:
                        continue
                    local = self._download_asset(
                        str(val), page_url, assets_dir, seen, entries
                    )
                    if local:
                        node[attr] = local
                for attr in ("srcset", "data-srcset"):
                    val = node.get(attr)
                    if not val:
                        continue
                    rewritten_parts: list[str] = []
                    for part in str(val).split(","):
                        tokens = part.strip().split()
                        if not tokens:
                            continue
                        local = self._download_asset(
                            tokens[0], page_url, assets_dir, seen, entries
                        )
                        if local:
                            tokens[0] = local
                        rewritten_parts.append(" ".join(tokens))
                    if rewritten_parts:
                        node[attr] = ", ".join(rewritten_parts)

        # Inline style url(...)
        for node in soup.find_all(style=True):
            if not isinstance(node, Tag):
                continue
            style = str(node.get("style") or "")
            if "url(" not in style.lower():
                continue
            node["style"] = self._rewrite_css_text(
                style,
                page_url,
                assets_dir,
                seen,
                entries,
                relative_to_assets=False,
            )

        # <style> blocks
        for style_tag in list(soup.find_all("style")):
            if not isinstance(style_tag, Tag):
                continue
            raw_css = style_tag.string
            if not raw_css:
                continue
            rewritten = self._rewrite_css_text(
                str(raw_css),
                page_url,
                assets_dir,
                seen,
                entries,
                relative_to_assets=False,
            )
            style_tag.clear()
            style_tag.append(rewritten)

    def _download_asset(
        self,
        raw_url: str,
        page_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        entries: list[ManifestEntry],
        *,
        rewrite_nested: bool = False,
    ) -> str | None:
        absolute = self._absolutize(raw_url, page_url)
        if absolute is None:
            return None
        if absolute in seen:
            return seen[absolute]
        if self.css_only:
            low = absolute.lower()
            # Allow CSS and @import targets that look like stylesheets only.
            if ".js" in low or ".mjs" in low:
                return None
        if len(seen) >= self.max_assets:
            entries.append(
                ManifestEntry(
                    url=absolute, local="", type="other", status="skipped"
                )
            )
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
            filename = _safe_filename(absolute, content_type)
            atype = _asset_type(Path(filename).suffix, content_type)

            dest = assets_dir / filename
            if dest.exists():
                stem, suf = dest.stem, dest.suffix
                n = 1
                while dest.exists():
                    dest = assets_dir / f"{stem}_{n}{suf}"
                    n += 1
                filename = dest.name

            size = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    entries.append(
                        ManifestEntry(
                            url=absolute, local="", type=atype, status="failed"
                        )
                    )
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)

            if atype == "css" or (
                rewrite_nested and ("text/css" in content_type.lower() or filename.endswith(".css"))
            ):
                text = data.decode(resp.encoding or "utf-8", errors="replace")
                text = self._rewrite_css_text(
                    text,
                    absolute,
                    assets_dir,
                    seen,
                    entries,
                    relative_to_assets=True,
                )
                dest.write_text(text, encoding="utf-8")
                atype = "css"
            elif atype == "js" or (
                rewrite_nested
                and (
                    "javascript" in content_type.lower()
                    or filename.endswith((".js", ".mjs"))
                )
            ):
                text = data.decode(resp.encoding or "utf-8", errors="replace")
                text = self._rewrite_js_text(
                    text, absolute, assets_dir, seen, entries
                )
                dest.write_text(text, encoding="utf-8")
                atype = "js"
            else:
                dest.write_bytes(data)

            rel = f"./assets/{filename}"
            seen[absolute] = rel
            entries.append(
                ManifestEntry(url=absolute, local=rel, type=atype, status="success")
            )
            return rel
        except Exception as exc:  # noqa: BLE001
            logger.debug("Asset download failed %s: %s", absolute, exc)
            entries.append(
                ManifestEntry(
                    url=absolute,
                    local="",
                    type=_asset_type(_guess_extension(urlparse(absolute).path), ""),
                    status="failed",
                )
            )
            return None

    def _store_text_asset(
        self,
        absolute: str,
        text: str,
        assets_dir: Path,
        seen: dict[str, str],
        entries: list[ManifestEntry],
        *,
        content_type: str,
        rewrite_nested: bool,
        page_url: str,
    ) -> str | None:
        if absolute in seen:
            return seen[absolute]
        filename = _safe_filename(absolute, content_type)
        atype = _asset_type(Path(filename).suffix, content_type)
        dest = assets_dir / filename
        if dest.exists():
            stem, suf = dest.stem, dest.suffix
            n = 1
            while dest.exists():
                dest = assets_dir / f"{stem}_{n}{suf}"
                n += 1
            filename = dest.name
        body = text
        if rewrite_nested and atype == "css":
            body = self._rewrite_css_text(
                text,
                absolute or page_url,
                assets_dir,
                seen,
                entries,
                relative_to_assets=True,
            )
        dest.write_text(body, encoding="utf-8")
        rel = f"./assets/{filename}"
        seen[absolute] = rel
        entries.append(
            ManifestEntry(url=absolute, local=rel, type=atype, status="success")
        )
        return rel

    def _rewrite_css_text(
        self,
        css_text: str,
        base_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        entries: list[ManifestEntry],
        *,
        relative_to_assets: bool,
    ) -> str:
        def _local_href(local: str) -> str:
            if relative_to_assets and local.startswith("./assets/"):
                return Path(local).name
            return local

        def _url_repl(match: re.Match[str]) -> str:
            quote = match.group(1) or ""
            raw = (match.group(2) or "").strip()
            if not raw or raw.startswith(("data:", "#")):
                return match.group(0)
            local = self._download_asset(
                raw, base_url, assets_dir, seen, entries, rewrite_nested=True
            )
            if not local:
                return match.group(0)
            href = _local_href(local)
            return f"url({quote}{href}{quote})"

        def _import_repl(match: re.Match[str]) -> str:
            raw = (match.group(2) or match.group(4) or "").strip()
            if not raw:
                return match.group(0)
            local = self._download_asset(
                raw, base_url, assets_dir, seen, entries, rewrite_nested=True
            )
            if not local:
                return match.group(0)
            href = _local_href(local)
            return f'@import url("{href}");'

        out = _CSS_URL_RE.sub(_url_repl, css_text)
        out = _CSS_IMPORT_RE.sub(_import_repl, out)
        return out

    def _rewrite_js_text(
        self,
        js_text: str,
        base_url: str,
        assets_dir: Path,
        seen: dict[str, str],
        entries: list[ManifestEntry],
    ) -> str:
        def _replace_spec(raw: str) -> str | None:
            text = (raw or "").strip()
            if not text or text.startswith(("data:", "node:", "fs:", "http://localhost")):
                return None
            # Relative / absolute HTTP(S) module URLs only.
            absolute = self._absolutize(text, base_url)
            if absolute is None:
                return None
            local = self._download_asset(
                absolute, base_url, assets_dir, seen, entries, rewrite_nested=True
            )
            return local

        def _from_repl(match: re.Match[str]) -> str:
            prefix, quote, spec = match.group(1), match.group(2), match.group(3)
            local = _replace_spec(spec)
            if not local:
                return match.group(0)
            return f"{prefix}{quote}{local}{quote}"

        def _import_call_repl(match: re.Match[str]) -> str:
            quote, spec = match.group(1), match.group(2)
            local = _replace_spec(spec)
            if not local:
                return match.group(0)
            return f"import({quote}{local}{quote})"

        out = _JS_FROM_RE.sub(_from_repl, js_text)
        out = _JS_IMPORT_CALL_RE.sub(_import_call_repl, out)
        return out

    @staticmethod
    def _write_manifest(path: Path, entries: list[ManifestEntry]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(e) for e in entries]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _absolutize(raw_url: str, page_url: str) -> str | None:
        text = (raw_url or "").strip()
        if not text or text.startswith("#") or text.lower().startswith("data:"):
            return None
        if text.startswith(("javascript:", "mailto:", "blob:", "node:")):
            return None
        try:
            absolute = urljoin(page_url, text)
        except Exception:  # noqa: BLE001
            return None
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            return None
        return absolute
