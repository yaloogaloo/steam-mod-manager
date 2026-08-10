"""Orchestrate Playwright capture + resource rewrite, with optional legacy fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.offline.browser_snapshot.playwright_capture import (
    PlaywrightCapture,
    PlaywrightCaptureError,
)
from services.offline.browser_snapshot.resource_rewriter import ResourceRewriter

logger = logging.getLogger(__name__)


@dataclass
class BrowserSnapshotResult:
    """Outcome of ``BrowserSnapshotProvider.snapshot``."""

    success: bool
    html_path: Path
    asset_count: int
    error: str | None = None
    used_fallback: bool = False
    manifest_path: Path | None = None
    backend: str = "browser"  # browser | legacy | info_fallback


class BrowserSnapshotProvider:
    """
    Primary offline snapshot path for Nexus / GitHub.

    1. Playwright Chromium → final DOM after JS render
    2. ResourceRewriter → download + rewrite + snapshot_manifest.json
    3. Optional legacy ``WebSnapshotDownloader`` (GitHub only by default)
    """

    def __init__(
        self,
        *,
        capture: PlaywrightCapture | None = None,
        capture_factory: Callable[[], PlaywrightCapture] | None = None,
        rewriter: ResourceRewriter | None = None,
        rewriter_factory: Callable[[], ResourceRewriter] | None = None,
        enable_legacy_fallback: bool = True,
    ) -> None:
        self._capture = capture
        self._capture_factory = capture_factory
        self._rewriter = rewriter
        self._rewriter_factory = rewriter_factory
        self.enable_legacy_fallback = bool(enable_legacy_fallback)

    def _get_capture(self) -> PlaywrightCapture:
        if self._capture is not None:
            return self._capture
        if self._capture_factory is not None:
            self._capture = self._capture_factory()
            return self._capture
        self._capture = PlaywrightCapture()
        return self._capture

    def _get_rewriter(self) -> ResourceRewriter:
        if self._rewriter is not None:
            return self._rewriter
        if self._rewriter_factory is not None:
            self._rewriter = self._rewriter_factory()
            return self._rewriter
        self._rewriter = ResourceRewriter()
        return self._rewriter

    def snapshot(self, url: str, output_dir: Path | str) -> BrowserSnapshotResult:
        """Capture *url* into *output_dir* (``index.html`` + ``assets/`` + manifest)."""
        target = Path(output_dir)
        index_path = target / "index.html"
        page_url = str(url or "").strip()
        if not page_url:
            return BrowserSnapshotResult(
                success=False,
                html_path=index_path,
                asset_count=0,
                error="Empty URL",
                backend="browser",
            )

        browser_error: str | None = None
        try:
            page = self._get_capture().capture(page_url)
            rewriter = self._get_rewriter()
            rewritten = rewriter.rewrite(page, target)
            if rewritten.success and rewritten.html_path.is_file():
                return BrowserSnapshotResult(
                    success=True,
                    html_path=rewritten.html_path,
                    asset_count=rewritten.asset_count,
                    error=None,
                    used_fallback=False,
                    manifest_path=rewritten.manifest_path,
                    backend="browser",
                )
            browser_error = rewritten.error or "Resource rewrite failed"
            logger.warning(
                "Browser snapshot rewrite failed for %s: %s", page_url, browser_error
            )
        except PlaywrightCaptureError as exc:
            browser_error = exc.code or str(exc)
            logger.info(
                "Playwright capture failed for %s: %s",
                page_url,
                browser_error,
            )
        except Exception as exc:  # noqa: BLE001
            browser_error = str(exc)
            logger.warning("Browser snapshot error for %s: %s", page_url, exc)

        if not self.enable_legacy_fallback:
            return BrowserSnapshotResult(
                success=False,
                html_path=index_path,
                asset_count=0,
                error=browser_error or "Browser snapshot failed",
                used_fallback=False,
                backend="browser",
            )

        return self._legacy_fallback(page_url, target, browser_error)

    def _legacy_fallback(
        self,
        page_url: str,
        target: Path,
        browser_error: str | None,
    ) -> BrowserSnapshotResult:
        """Degrade to ``services.offline.snapshot.WebSnapshotDownloader`` (GitHub path)."""
        try:
            from services.offline.snapshot import WebSnapshotDownloader
        except Exception as exc:  # noqa: BLE001
            msg = f"Browser failed ({browser_error}); legacy unavailable: {exc}"
            return BrowserSnapshotResult(
                success=False,
                html_path=target / "index.html",
                asset_count=0,
                error=msg,
                used_fallback=True,
                backend="legacy",
            )

        try:
            with WebSnapshotDownloader() as downloader:
                snap = downloader.download(page_url, target)
            if snap.success and snap.html_path.is_file():
                note = (
                    f"Degraded to legacy snapshot after browser failure: {browser_error}"
                    if browser_error
                    else None
                )
                return BrowserSnapshotResult(
                    success=True,
                    html_path=snap.html_path,
                    asset_count=snap.asset_count,
                    error=note,
                    used_fallback=True,
                    manifest_path=None,
                    backend="legacy",
                )
            return BrowserSnapshotResult(
                success=False,
                html_path=snap.html_path,
                asset_count=0,
                error=(
                    f"Browser failed ({browser_error}); "
                    f"legacy also failed ({snap.error})"
                ),
                used_fallback=True,
                backend="legacy",
            )
        except Exception as exc:  # noqa: BLE001
            return BrowserSnapshotResult(
                success=False,
                html_path=target / "index.html",
                asset_count=0,
                error=f"Browser failed ({browser_error}); legacy error: {exc}",
                used_fallback=True,
                backend="legacy",
            )


def snapshot_url(url: str, output_dir: Path | str, **kwargs: Any) -> BrowserSnapshotResult:
    """Convenience entry point."""
    return BrowserSnapshotProvider(**kwargs).snapshot(url, output_dir)


def run_mod_offline_snapshot(
    mod_id: str | int,
    *,
    provider_name: str,
    source_url: str,
    output_dir: Path | str,
    snapshot_provider: BrowserSnapshotProvider | None = None,
) -> tuple[BrowserSnapshotResult, str]:
    """
    Run snapshot for one Mod page.

    Returns ``(result, offline_status)``.
    - Browser success → status archived
    - Legacy degrade success → status failed (page may still exist; error explains degrade)
    - Total failure → status failed
    """
    from core.mod_platform import OFFLINE_STATUS_ARCHIVED, OFFLINE_STATUS_FAILED

    del mod_id, provider_name  # status columns updated by caller
    provider = snapshot_provider or BrowserSnapshotProvider()
    result = provider.snapshot(source_url, output_dir)
    if result.success and not result.used_fallback:
        return result, OFFLINE_STATUS_ARCHIVED
    if result.success and result.used_fallback:
        if not result.error:
            result.error = "Browser snapshot unavailable; used legacy snapshot"
        return result, OFFLINE_STATUS_FAILED
    return result, OFFLINE_STATUS_FAILED
