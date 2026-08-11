"""Multi-platform offline page providers."""

from __future__ import annotations

from services.offline.base import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_NONE,
    OfflineProvider,
    OfflineUpdateResult,
    PROVIDER_GITHUB_GENERATOR,
    PROVIDER_GITHUB_SNAPSHOT,
    PROVIDER_MODIO_ARCHIVE,
    PROVIDER_NEXUS_GENERATOR,
    PROVIDER_NEXUS_MANUAL_IMPORT,
    PROVIDER_NEXUS_SNAPSHOT,
    PROVIDER_STEAM_ARCHIVE,
    format_offline_provider,
    normalize_offline_status,
)
from services.offline.browser import BrowserSnapshotBackend
from services.offline.browser_snapshot import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
)
from services.offline.github import GithubOfflineProvider
from services.offline.github_browser_snapshot import (
    GitHubBrowserSnapshot,
    GitHubBrowserSnapshotResult,
)
from services.offline.layout_snapshot import (
    GitHubSnapshotProvider,
    LayoutSnapshotDownloader,
    LayoutSnapshotProcessor,
    LayoutSnapshotResult,
)
from services.offline.manager import (
    OfflineManager,
    attach_nexus_offline_html,
    attach_nexus_offline_page,
)
from services.offline.modio import ModioOfflineProvider
from services.offline.paths import (
    resolve_offline_page,
    resolve_offline_page_path,
    offline_page_file_exists,
)
from services.offline.nexus_manual import (
    NexusManualOfflineProvider,
    NexusOfflineProvider,
    store_snapshot,
    validate_html_path,
)
from services.offline.manual_import import (
    UnsupportedOfflineFormat,
    import_offline_snapshot,
    validate_offline_path,
)
from services.offline.snapshot import SnapshotResult, WebSnapshotDownloader
from services.offline.steam import SteamOfflineProvider

__all__ = [
    "OFFLINE_STATUS_ARCHIVED",
    "OFFLINE_STATUS_FAILED",
    "OFFLINE_STATUS_GENERATED",
    "OFFLINE_STATUS_NONE",
    "PROVIDER_GITHUB_GENERATOR",
    "PROVIDER_GITHUB_SNAPSHOT",
    "PROVIDER_MODIO_ARCHIVE",
    "PROVIDER_NEXUS_GENERATOR",
    "PROVIDER_NEXUS_MANUAL_IMPORT",
    "PROVIDER_NEXUS_SNAPSHOT",
    "PROVIDER_STEAM_ARCHIVE",
    "BrowserSnapshotBackend",
    "BrowserSnapshotProvider",
    "BrowserSnapshotResult",
    "GitHubBrowserSnapshot",
    "GitHubBrowserSnapshotResult",
    "GitHubSnapshotProvider",
    "GithubOfflineProvider",
    "LayoutSnapshotDownloader",
    "LayoutSnapshotProcessor",
    "LayoutSnapshotResult",
    "ModioOfflineProvider",
    "NexusManualOfflineProvider",
    "NexusOfflineProvider",
    "OfflineManager",
    "UnsupportedOfflineFormat",
    "attach_nexus_offline_html",
    "attach_nexus_offline_page",
    "import_offline_snapshot",
    "offline_page_file_exists",
    "OfflineProvider",
    "OfflineUpdateResult",
    "resolve_offline_page",
    "resolve_offline_page_path",
    "SnapshotResult",
    "SteamOfflineProvider",
    "WebSnapshotDownloader",
    "format_offline_provider",
    "normalize_offline_status",
    "store_snapshot",
    "validate_html_path",
    "validate_offline_path",
]
