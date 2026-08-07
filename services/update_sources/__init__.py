"""Update source package."""

from __future__ import annotations

from services.update_sources.base import UpdateSource, VersionCheckResult
from services.update_sources.github import GithubUpdateSource
from services.update_sources.nexus import NexusUpdateSource
from services.update_sources.steam import SteamUpdateSource

__all__ = [
    "UpdateSource",
    "VersionCheckResult",
    "SteamUpdateSource",
    "NexusUpdateSource",
    "GithubUpdateSource",
    "get_update_source",
]


def get_update_source(platform: str) -> UpdateSource:
    key = str(platform or "").strip().lower()
    if key == "nexus":
        return NexusUpdateSource()
    if key == "github":
        return GithubUpdateSource()
    return SteamUpdateSource()
