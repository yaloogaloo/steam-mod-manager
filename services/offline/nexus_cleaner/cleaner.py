"""Orchestrate Nexus MHTML clean import → ``.info/offline/index.html``."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from services.offline.nexus_cleaner.html_cleaner import clean_html
from services.offline.nexus_cleaner.layout_optimizer import optimize_layout
from services.offline.nexus_cleaner.mhtml_parser import parse_mhtml
from services.offline.nexus_cleaner.resource_processor import process_resources

logger = logging.getLogger(__name__)

CLEAN_VERSION = "2"
DEFAULT_INDEX_NAME = "index.html"


def _maybe_debug_dir(explicit: Path | str | None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    raw = str(os.environ.get("SMM_NEXUS_CLEANER_DEBUG") or "").strip()
    return Path(raw) if raw else None


def _write_stage(debug_dir: Path | None, name: str, html: str) -> None:
    if debug_dir is None:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / name).write_text(html or "", encoding="utf-8")


class NexusCleaner:
    """
    Nexus-only MHTML cleaner.

    Parse → clean ads/empty chrome → collapse tall blanks → write assets.
    """

    def __init__(
        self, *, clean: bool = True, debug_dir: Path | str | None = None
    ) -> None:
        self.clean = bool(clean)
        self.debug_dir = debug_dir

    def process_file(
        self,
        mhtml_path: str | Path,
        output_dir: Path | str,
    ) -> tuple[Path, int]:
        """
        Convert *mhtml_path* into ``output_dir/index.html`` (+ assets).

        Returns ``(index_path, asset_count)``.
        """
        debug = _maybe_debug_dir(self.debug_dir)
        document = parse_mhtml(mhtml_path)
        html = document.html
        _write_stage(debug, "stage_1_extracted.html", html)

        if self.clean:
            html = clean_html(html)
            _write_stage(debug, "stage_2_cleaned.html", html)
            html = optimize_layout(html)
            _write_stage(debug, "stage_3_optimized.html", html)

        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        rewritten, asset_count = process_resources(
            document, target, html_text=html
        )
        if not rewritten.lstrip().lower().startswith("<!doctype"):
            rewritten = "<!DOCTYPE html>\n" + rewritten
        _write_stage(debug, "stage_4_final.html", rewritten)

        if debug is not None:
            needles = (
                "复制纯链接",
                "请请我喝杯咖啡",
                "dms-link-cleaner",
                "k-support-wrap",
            )
            for stage in (
                "stage_1_extracted.html",
                "stage_2_cleaned.html",
                "stage_3_optimized.html",
                "stage_4_final.html",
            ):
                path = debug / stage
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                hits = [n for n in needles if n in text]
                logger.info(
                    "[NEXUS_CLEANER_DEBUG] %s hits=%s",
                    stage,
                    hits or "(none)",
                )

        index = target / DEFAULT_INDEX_NAME
        tmp = target / f".{DEFAULT_INDEX_NAME}.tmp"
        tmp.write_text(rewritten, encoding="utf-8")
        tmp.replace(index)
        logger.info(
            "[NEXUS_CLEANER] clean=%s assets=%s path=%s",
            self.clean,
            asset_count,
            index,
        )
        return index, asset_count


def clean_mhtml_to_offline(
    mhtml_path: str | Path,
    output_dir: Path | str,
    *,
    clean: bool = True,
) -> tuple[Path, int]:
    """Convenience wrapper used by :mod:`services.offline.manual_import`."""
    return NexusCleaner(clean=clean).process_file(mhtml_path, output_dir)
