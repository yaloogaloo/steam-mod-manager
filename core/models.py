"""Data models for Steam Workshop mods."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_UNKNOWN_TITLE_RE = re.compile(
    r"^Unknown[_\s]?Mod(?:[_\s]*(\d+))?\s*$",
    re.IGNORECASE,
)


def is_unknown_mod_title(title: str | None, *, published_file_id: str = "") -> bool:
    """True when *title* is empty, numeric, or an ``Unknown_Mod_*`` / ``Unknown Mod`` placeholder."""
    text = str(title or "").strip()
    if not text:
        return True
    if text.isdigit():
        return True
    match = _UNKNOWN_TITLE_RE.match(text)
    if match:
        return True
    mid = str(published_file_id or "").strip()
    if mid and text in {
        f"Unknown_Mod_{mid}",
        f"Unknown Mod {mid}",
        f"Unknown Mod_{mid}",
    }:
        return True
    return False


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
    game_name: str = ""  # English store name (sanitized when used as folder)
    tags: list[str] = field(default_factory=list)
    source_path: str | None = None
    # Local paths after sync (filled by later steps)
    managed_path: str | None = None
    local_path: str | None = None  # runtime only — backfilled from disk, not in JSON
    url: str = ""  # portable source link (JSON key: ``url``)
    cover_path: str | None = None
    offline_page_path: str | None = None
    fetch_error: str | None = None
    custom_notes: str = ""  # user notes stored in .info/mod.json
    # Optional author / uploader label (Mod.io ``submitted_by``, etc.).
    author: str = ""
    # Mod-level source platform (canonical JSON key: ``source_type``).
    source_type: str = ""
    # Portable UI label from JSON ``display_name`` (not the ``display_name`` property).
    json_display_name: str = ""

    @property
    def game_display_name(self) -> str:
        """Prefer resolved game name; fall back to App_<id> / Unknown Game."""
        if self.game_name.strip():
            return self.game_name.strip()
        if self.app_id:
            return f"App_{self.app_id}"
        return "Unknown Game"

    @property
    def workshop_url(self) -> str:
        return (
            "https://steamcommunity.com/sharedfiles/filedetails/"
            f"?id={self.published_file_id}"
        )

    @property
    def display_name(self) -> str:
        """
        Human-readable Mod name for UI and folders.

        Prefers JSON ``display_name`` when set to a real name; never returns a
        bare numeric ID. Placeholder ``Unknown Mod*`` labels fall through to title.
        """
        custom = (self.json_display_name or "").strip()
        if custom and not is_unknown_mod_title(
            custom, published_file_id=str(self.published_file_id or "")
        ):
            return custom
        return self.effective_title()

    def effective_title(self) -> str:
        """Real title if usable; otherwise ``Unknown_Mod_<published_file_id>``."""
        title = (self.title or "").strip()
        mid = str(self.published_file_id or "")
        if title and not title.isdigit() and not is_unknown_mod_title(
            title, published_file_id=mid
        ):
            return title
        return f"Unknown_Mod_{self.published_file_id}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        custom = str(data.pop("json_display_name", "") or "").strip()
        mid = str(self.published_file_id or "")
        if custom and not is_unknown_mod_title(custom, published_file_id=mid):
            data["display_name"] = custom
        return data

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
