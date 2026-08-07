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
    PROVIDER_NEXUS_GENERATOR,
    PROVIDER_STEAM_ARCHIVE,
    normalize_offline_status,
)
from services.offline.github import GithubOfflineProvider
from services.offline.manager import OfflineManager
from services.offline.nexus import NexusOfflineProvider
from services.offline.steam import SteamOfflineProvider

__all__ = [
    "OFFLINE_STATUS_ARCHIVED",
    "OFFLINE_STATUS_FAILED",
    "OFFLINE_STATUS_GENERATED",
    "OFFLINE_STATUS_NONE",
    "PROVIDER_GITHUB_GENERATOR",
    "PROVIDER_NEXUS_GENERATOR",
    "PROVIDER_STEAM_ARCHIVE",
    "GithubOfflineProvider",
    "NexusOfflineProvider",
    "OfflineManager",
    "OfflineProvider",
    "OfflineUpdateResult",
    "SteamOfflineProvider",
    "normalize_offline_status",
]
