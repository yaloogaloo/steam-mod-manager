"""Runtime guard: metadata content must belong to the entity's internal_id / app_id."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

_NEXUS_SLUG_APP: dict[str, int] = {
    "stardewvalley": 413150,
    "baldursgate3": 1086940,
    "cyberpunk2077": 1091500,
    "witcher3": 292030,
    "palworld": 1623730,
    "kingdomcomedeliverance2": 1771300,
    "skyrimspecialedition": 489830,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def app_id_from_source_url(url: str) -> int:
    raw = _text(url)
    if not raw or "nexusmods.com" not in raw.lower():
        return 0
    try:
        parts = [p for p in urlparse(raw).path.split("/") if p]
        if "mods" in parts:
            idx = parts.index("mods")
            if idx > 0:
                return int(_NEXUS_SLUG_APP.get(parts[idx - 1].lower(), 0) or 0)
    except Exception:
        return 0
    return 0


def payload_metadata_app_id(payload: dict[str, Any] | None) -> int:
    data = dict(payload or {})
    try:
        declared = int(data.get("app_id") or 0)
    except (TypeError, ValueError):
        declared = 0
    url = _text(data.get("url") or data.get("source_url") or data.get("website"))
    return declared or app_id_from_source_url(url)


def metadata_payload_is_foreign(
    payload: dict[str, Any] | None,
    *,
    entity_app_id: int,
) -> bool:
    """
    True when *payload* metadata evidence belongs to another game.

    Ownership key is the entity (caller passes entity_app_id from DB by internal_id).
    Never uses workspace_id / external_id / folder name.
    """
    app = int(entity_app_id or 0)
    if app <= 0:
        return False
    meta_app = payload_metadata_app_id(payload)
    return meta_app > 0 and meta_app != app


def resolve_owner_mod_id_from_info(data: dict[str, Any] | None) -> str:
    """
    Resolve backup/sync target Internal Database ID from .info only.

    Allowed: ``internal_id`` → DB lookup / numeric PK.
    Forbidden: published_file_id, workspace_id, external_id, folder name, source_url alone.
    """
    payload = dict(data or {})
    from services.mod_identity import read_internal_id, resolve_existing_mod_id

    found = resolve_existing_mod_id(payload)
    if found.isdigit():
        return found
    # Historical: numeric internal_id field already is PK.
    internal = read_internal_id(payload)
    if internal.isdigit():
        try:
            from core.db_manager import get_db

            if get_db().get_mod(internal) is not None:
                return internal
        except Exception:  # noqa: BLE001
            pass
    return ""
