"""Pluggable Mod update / version check sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class VersionCheckResult:
    """Outcome of one platform version probe."""

    supported: bool = True
    latest: str = ""
    source: str = ""
    error: str = ""
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "latest": self.latest or "",
            "source": self.source or "",
            "error": self.error or "",
        }


class UpdateSource(ABC):
    """Platform-specific version lookup."""

    platform: str = ""

    @abstractmethod
    def check_version(
        self,
        *,
        mod_id: str,
        source_url: str = "",
        external_id: str = "",
        **kwargs: Any,
    ) -> VersionCheckResult:
        """Return latest author version when available."""
