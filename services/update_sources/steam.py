"""Steam Workshop update source — intentionally unsupported for now."""

from __future__ import annotations

from typing import Any

from services.update_sources.base import UpdateSource, VersionCheckResult


class SteamUpdateSource(UpdateSource):
    platform = "steam"

    def check_version(
        self,
        *,
        mod_id: str,
        source_url: str = "",
        external_id: str = "",
        **kwargs: Any,
    ) -> VersionCheckResult:
        return VersionCheckResult(
            supported=False,
            latest="",
            source="steam",
            error="unsupported",
        )
