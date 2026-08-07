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
    PROVIDER_NEXUS_GENERATOR,
    PROVIDER_STEAM_ARCHIVE,
    normalize_offline_status,
)

__all__ = [
    "OFFLINE_STATUS_ARCHIVED",
    "OFFLINE_STATUS_FAILED",
    "OFFLINE_STATUS_GENERATED",
    "OFFLINE_STATUS_NONE",
    "PROVIDER_GITHUB_GENERATOR",
    "PROVIDER_NEXUS_GENERATOR",
    "PROVIDER_STEAM_ARCHIVE",
    "OfflineProvider",
    "OfflineUpdateResult",
    "normalize_offline_status",
]


@dataclass(frozen=True)
class OfflineUpdateResult:
    """Outcome of one ``update_offline_page`` / manager refresh."""

    mod_id: str
    index_path: Path
    status: str
    provider: str
    error: str = ""


class OfflineProvider(ABC):
    """Unified interface for Steam archive and local HTML generators."""

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
    ) -> OfflineUpdateResult:
        """Create or refresh ``.info/index.html`` for *mod_id*."""

    @abstractmethod
    def get_provider_name(self) -> str:
        """Stable provider id stored in ``mods.offline_provider``."""
