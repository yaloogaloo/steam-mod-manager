"""Unified manual offline snapshot import (HTML / MHTML)."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.mod_platform import PROVIDER_NEXUS_MANUAL_IMPORT
from services.offline.html_rewriter import rewrite_imported_html
from services.offline.mhtml import MHTML_SUFFIXES, store_mhtml_snapshot

logger = logging.getLogger(__name__)

DEFAULT_INDEX_NAME = "index.html"
HTML_SUFFIXES = {".html", ".htm"}
SUPPORTED_OFFLINE_SUFFIXES = HTML_SUFFIXES | MHTML_SUFFIXES


class UnsupportedOfflineFormat(ValueError):
    """Raised when the offline snapshot file type is not supported."""


def detect_source_format(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in HTML_SUFFIXES:
        return "html"
    if suffix in MHTML_SUFFIXES:
        return "mhtml"
    raise UnsupportedOfflineFormat(
        f"不支持的离线页面格式: {suffix or '(无后缀)'}。"
        f"请使用 {', '.join(sorted(SUPPORTED_OFFLINE_SUFFIXES))}。"
    )


def validate_offline_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise FileNotFoundError(f"离线页面文件不存在: {candidate}")
    detect_source_format(candidate)
    return candidate.resolve()


def _write_metadata(
    output_dir: Path,
    *,
    source_format: str,
    source_file: str,
    source_url: str = "",
    title: str = "",
    asset_count: int = 0,
    cleaned: bool | None = None,
    clean_version: str | None = None,
) -> None:
    meta = {
        "provider": PROVIDER_NEXUS_MANUAL_IMPORT,
        "source": "manual_browser_save",
        "source_format": source_format,
        "import_time": datetime.now(timezone.utc).isoformat(),
        "original_url": source_url or "",
        "title": title or "",
        "source_file": source_file,
        "asset_count": asset_count,
    }
    if cleaned is not None:
        meta["cleaned"] = bool(cleaned)
    if clean_version:
        meta["clean_version"] = str(clean_version)
    (output_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _import_html(
    src: Path,
    output_dir: Path,
    *,
    source_url: str = "",
    title: str = "",
) -> tuple[Path, int]:
    raw = src.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        raise ValueError("HTML 文件为空")
    rewritten, asset_count = rewrite_imported_html(
        raw,
        html_path=src,
        output_dir=output_dir,
        keep_remote=True,
    )
    index = output_dir / DEFAULT_INDEX_NAME
    tmp = output_dir / f".{DEFAULT_INDEX_NAME}.tmp"
    tmp.write_text(rewritten, encoding="utf-8")
    tmp.replace(index)
    _write_metadata(
        output_dir,
        source_format="html",
        source_file=src.name,
        source_url=source_url,
        title=title,
        asset_count=asset_count,
    )
    return index, asset_count


def _import_mhtml(
    src: Path,
    output_dir: Path,
    *,
    source_url: str = "",
    title: str = "",
    clean: bool = True,
) -> tuple[Path, int]:
    """
    Import MHTML.

    When *clean* is True (default), run Nexus Offline Snapshot Cleaner
    (``mhtml_clean_import``). When False, store the raw conversion only.
    """
    if clean:
        from services.offline.nexus_cleaner import CLEAN_VERSION, clean_mhtml_to_offline

        index, asset_count = clean_mhtml_to_offline(src, output_dir, clean=True)
        _write_metadata(
            output_dir,
            source_format="mhtml",
            source_file=src.name,
            source_url=source_url,
            title=title,
            asset_count=asset_count,
            cleaned=True,
            clean_version=CLEAN_VERSION,
        )
    else:
        index, asset_count = store_mhtml_snapshot(src, output_dir)
        _write_metadata(
            output_dir,
            source_format="mhtml",
            source_file=src.name,
            source_url=source_url,
            title=title,
            asset_count=asset_count,
            cleaned=False,
        )
    return index, asset_count


def import_offline_snapshot(
    path: str | Path,
    output_dir: Path | str,
    *,
    source_url: str = "",
    title: str = "",
    clean: bool = True,
) -> tuple[Path, int, str]:
    """
    Import a user-saved offline page into ``output_dir``.

    Supports ``.html`` / ``.htm`` and ``.mhtml`` / ``.mht``.

    *clean* applies to MHTML only (Nexus layout optimizer). HTML import is unchanged.

    Returns ``(index_path, asset_count, source_format)``.
    """
    src = validate_offline_path(path)
    source_format = detect_source_format(src)
    target = Path(output_dir)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    (target / "assets").mkdir(parents=True, exist_ok=True)

    if source_format == "html":
        index, count = _import_html(
            src, target, source_url=source_url, title=title
        )
    elif source_format == "mhtml":
        index, count = _import_mhtml(
            src, target, source_url=source_url, title=title, clean=clean
        )
    else:
        raise UnsupportedOfflineFormat(source_format)

    logger.info(
        "[OFFLINE_IMPORT] format=%s assets=%s path=%s",
        source_format,
        count,
        index,
    )
    return index, count, source_format
