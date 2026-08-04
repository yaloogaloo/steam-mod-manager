"""Data models for Steam Workshop mods."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ModMetadata:
    """Metadata for a single Steam Workshop item."""

    published_file_id: str
    title: str = ""
    description: str = ""
    preview_url: str = ""
    file_size: int = 0
    time_created: int = 0
    time_updated: int = 0
    creator_steam_id: str = ""
    app_id: int = 0
    tags: list[str] = field(default_factory=list)
    source_path: str | None = None
    # Local paths after sync (filled by later steps)
    managed_path: str | None = None
    cover_path: str | None = None
    offline_page_path: str | None = None
    fetch_error: str | None = None

    @property
    def workshop_url(self) -> str:
        return (
            "https://steamcommunity.com/sharedfiles/filedetails/"
            f"?id={self.published_file_id}"
        )

    @property
    def display_name(self) -> str:
        """Prefer Steam title; fall back to numeric ID."""
        return self.title.strip() or self.published_file_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_api_response(cls, item: dict[str, Any]) -> ModMetadata:
        """Build from a GetPublishedFileDetails result item."""
        tags_raw = item.get("tags") or []
        tags: list[str] = []
        for tag in tags_raw:
            if isinstance(tag, dict):
                name = tag.get("tag")
                if name:
                    tags.append(str(name))
            elif tag:
                tags.append(str(tag))

        result = int(item.get("result", 0))
        file_id = str(item.get("publishedfileid", "") or "")

        if result != 1:
            return cls(
                published_file_id=file_id,
                fetch_error=f"Steam API result code: {result}",
            )

        return cls(
            published_file_id=file_id,
            title=str(item.get("title") or ""),
            description=str(item.get("description") or ""),
            preview_url=str(item.get("preview_url") or ""),
            file_size=int(item.get("file_size") or 0),
            time_created=int(item.get("time_created") or 0),
            time_updated=int(item.get("time_updated") or 0),
            creator_steam_id=str(item.get("creator") or ""),
            app_id=int(item.get("consumer_app_id") or item.get("creator_app_id") or 0),
            tags=tags,
        )
