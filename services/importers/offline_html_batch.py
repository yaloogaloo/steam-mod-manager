"""Batch offline HTML/MHTML file sources for ImportWorker."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from core.sanitize import sanitize_folder_name

OFFLINE_HTML_SUFFIXES = {".html", ".htm", ".mhtml", ".mht"}


def is_offline_html_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in OFFLINE_HTML_SUFFIXES


def normalize_offline_html_paths(
    paths: list[str] | tuple[str, ...] | None,
) -> list[Path]:
    """Deduplicate existing offline page files while preserving order."""
    out: list[Path] = []
    seen: set[str] = set()
    for raw in paths or ():
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_file() or not is_offline_html_path(path):
            continue
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(path.resolve())
    return out


def make_empty_mod_stub(*, title: str = "", ident: str = "") -> Path:
    """Empty payload folder — same role as the single-import stub."""
    raw = str(title or ident or "").strip() or f"Empty_Mod_{uuid.uuid4().hex[:8]}"
    label = sanitize_folder_name(raw, fallback="Empty_Mod")
    dest = Path(tempfile.mkdtemp(prefix="smm_empty_import_")) / label
    dest.mkdir(parents=True, exist_ok=True)
    return dest
