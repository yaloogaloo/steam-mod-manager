"""Offline page provider abstractions and status constants."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OFFLINE_STATUS_NONE,
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

__all__ = [
    "OFFLINE_STATUS_ARCHIVED",
    "OFFLINE_STATUS_FAILED",
    "OFFLINE_STATUS_GENERATED",
    "OFFLINE_STATUS_NONE",
    "OFFLINE_OUTCOME_SUCCESS",
    "OFFLINE_OUTCOME_SKIPPED",
    "OFFLINE_OUTCOME_FAILED",
    "OFFLINE_OUTCOME_RATE_LIMITED",
    "OFFLINE_OUTCOME_NOT_RUN",
    "PROVIDER_GITHUB_GENERATOR",
    "PROVIDER_GITHUB_SNAPSHOT",
    "PROVIDER_MODIO_ARCHIVE",
    "PROVIDER_NEXUS_GENERATOR",
    "PROVIDER_NEXUS_MANUAL_IMPORT",
    "PROVIDER_NEXUS_SNAPSHOT",
    "PROVIDER_STEAM_ARCHIVE",
    "OfflineProvider",
    "OfflineUpdateResult",
    "format_offline_provider",
    "normalize_offline_status",
]

# One-shot operation outcome (distinct from DB ``offline_status``).
OFFLINE_OUTCOME_SUCCESS = "success"
OFFLINE_OUTCOME_SKIPPED = "skipped"
OFFLINE_OUTCOME_FAILED = "failed"
OFFLINE_OUTCOME_RATE_LIMITED = "rate_limited"
OFFLINE_OUTCOME_NOT_RUN = "not_run"


@dataclass(frozen=True)
class OfflineUpdateResult:
    """Outcome of one ``update_offline_page`` / manager refresh."""

    mod_id: str
    index_path: Path
    status: str  # DB offline_status (archived/failed/…)
    provider: str
    error: str = ""
    outcome: str = OFFLINE_OUTCOME_SUCCESS
    skip_reason: str = ""
    force_refresh: bool = False
    http_performed: bool = False
    write_performed: bool = False


class OfflineProvider(ABC):
    """Unified interface for Steam archive and webpage snapshot providers."""

    @abstractmethod
    def can_handle(self, mod: Any) -> bool:
        """Return True when this provider owns *mod* (by platform)."""

    @abstractmethod
    def update_offline_page(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        metadata: Any | None = None,
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        """Create or refresh offline ``index.html`` for *mod_id*."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Stable provider id stored in ``mods.offline_provider``."""

