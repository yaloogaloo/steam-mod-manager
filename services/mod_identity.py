"""Stable Mod identity for portable ``.info`` + backup matching.

Resolution order (never invent Identity from digits / path / folder name):

1. workspace_id + platform + app context
2. sidecar UUID ``internal_id`` (DB row lookup only)
3. validated source URL
4. legacy published_file_id / external_id (existing entity only)
5. last_known_path (auxiliary)

A numeric legacy field is never Steam proof.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

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


def _is_placeholder_folder(payload: dict[str, Any]) -> bool:
    from services.identity_service import is_empty_mod_placeholder

    name = _text(payload.get("_folder_name") or payload.get("title") or "")
    title = _text(payload.get("title") or payload.get("display_name") or "")
    return is_empty_mod_placeholder(name) or is_empty_mod_placeholder(title)


def _has_official_identity(payload: dict[str, Any], *, platform: str) -> bool:
    from services.identity_service import has_official_platform_identity

    return has_official_platform_identity(
        platform=platform,
        external_id=_text(payload.get("external_id")),
        source_url=_text(payload.get("url") or payload.get("source_url")),
        workshop_id=_text(payload.get("workspace_id")),
    )


def _lookup_workspace(
    db: Any,
    workspace: str,
    *,
    platform: str = "",
    app_id: int = 0,
) -> str:
    if not workspace or is_internal_mod_id(workspace):
        return ""
    try:
        found = db.find_mod_by_workspace_id(
            workspace, platform=platform or None, app_id=app_id
        )
        if found:
            return str(found)
        if app_id > 0:
            found = db.find_mod_by_workspace_id(
                workspace, platform=platform or None, app_id=0
            )
            if found:
                return str(found)
        if platform:
            found = db.find_mod_by_workspace_id(workspace, platform=None, app_id=0)
            if found:
                info = db.get_mod_display_info(found)
                if info is not None and normalize_platform(info.platform) in (
                    "",
                    platform,
                ):
                    return str(found)
        # Unique workspace_id across the DB — rename recovery without platform.
        found = db.find_mod_by_workspace_id(workspace, platform=None, app_id=0)
        if found:
            return str(found)
    except Exception:  # noqa: BLE001
        logger.debug("workspace_id lookup failed", exc_info=True)
    return ""


def _legacy_token_matches_row(info: Any, token: str) -> bool:
    if not token or info is None:
        return False
    return token in (
        _text(getattr(info, "workspace_id", "")),
        _text(getattr(info, "external_id", "")),
    )


def _lookup_relocated_folder(db: Any, payload: dict[str, Any]) -> str:
    """Bind a vanished last_known_path when sidecar still carries durable identity.

    Path is auxiliary: a unique missing sibling is not enough by itself.
    """
    managed = _text(payload.get("_managed_path"))
    if not managed:
        return ""
    try:
        folder = Path(managed).resolve()
    except OSError:
        return ""
    workspace = _text(payload.get("workspace_id"))
    if workspace and is_internal_mod_id(workspace):
        workspace = ""
    url = _text(payload.get("url") or payload.get("source_url"))
    if _steam_url_embeds_internal(url):
        url = ""
    external = _text(payload.get("external_id"))
    pub = _text(payload.get("published_file_id"))
    internal = read_internal_id(payload)
    platform = normalize_platform_if_known(
        _text(payload.get("source_type") or payload.get("platform"))
    )
    if not any((workspace, url, external, pub, internal)):
        return ""

    matches: list[str] = []
    try:
        rows = db.iter_mod_backup_rows()
    except Exception:  # noqa: BLE001
        return ""
    for row in rows:
        mid = _text(row.get("mod_id"))
        if not mid.isdigit():
            continue
        prev = _text(row.get("last_known_path"))
        if not prev:
            continue
        try:
            old = Path(prev).resolve()
        except OSError:
            old = Path(prev)
        if old == folder:
            return mid
        try:
            if Path(prev).is_dir():
                continue
        except OSError:
            pass
        try:
            info = db.get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            continue
        row_ws = _text(getattr(info, "workspace_id", ""))
        row_ext = _text(getattr(info, "external_id", ""))
        row_url = _text(getattr(info, "source_url", ""))
        row_plat = normalize_platform(getattr(info, "platform", "") or "")
        row_uuid = ""
        try:
            bak = db.get_mod_backup_row(mid) or {}
            row_uuid = _text(bak.get("internal_id"))
        except Exception:  # noqa: BLE001
            row_uuid = ""
        ok = False
        if workspace and row_ws == workspace:
            ok = True
        elif internal and (row_uuid == internal or mid == internal):
            ok = True
        elif url and row_url == url:
            ok = True
        elif platform and external and row_plat == platform and row_ext == external:
            ok = True
        elif pub and not is_internal_mod_id(pub) and _legacy_token_matches_row(info, pub):
            ok = True
        if ok:
            matches.append(mid)
    if len(matches) == 1:
        return matches[0]
    return ""


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

    Priority: workspace_id → UUID internal_id → source_url → legacy fields → path.
    Never treats a bare digit as a new Steam identity.
    """
    payload = dict(data or {})
    db = _bind_database(db)
    if db is None:
        return ""

    platform = normalize_platform_if_known(
        _text(payload.get("source_type") or payload.get("platform"))
    )
    workspace = _text(payload.get("workspace_id"))
    if workspace and is_internal_mod_id(workspace):
        workspace = ""
    url = _text(payload.get("url") or payload.get("source_url"))
    external = _text(payload.get("external_id"))
    pub = _text(payload.get("published_file_id"))
    internal = read_internal_id(payload)
    app_id = 0
    try:
        app_id = int(payload.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0

    placeholder = _is_placeholder_folder(payload)
    official = _has_official_identity(payload, platform=platform)
    if placeholder and not official and not workspace:
        # Polluted Internal PK in published_file_id must not bind/create.
        return ""

    # 1. Durable user identity: workspace_id + platform / app context.
    if workspace:
        found = _lookup_workspace(db, workspace, platform=platform, app_id=app_id)
        if found:
            return found

    # 2. Sidecar UUID — recover the DB entity only. Never mint Workspace ID.
    if internal:
        try:
            found = db.find_mod_by_internal_id(internal)
            if found is not None:
                return str(found)
        except Exception:  # noqa: BLE001
            pass

    # 3. Validated source URL (reject Internal ID embedded as Steam workshop id).
    if url and not _steam_url_embeds_internal(url, internal_pk=pub):
        try:
            from services.importers.duplicate_check import find_mod_by_source_url_relaxed

            info = find_mod_by_source_url_relaxed(
                db,
                url,
                platform=platform or "",
                app_id=app_id,
            )
            if info is not None and str(info.mod_id).isdigit():
                return str(info.mod_id)
        except Exception:  # noqa: BLE001
            pass

    # 4. Legacy published_file_id / external_id — existing entity only.
    if pub.isdigit() and is_internal_mod_id(pub):
        try:
            if db.get_mod(pub) is not None and not (placeholder and not official):
                return pub
        except Exception:  # noqa: BLE001
            pass
    elif pub.isdigit() and not is_internal_mod_id(pub):
        # Legacy field may equal Workspace ID of an existing row. Never assume Steam.
        found = _lookup_workspace(db, pub, platform=platform, app_id=app_id)
        if found:
            return found
        try:
            info = db.get_mod_display_info(pub)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None:
            row_plat = normalize_platform(info.platform)
            if platform == PLATFORM_STEAM or row_plat == PLATFORM_STEAM:
                if platform in ("", PLATFORM_STEAM) and row_plat in ("", PLATFORM_STEAM):
                    return str(info.mod_id)
            # Non-Steam existing PK coincidence: only bind if platform matches.
            if platform and row_plat == platform:
                return str(info.mod_id)

    if platform and external:
        if not is_provisional_external_id(external) and not is_modio_external_id_pollution(
            external, mod_id=pub or external
        ):
            if not is_internal_mod_id(external):
                try:
                    info = db.find_mod_by_external(platform, external, app_id=app_id)
                    if info is not None and str(info.mod_id).isdigit():
                        return str(info.mod_id)
                    if app_id > 0:
                        info = db.find_mod_by_external(platform, external, app_id=0)
                        if info is not None and str(info.mod_id).isdigit():
                            return str(info.mod_id)
                except Exception:  # noqa: BLE001
                    pass

    # 5. Filesystem path — auxiliary only (never sole identity).
    try:
        managed = _text(payload.get("_managed_path"))
        if managed:
            found = db.find_mod_by_last_known_path(managed)
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass

    relocated = _lookup_relocated_folder(db, payload)
    if relocated:
        return relocated

    # Backup snapshots for portable identity (rename / migrate).
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
            if workspace and _text(meta.get("workspace_id")) == workspace:
                if not platform or normalize_platform(
                    _text(meta.get("source_type") or meta.get("platform"))
                ) in ("", platform):
                    return mid
            if internal and read_internal_id(meta) == internal:
                return mid
            if url and _text(meta.get("url") or meta.get("source_url")) == url:
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
            _ = backup_root(mid)
    except Exception:  # noqa: BLE001
        logger.debug("backup identity scan failed", exc_info=True)

    return ""


def ensure_mod_identity(
    managed_path: str | Path,
    data: dict[str, Any] | None = None,
    db: Any | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """
    Resolve a stable Mod identity for *managed_path*. Never allocates.

    Unresolved folders stay ``identity_status=unresolved``.
    """
    root = Path(managed_path)
    payload = dict(data if data is not None else (read_info_metadata_dict(root) or {}))
    payload.setdefault("_folder_name", root.name)
    payload.setdefault("_managed_path", str(root.resolve()) if root.exists() else str(root))
    payload, changed = ensure_internal_id(payload)

    existing = resolve_existing_mod_id(payload, db=db)
    pub = _text(payload.get("published_file_id"))
    plat = normalize_platform_if_known(
        _text(payload.get("source_type") or payload.get("platform"))
    )

    if existing:
        mod_id = existing
        if is_internal_mod_id(mod_id):
            if pub and (is_internal_mod_id(pub) or pub == mod_id):
                payload.pop("published_file_id", None)
                changed = True
        elif plat == PLATFORM_STEAM or (not plat and not is_internal_mod_id(mod_id)):
            if not pub.isdigit() or is_internal_mod_id(pub):
                from services.identity_service import sidecar_published_file_id

                steam_pub = sidecar_published_file_id(
                    mod_id=mod_id, platform=plat or PLATFORM_STEAM, external_id=mod_id
                )
                if steam_pub:
                    payload["published_file_id"] = steam_pub
                    changed = True
        payload["identity_status"] = "complete"
        return mod_id, payload, changed

    if pub and is_internal_mod_id(pub):
        payload.pop("published_file_id", None)
        changed = True

    payload["identity_status"] = "unresolved"
    logger.info("identity unresolved for %s — will not allocate", root)
    return "", payload, changed
