"""Orchestrate Nexus MHTML clean import → ``.info/offline/index.html``."""

from __future__ import annotations

import logging
from pathlib import Path

from services.offline.nexus_cleaner.html_cleaner import clean_html
from services.offline.nexus_cleaner.layout_optimizer import optimize_layout
from services.offline.nexus_cleaner.mhtml_parser import parse_mhtml
from services.offline.nexus_cleaner.resource_processor import process_resources

logger = logging.getLogger(__name__)

CLEAN_VERSION = "1"
DEFAULT_INDEX_NAME = "index.html"


class NexusCleaner:
    """
    Nexus-only MHTML cleaner.

    Parse → clean ads/empty chrome → collapse tall blanks → write assets.
    """

    def __init__(self, *, clean: bool = True) -> None:
        self.clean = bool(clean)

    def process_file(
        self,
        mhtml_path: str | Path,
        output_dir: Path | str,
    ) -> tuple[Path, int]:
        """
        Convert *mhtml_path* into ``output_dir/index.html`` (+ assets).

        Returns ``(index_path, asset_count)``.
        """
        document = parse_mhtml(mhtml_path)
        html = document.html
        if self.clean:
            html = clean_html(html)
            html = optimize_layout(html)

        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        rewritten, asset_count = process_resources(
            document, target, html_text=html
        )
        if not rewritten.lstrip().lower().startswith("<!doctype"):
            rewritten = "<!DOCTYPE html>\n" + rewritten

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
