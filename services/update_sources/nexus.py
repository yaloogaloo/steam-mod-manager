"""Nexus Mods update source — stub reserved for page / API fetch."""

from __future__ import annotations

from typing import Any

from services.update_sources.base import UpdateSource, VersionCheckResult


class NexusUpdateSource(UpdateSource):
    """
    Reserved: will use ``source_url`` / ``external_id``.

    V1 returns unsupported so callers can store local versions without network.
    """

    platform = "nexus"

    def check_version(
        self,
        *,
        mod_id: str,
        source_url: str = "",
        external_id: str = "",
        **kwargs: Any,
    ) -> VersionCheckResult:
        # Hook for future Nexus API / page scrape.
        _ = (mod_id, source_url, external_id, kwargs)
        return VersionCheckResult(
            supported=False,
            latest="",
            source="nexus",
            error="unsupported",
        )
