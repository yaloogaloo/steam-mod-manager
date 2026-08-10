"""Parse browser MHTML into structured HTML + resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from services.offline.mhtml import extract_mhtml


@dataclass
class MhtmlResource:
    """One non-HTML part extracted from an MHTML archive."""

    name: str
    content_type: str
    data: bytes
    content_id: str = ""


@dataclass
class MhtmlDocument:
    """Parsed MHTML snapshot."""

    html: str
    resources: list[MhtmlResource] = field(default_factory=list)
    cid_to_name: dict[str, str] = field(default_factory=dict)


def parse_mhtml(path: str | Path) -> MhtmlDocument:
    """
    Parse ``.mhtml`` / ``.mht`` into HTML text and resource blobs.

    Reuses the shared email-based extractor in :mod:`services.offline.mhtml`.
    """
    html, assets, cid_map = extract_mhtml(path)
    # Invert cid_map name → cids for content_type hints (best-effort).
    name_to_cid: dict[str, str] = {}
    for cid, name in cid_map.items():
        name_to_cid.setdefault(name, cid)

    resources: list[MhtmlResource] = []
    for name, data in assets.items():
        suffix = Path(name).suffix.lower()
        if suffix == ".css":
            ctype = "text/css"
        elif suffix in {".js", ".mjs"}:
            ctype = "application/javascript"
        elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}:
            ctype = f"image/{suffix.lstrip('.')}"
        elif suffix in {".woff", ".woff2", ".ttf", ".otf"}:
            ctype = "font/woff"
        else:
            ctype = "application/octet-stream"
        resources.append(
            MhtmlResource(
                name=name,
                content_type=ctype,
                data=data,
                content_id=name_to_cid.get(name, ""),
            )
        )
    return MhtmlDocument(html=html, resources=resources, cid_to_name=dict(cid_map))
