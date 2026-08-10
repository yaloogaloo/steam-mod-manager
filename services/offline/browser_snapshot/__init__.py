"""Browser-level offline webpage snapshot (Playwright + resource rewrite)."""

from __future__ import annotations

from services.offline.browser_snapshot.manager import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
    run_mod_offline_snapshot,
    snapshot_url,
)
from services.offline.browser_snapshot.playwright_capture import (
    ERROR_BLOCKED_BY_ANTI_BOT,
    ERROR_LOGIN_REQUIRED,
    ERROR_NETWORK_TIMEOUT,
    PageCapture,
    PlaywrightCapture,
    PlaywrightCaptureError,
    detect_bot_challenge,
    detect_page_block_reason,
)
from services.offline.browser_snapshot.resource_rewriter import (
    ManifestEntry,
    ResourceRewriter,
    RewriteResult,
)

__all__ = [
    "ERROR_BLOCKED_BY_ANTI_BOT",
    "ERROR_LOGIN_REQUIRED",
    "ERROR_NETWORK_TIMEOUT",
    "BrowserSnapshotProvider",
    "BrowserSnapshotResult",
    "ManifestEntry",
    "PageCapture",
    "PlaywrightCapture",
    "PlaywrightCaptureError",
    "ResourceRewriter",
    "RewriteResult",
    "detect_bot_challenge",
    "detect_page_block_reason",
    "run_mod_offline_snapshot",
    "snapshot_url",
]
