"""Stable Mod identity for portable ``.info`` + backup matching."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from core.mod_platform import normalize_platform
from services.file_ops import read_info_metadata_dict

logger = logging.getLogger(__name__)

INTERNAL_ID_KEY = "internal_id"


def _text(value: Any) -> str:
    return str(value or "").strip()


def read_internal_id(data: dict[str, Any] | None) -> str:
    return _text((data or {}).get(INTERNAL_ID_KEY))


def ensure_internal_id(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Ensure ``internal_id`` UUID exists in *data*.

    Returns ``(data, changed)``. Never invents from directory names.
    """
    payload = dict(data or {})
    current = read_internal_id(payload)
    if current:
        return payload, False
    payload[INTERNAL_ID_KEY] = str(uuid.uuid4())
    return payload, True


def resolve_existing_mod_id(data: dict[str, Any] | None) -> str:
    """
    Find an existing SQLite ``mod_id`` for metadata without creating one.

    Priority: internal_id → published_file_id → platform+external_id → workspace_id.
    """
    payload = dict(data or {})
    try:
        from core.db_manager import get_db

        db = get_db()
    except Exception:  # noqa: BLE001
        return ""

    internal = read_internal_id(payload)
    if internal:
        try:
            found = db.find_mod_by_internal_id(internal)
            if found is not None:
                return str(found)
        except Exception:  # noqa: BLE001
            pass

    pub = _text(payload.get("published_file_id"))
    if pub.isdigit():
        try:
            if db.get_mod(pub) is not None:
                return pub
            row = db.get_mod_backup_row(pub)
            if row is not None:
                return pub
        except Exception:  # noqa: BLE001
            pass

    platform = normalize_platform(
        _text(payload.get("source_type") or payload.get("platform"))
    )
    external = _text(payload.get("external_id"))
    if platform and external:
        try:
            info = db.find_mod_by_external(platform, external)
            if info is not None and str(info.mod_id).isdigit():
                return str(info.mod_id)
        except Exception:  # noqa: BLE001
            pass

    workspace = _text(payload.get("workspace_id"))
    if workspace:
        try:
            found = db.find_mod_by_workspace_id(workspace, platform=platform or None)
            if found is not None:
                return str(found)
        except Exception:  # noqa: BLE001
            pass

    # Scan backup snapshots for portable identity (rename / migrate).
    try:
        from services.metadata_backup import backup_root, load_backup

        for row in db.iter_mod_backup_rows():
            mid = _text(row.get("mod_id"))
            if not mid.isdigit():
                continue
            snap = load_backup(mid)
            if snap is None:
                continue
            meta = snap.metadata or {}
            if internal and read_internal_id(meta) == internal:
                return mid
            if pub.isdigit() and _text(meta.get("published_file_id")) == pub:
                return mid
            if workspace and _text(meta.get("workspace_id")) == workspace:
                if not platform or normalize_platform(
                    _text(meta.get("source_type") or meta.get("platform"))
                ) in ("", platform):
                    return mid
            if (
                platform
                and external
                and normalize_platform(
                    _text(meta.get("source_type") or meta.get("platform"))
                )
                == platform
                and _text(meta.get("external_id")) == external
            ):
                return mid
            # Keep linter happy — backup_root referenced for API clarity.
            _ = backup_root(mid)
    except Exception:  # noqa: BLE001
        logger.debug("backup identity scan failed", exc_info=True)

    return ""


def ensure_mod_identity(
    managed_path: str | Path,
    data: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """
    Resolve or create a stable Mod identity for *managed_path*.

    Returns ``(mod_id, metadata, metadata_changed)``.

    Priority for mod_id:
    published_file_id (digit) → matched existing → allocate new non-Steam id.

    Always ensures ``internal_id`` UUID in metadata. When a new numeric id is
    allocated, it is written to ``published_file_id`` so backup keys stay numeric
    (compatible with ``data/mod_backup/<id>``).
    """
    root = Path(managed_path)
    payload = dict(data if data is not None else (read_info_metadata_dict(root) or {}))
    payload, changed = ensure_internal_id(payload)

    existing = resolve_existing_mod_id(payload)
    pub = _text(payload.get("published_file_id"))

    if existing:
        mod_id = existing
        if pub != mod_id and not (pub.isdigit() and pub == mod_id):
            # Keep Steam workshop ids; for matched rows prefer existing mod_id.
            if not pub.isdigit() or int(pub) != int(mod_id):
                if not pub.isdigit():
                    payload["published_file_id"] = mod_id
                    changed = True
        return mod_id, payload, changed

    if pub.isdigit():
        return pub, payload, changed

    # Brand-new Mod: allocate numeric id for SQLite / backup key.
    try:
        from core.db_manager import get_db

        mod_id = str(get_db().allocate_mod_id())
    except Exception:  # noqa: BLE001
        # Extremely rare: DB unavailable — still persist UUID; caller may retry.
        mod_id = ""
        logger.warning("allocate_mod_id failed for %s", root)
        return mod_id, payload, changed

    payload["published_file_id"] = mod_id
    changed = True
    return mod_id, payload, changed
