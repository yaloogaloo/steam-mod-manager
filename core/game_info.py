"""Game / Store metadata models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class GameInfo:
    """Steam Store metadata for a single AppID."""

    app_id: int
    name: str = ""
    header_image: str = ""
    short_description: str = ""
    # Sanitized folder name used under the Mod library
    folder_name: str = ""
    fetch_error: str | None = None

    @property
    def display_name(self) -> str:
        return self.name.strip() or (f"App_{self.app_id}" if self.app_id else "Unknown Game")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameInfo:
        return cls(
            app_id=int(data.get("app_id") or 0),
            name=str(data.get("name") or ""),
            header_image=str(data.get("header_image") or ""),
            short_description=str(data.get("short_description") or ""),
            folder_name=str(data.get("folder_name") or ""),
            fetch_error=data.get("fetch_error"),
        )

    @classmethod
    def fallback(cls, app_id: int, error: str | None = None) -> GameInfo:
        return cls(
            app_id=app_id,
            name=f"App_{app_id}" if app_id else "Unknown Game",
            folder_name=f"App_{app_id}" if app_id else "Unknown Game",
            fetch_error=error,
        )
