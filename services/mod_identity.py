"""Stable Mod identity for portable ``.info`` + backup matching.

Resolution order (never invent Identity from digits / path / folder name):

1. workspace_id + platform + app context
2. sidecar UUID ``internal_id`` (DB row lookup only)
3. validated source URL
4. legacy fields that may hold Workspace ID (existing entity only)
5. last_known_path (auxiliary)

ARCHITECTURE RULE
-----------------
Only two identity concepts: **Workspace ID** (external) and **Internal ID**
(system). ``Unknown_Mod_<digits>`` embeds a Workspace ID in the display name —
parse it; do not treat Unknown titles as “no identity”.

Ordinary resolve is indexed DB lookups only. Do not reintroduce full backup
table scans on the library-load / reconcile hot path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_modio_external_id_pollution,
    is_provisional_external_id,
    normalize_platform,
    normalize_platform_if_known,
)
from services.file_ops import read_info_metadata_dict

logger = logging.getLogger(__name__)

INTERNAL_ID_KEY = "internal_id"

# ``Unknown_Mod_2314657561`` / ``Unknown Mod 2314657561`` → Workspace ID
_WORKSPACE_FROM_UNKNOWN_MOD_RE = re.compile(
    r"^Unknown[_\s]?Mod[_\s]*(\d+)\s*$",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def read_internal_id(data: dict[str, Any] | None) -> str:
    return _text((data or {}).get(INTERNAL_ID_KEY))


def ensure_internal_id(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Return *data* unchanged — never mint ``internal_id``.

    ``internal_id`` is allocated only by Import / Steam Sync via IdentityService.
    Reconcile, Backup, and directory scans must not create identity.
    """
    return dict(data or {}), False


def source_url_embeds_internal(url: str, *, internal_pk: str = "") -> bool:
    """True when a Steam filedetails URL encodes an Internal Database ID."""
    return _steam_url_embeds_internal(url, internal_pk=internal_pk)


def _steam_url_embeds_internal(url: str, *, internal_pk: str = "") -> bool:
    text = str(url or "").strip()
    if not text or "steamcommunity.com" not in text.lower():
        return False
    compact = text.replace(" ", "")
    if internal_pk and is_internal_mod_id(internal_pk) and f"id={internal_pk}" in compact:
        return True
    from urllib.parse import parse_qs, urlparse

    try:
        ids = parse_qs(urlparse(text).query).get("id", [])
    except Exception:  # noqa: BLE001
        ids = []
    return any(is_internal_mod_id(item) for item in ids)


def extract_workspace_id(
    *,
    workspace_id: str = "",
    external_id: str = "",
    folder_name: str = "",
    title: str = "",
    source_url: str = "",
    legacy_token: str = "",
) -> str:
    """
    Resolve external **Workspace ID** from durable fields / naming.

    ``Unknown_Mod_<digits>`` embeds Workspace ID in the display name — parse it.
    Never returns an Internal Database ID. Never invents identity from arbitrary
    folder text.

    *legacy_token* accepts historical sidecar keys that stored the same external
    digits (must not be treated as a third identity concept).
    """
    _ = folder_name  # directory name is never identity
    for cand in (workspace_id, external_id, legacy_token):
        text = _text(cand)
        if text.isdigit() and not is_internal_mod_id(text):
            return text

    text = _text(title)
    if text:
        match = _WORKSPACE_FROM_UNKNOWN_MOD_RE.match(text)
        if match:
            wid = str(match.group(1) or "").strip()
            if wid.isdigit() and not is_internal_mod_id(wid):
                return wid

    url = _text(source_url)
    if url and "steamcommunity.com" in url.lower() and "id=" in url:
        try:
            ids = parse_qs(urlparse(url).query).get("id", [])
        except Exception:  # noqa: BLE001
            ids = []
        for item in ids:
            text = _text(item)
            if text.isdigit() and not is_internal_mod_id(text):
                return text
    return ""


def _is_empty_mod_placeholder_folder(payload: dict[str, Any]) -> bool:
    """True only for ``Empty Mod <hex>`` — not for Unknown_Mod (has Workspace ID)."""
    from services.identity_service import is_empty_mod_placeholder

    name = _text(payload.get("_folder_name") or payload.get("title") or "")
    title = _text(payload.get("title") or payload.get("display_name") or "")
    return is_empty_mod_placeholder(name) or is_empty_mod_placeholder(title)


def _has_official_identity(payload: dict[str, Any], *, platform: str) -> bool:
    from services.identity_service import has_official_platform_identity

    ws = extract_workspace_id(
        workspace_id=_text(payload.get("workspace_id")),
        external_id=_text(payload.get("external_id")),
        folder_name=_text(payload.get("_folder_name")),
        title=_text(payload.get("title") or payload.get("display_name")),
        source_url=_text(payload.get("url") or payload.get("source_url")),
        legacy_token=_text(payload.get("published_file_id")),
    )
    return has_official_platform_identity(
        platform=platform,
        external_id=_text(payload.get("external_id")) or ws,
        source_url=_text(payload.get("url") or payload.get("source_url")),
        workshop_id=ws if platform in ("", PLATFORM_STEAM) else "",
    )


def _bind_database(db: Any | None) -> Any | None:
    if db is not None:
        return db
    try:
        from core.db_manager import get_db

        return get_db()
    except Exception:  # noqa: BLE001
        return None


def resolve_existing_mod_id(data: dict[str, Any] | None, db: Any | None = None) -> str:
    """
    Find an existing SQLite ``mod_id`` for metadata without creating one.

    Minimal identity model — **entity bind uses ``internal_id`` only**.

    Sync/Import registration rematch must call
    ``DatabaseManager.find_mod_for_registration`` / ``find_duplicate_mod``,
    never this helper.

    Forbidden here: workspace_id, external_id, published_file_id, path, folder name.
    """
    payload = dict(data or {})
    db = _bind_database(db)
    if db is None:
        return ""

    internal = read_internal_id(payload)
    if not internal:
        return ""

    # Empty Mod placeholders never invent identity from title alone.
    if _is_empty_mod_placeholder_folder(payload) and not internal:
        return ""

    try:
        found = db.find_mod_by_internal_id(internal)
        if found is not None:
            return str(found)
    except Exception:  # noqa: BLE001
        pass

    # Numeric internal_id may equal historical PK stored in the field.
    if internal.isdigit():
        try:
            if db.get_mod(internal) is not None:
                return internal
        except Exception:  # noqa: BLE001
            pass
    return ""


def ensure_mod_identity(
    managed_path: str | Path,
    data: dict[str, Any] | None = None,
    db: Any | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """
    Bind *managed_path* to an existing DB entity. Never allocates / mints.

    Requires ``.info`` with ``internal_id`` that already exists in the database.
    Missing / forged ``.info`` → unresolved (ignore).
    Never matches via workspace_id / external_id / published_file_id / path.
    """
    root = Path(managed_path)
    if data is None:
        loaded = read_info_metadata_dict(root)
        if not loaded:
            logger.info("identity ignore %s — no .info", root)
            return "", {}, False
        payload = dict(loaded)
    else:
        payload = dict(data)
        if not payload:
            return "", {}, False
    payload.setdefault("_folder_name", root.name)
    payload.setdefault(
        "_managed_path", str(root.resolve()) if root.exists() else str(root)
    )
    changed = False

    # Strip legacy identity keys from the in-memory payload (do not persist here).
    for legacy in ("published_file_id", "workshop_id"):
        if legacy in payload:
            payload.pop(legacy, None)
            changed = True

    sidecar_uuid = read_internal_id(payload)
    if not sidecar_uuid:
        payload["identity_status"] = "unresolved"
        logger.info("identity ignore %s — .info missing internal_id", root)
        return "", payload, False

    db_bound = _bind_database(db)
    found_pk = ""
    if db_bound is not None:
        try:
            found_pk = str(db_bound.find_mod_by_internal_id(sidecar_uuid) or "")
        except Exception:  # noqa: BLE001
            found_pk = ""
    if not found_pk and sidecar_uuid.isdigit() and db_bound is not None:
        try:
            if db_bound.get_mod(sidecar_uuid) is not None:
                found_pk = sidecar_uuid
        except Exception:  # noqa: BLE001
            found_pk = ""
    if not found_pk:
        payload["identity_status"] = "unresolved"
        logger.info("identity ignore %s — .info internal_id not in DB", root)
        return "", payload, False

    existing = resolve_existing_mod_id(payload, db=db) or found_pk
    if existing:
        payload["identity_status"] = "complete"
        return existing, payload, changed

    payload["identity_status"] = "unresolved"
    logger.info("identity unresolved for %s — will not allocate", root)
    return "", payload, changed
