"""Write MHTML resources under ``assets/`` and rewrite ``cid:`` / embedded URLs."""

from __future__ import annotations

from pathlib import Path

from services.offline.mhtml import rewrite_cid_references, rewrite_embedded_urls
from services.offline.nexus_cleaner.mhtml_parser import MhtmlDocument


def _build_cid_to_href(document: MhtmlDocument) -> dict[str, str]:
    cid_to_href = {
        cid: f"./assets/{name}" for cid, name in document.cid_to_name.items()
    }
    for resource in document.resources:
        cid_to_href.setdefault(resource.name, f"./assets/{resource.name}")
        if resource.content_id:
            cid_to_href.setdefault(resource.content_id, f"./assets/{resource.name}")
    return cid_to_href


def _rewrite_text(text: str, mapping: dict[str, str]) -> str:
    out = rewrite_cid_references(text, mapping)
    return rewrite_embedded_urls(out, mapping)


def process_resources(
    document: MhtmlDocument,
    output_dir: Path | str,
    *,
    html_text: str | None = None,
) -> tuple[str, int]:
    """
    Persist resources to ``output_dir/assets/`` and rewrite CID / embedded URLs.

    Returns ``(rewritten_html, asset_count)``.
    """
    target = Path(output_dir)
    assets_root = target / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)

    cid_to_href = _build_cid_to_href(document)
    # CSS url(…) → relative to the CSS file under assets/ → ./image.png
    css_cid_to_href = {
        key: f"./{Path(href).name}" for key, href in cid_to_href.items()
    }

    written = 0
    for resource in document.resources:
        dest = assets_root / resource.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = resource.data
        if resource.content_type.startswith("text/css") or dest.suffix.lower() == ".css":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            if text:
                data = _rewrite_text(text, css_cid_to_href).encode("utf-8")
        dest.write_bytes(data)
        written += 1

    body = html_text if html_text is not None else document.html
    rewritten = _rewrite_text(body, cid_to_href)
    return rewritten, written
