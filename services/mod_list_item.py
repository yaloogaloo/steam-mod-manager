"""Layer-1 Mod list DTOs — Library grid only.

ARCHITECTURE RULE
-----------------
``ModListItem`` is the **only** payload allowed for Library list / game-switch.
It must stay free of description, HTML, offline trees, hashes, and filesystem
scan results. Detail and heavy resources load on demand via separate layers.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


# Allowed attribute names for architecture tests (Layer 1 contract).
MOD_LIST_ITEM_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "internal_id",
        "workspace_id",
        "game_id",
        "game_folder",
        "name",
        "favorite",
        "cover_path",
        "status_badge",
        "managed_path",
        "platform",
        "deployed",
        "folder_absent",
        "content_status",
        "identity_status",
        "conflict",
        "conflict_status",
        "invalid",
        "abandoned",
        "enabled",
        "has_offline",
        "mtime",
        "category_tags",
        "external_id",
        "source_url",
        "source_type",
        "deploy_status",
        "offline_status",
        "library_status",
        "steam_name",
        "relation_deps",
        "relation_conflicts",
        "notes_preview",
        "local_size_bytes",
        "local_size_status",
        "type_id",
    }
)

# Explicitly forbidden on Layer 1 (architecture guard).
MOD_LIST_ITEM_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "html",
        "offline_html",
        "hash",
        "file_hash",
        "file_list",
        "payload_scan",
        "backup_content",
        "mod_files",
        "manifest",
    }
)


@dataclass(frozen=True, slots=True)
class ModListItem:
    """Lightweight row for Library list display and filtering."""

    internal_id: str
    workspace_id: str
    game_id: int
    game_folder: str
    name: str
    favorite: bool = False
    cover_path: str = ""
    status_badge: str = ""
    managed_path: str = ""
    platform: str = ""
    deployed: bool = False
    folder_absent: bool = False
    content_status: str = ""
    identity_status: str = "ok"
    conflict: bool = False
    conflict_status: str = "none"
    invalid: bool = False
    abandoned: bool = False
    enabled: bool = True
    has_offline: bool = False
    mtime: float = 0.0
    category_tags: str = ""
    external_id: str = ""
    source_url: str = ""
    source_type: str = ""
    deploy_status: str = "not_deployed"
    offline_status: str = "none"
    library_status: str = ""
    steam_name: str = ""
    relation_deps: int = 0
    relation_conflicts: int = 0
    # Short notes for search only — not full metadata body.
    notes_preview: str = ""
    # Local managed-dir size observation (None = unknown; 0 + ok = empty).
    local_size_bytes: int | None = None
    local_size_status: str = "unknown"
    # Game-scoped Type Definition id. None = unbound. Not a type name.
    type_id: int | None = None


def assert_mod_list_item_layer1(item: ModListItem) -> None:
    """Raise if *item* carries forbidden Layer-1 fields (dynamic attrs)."""
    names = {f.name for f in fields(item)}
    bad = names & MOD_LIST_ITEM_FORBIDDEN_FIELDS
    if bad:
        raise AssertionError(f"ModListItem contains forbidden fields: {sorted(bad)}")
    extra = names - MOD_LIST_ITEM_ALLOWED_FIELDS
    if extra:
        raise AssertionError(f"ModListItem has undeclared fields: {sorted(extra)}")
