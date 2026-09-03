"""Canonical Mod Identity Authority — single write entry for identity fields.

ID contract: Internal ID (``ResolvedIdentity.mod_id``) is database-only.
Workspace ID is derived from platform ``external_id`` (Steam/Nexus) or
generated uniquely (GitHub/mod.io/其它). Never ``workspace_id = internal_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    generate_unique_workspace_id,
    is_internal_mod_id,
    is_modio_external_id_pollution,
    is_provisional_external_id,
    normalize_platform,
    resolve_workspace_id,
)
from services.importers.duplicate_check import (
    find_duplicate_mod,
    find_mod_by_source_url_relaxed,
    normalize_source_url,
)

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIdentity:
    mod_id: str = ""
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    app_id: int = 0
    workspace_id: str = ""
    created: bool = False
    reused: bool = False
    notes: list[str] = field(default_factory=list)


def sanitize_platform_external_id(
    platform: str,
    external_id: str,
    *,
    mod_id: int | str = "",
) -> str:
    """Return platform external_id or empty — never an internal mod_id."""
    plat = normalize_platform(platform)
    ext = str(external_id or "").strip()
    if not ext:
        return ""
    if is_provisional_external_id(ext):
        return ""
    if is_modio_external_id_pollution(ext, mod_id=mod_id):
        return ""
    if is_internal_mod_id(ext):
        return ""
    if plat == PLATFORM_STEAM and is_internal_mod_id(ext):
        return ""
    return ext


def safe_workspace_id_for_deploy(
    *,
    platform: str = "",
    workspace_id: str = "",
    mod_id: int | str = "",
    source_url: str = "",
    external_id: str = "",
    workshop_id: str = "",
) -> str:
    """
    Workspace id for deploy manifests — never falls back to Internal ID.

    Steam: Workspace ID = Steam Workshop ID (``external_id`` / ``workshop_id``).
    Numeric equality with Internal PK is coincidence of the Steam PK scheme.
    Non-Steam: empty when unresolved (caller must not invent from Internal ID).
    """
    existing = str(workspace_id or "").strip()
    plat = normalize_platform(platform)
    mid = str(mod_id or "").strip()
    if existing:
        if mid and is_internal_mod_id(mid) and existing == mid:
            existing = ""
        else:
            return existing
    steam_workshop = str(workshop_id or "").strip()
    resolved = resolve_workspace_id(
        plat,
        source_url=source_url,
        external_id=sanitize_platform_external_id(plat, external_id, mod_id=mid),
        workshop_id=steam_workshop,
    )
    if resolved and mid and is_internal_mod_id(mid) and resolved == mid:
        return ""
    return resolved


def resolve_mod_identity(
    db,
    *,
    platform: str = "",
    external_id: str = "",
    source_url: str = "",
    workshop_id: str = "",
    app_id: int = 0,
    mod_id: str = "",
) -> ResolvedIdentity:
    """Locate an existing Mod without creating one."""
    out = ResolvedIdentity(
        platform=normalize_platform(platform),
        external_id=sanitize_platform_external_id(
            platform, external_id, mod_id=mod_id
        ),
        source_url=normalize_source_url(source_url),
        app_id=int(app_id or 0),
    )
    if str(mod_id or "").strip().isdigit():
        info = db.get_mod_display_info(str(mod_id).strip())
        if info is not None:
            out.mod_id = str(info.mod_id)
            out.reused = True
            out.notes.append("matched_mod_id")
            return out

    wid = str(workshop_id or "").strip()
    if (
        out.platform == PLATFORM_STEAM
        and wid.isdigit()
        and not is_internal_mod_id(wid)
    ):
        info = db.get_mod_display_info(wid)
        if info is not None:
            out.mod_id = str(info.mod_id)
            out.reused = True
            out.notes.append("matched_workshop_id")
            return out

    ws_key = out.external_id or wid
    if ws_key and not is_internal_mod_id(ws_key):
        try:
            found = db.find_mod_by_workspace_id(
                ws_key, platform=out.platform or None, app_id=out.app_id
            )
            if found:
                out.mod_id = str(found)
                out.reused = True
                out.notes.append("matched_workspace_id")
                return out
        except Exception:  # noqa: BLE001
            logger.debug("workspace_id create-gate lookup failed", exc_info=True)

    dup = find_duplicate_mod(
        db,
        platform=out.platform,
        external_id=out.external_id,
        source_url=out.source_url,
        workshop_id=wid,
        app_id=out.app_id,
    )
    if dup is not None:
        out.mod_id = str(dup.mod_id)
        out.reused = True
        out.notes.append("matched_duplicate_gate")
        return out

    if out.source_url:
        hit = find_mod_by_source_url_relaxed(
            db,
            out.source_url,
            platform=out.platform,
            app_id=out.app_id,
        )
        if hit is not None:
            out.mod_id = str(hit.mod_id)
            out.reused = True
            out.notes.append("matched_source_url")
    return out


def create_mod_identity(
    db,
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
    title: str = "",
    app_id: int = 0,
    game_name: str = "",
    workshop_id: str = "",
) -> ResolvedIdentity:
    """
    Create or reuse a Mod entity via the sole allocation path.

    Steam: Workshop ID → ``external_id`` → ``workspace_id``. The SQLite PK
    historically equals Workshop ID; that is Internal ID storage, not the
    Workspace source.

    Non-Steam: ``register_external_mod`` allocates Internal ID, then binds
    platform identity. Workspace is resolved from ``external_id``/URL or
    generated — never copied from Internal ID.
    """
    existing = resolve_mod_identity(
        db,
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        workshop_id=workshop_id,
        app_id=app_id,
    )
    if existing.mod_id:
        return existing

    plat = normalize_platform(platform)
    ext = sanitize_platform_external_id(plat, external_id)
    url = normalize_source_url(source_url)
    wid = str(workshop_id or "").strip()

    if plat == PLATFORM_STEAM and (wid.isdigit() or (ext.isdigit() and not is_internal_mod_id(ext))):
        steam_id = wid if wid.isdigit() else ext
        from core.models import ModMetadata

        db.upsert_mod(
            ModMetadata(
                published_file_id=steam_id,
                title=title or f"Unknown_Mod_{steam_id}",
                app_id=int(app_id or 0),
                url=url,
            ),
            allow_insert=True,
        )
        if url:
            db.update_mod_platform_info(
                steam_id,
                platform=PLATFORM_STEAM,
                source_url=url,
                external_id=steam_id,
            )
        log_identity_mutation(
            db,
            mod_id=steam_id,
            field_name="mod_id",
            old_value="",
            new_value=steam_id,
            source="identity_authority",
            reason="create_steam",
        )
        return ResolvedIdentity(
            mod_id=steam_id,
            platform=PLATFORM_STEAM,
            external_id=steam_id,
            source_url=url,
            app_id=int(app_id or 0),
            workspace_id=steam_id,
            created=True,
            notes=["created_steam"],
        )

    if not ext and url:
        # Provisional registration needs an external key — use URL slug tail.
        ext = url.rstrip("/").rsplit("/", 1)[-1] or url
    if not ext:
        raise ValueError("external_id or source_url required to create non-Steam mod")

    info = db.register_external_mod(
        platform=plat,
        external_id=ext,
        source_url=url,
        title=title,
        app_id=int(app_id or 0),
        game_name=game_name,
    )
    log_identity_mutation(
        db,
        mod_id=str(info.mod_id),
        field_name="mod_id",
        old_value="",
        new_value=str(info.mod_id),
        source="identity_authority",
        reason="create_external",
    )
    return ResolvedIdentity(
        mod_id=str(info.mod_id),
        platform=plat,
        external_id=str(info.external_id or ext),
        source_url=url or str(info.source_url or ""),
        app_id=int(info.app_id or app_id or 0),
        workspace_id=str(info.workspace_id or ""),
        created=True,
        notes=["created_external"],
    )


def update_platform_identity(
    db,
    mod_id: int | str,
    *,
    platform: str | None = None,
    external_id: str | None = None,
    source_url: str | None = None,
    app_id: int | None = None,
    title: str | None = None,
    description: str | None = None,
    preview_url: str | None = None,
    source: str = "identity_authority",
    reason: str = "update_platform",
) -> Any:
    """Update platform identity with provenance + pollution scrub."""
    mid = str(mod_id).strip()
    before = db.get_mod_display_info(mid)
    clean_ext = None
    if external_id is not None:
        plat = normalize_platform(
            platform
            if platform is not None
            else (before.platform if before else "")
        )
        clean_ext = sanitize_platform_external_id(plat, external_id, mod_id=mid)
    info = db.update_mod_platform_info(
        mid,
        platform=platform,
        external_id=clean_ext if external_id is not None else None,
        source_url=source_url,
        app_id=app_id,
        title=title,
        description=description,
        preview_url=preview_url,
    )
    if before is not None:
        pairs = (
            ("platform", before.platform, info.platform),
            ("external_id", before.external_id, info.external_id),
            ("source_url", before.source_url, info.source_url),
            ("app_id", str(before.app_id), str(info.app_id)),
            ("workspace_id", before.workspace_id, info.workspace_id),
        )
        for field_name, old, new in pairs:
            if str(old or "") != str(new or ""):
                log_identity_mutation(
                    db,
                    mod_id=mid,
                    field_name=field_name,
                    old_value=str(old or ""),
                    new_value=str(new or ""),
                    source=source,
                    reason=reason,
                )
    return info


def log_identity_mutation(
    db,
    *,
    mod_id: str,
    field_name: str,
    old_value: str,
    new_value: str,
    source: str,
    reason: str,
    commit: bool = True,
) -> None:
    """Append one identity provenance row (best-effort)."""
    try:
        db.append_identity_audit_log(
            mod_id=mod_id,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            source=source,
            reason=reason,
            commit=commit,
        )
    except Exception:  # noqa: BLE001
        logger.debug("identity audit log failed", exc_info=True)


def ensure_non_polluted_workspace(db, mod_id: int | str) -> str:
    """Clear workspace_id that equals Internal ID; never regenerate from Internal ID."""
    mid = str(mod_id).strip()
    info = db.get_mod_display_info(mid)
    if info is None:
        return ""
    plat = normalize_platform(info.platform)
    ws = str(info.workspace_id or "").strip()
    if not (is_internal_mod_id(mid) and ws == mid):
        return ws
    taken = set()
    try:
        with db._lock:
            rows = db._conn.execute(
                "SELECT workspace_id FROM mods "
                "WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) != ''"
            ).fetchall()
        taken = {str(r["workspace_id"] or "").strip() for r in rows}
    except Exception:  # noqa: BLE001
        pass
    new_ws = resolve_workspace_id(
        plat,
        source_url=info.source_url or "",
        external_id=sanitize_platform_external_id(
            plat, info.external_id or "", mod_id=mid
        ),
    )
    if not new_ws and plat not in (PLATFORM_STEAM, PLATFORM_NEXUS):
        new_ws = generate_unique_workspace_id(taken)
    db.update_mod_identity_fields(mid, workspace_id=new_ws)
    log_identity_mutation(
        db,
        mod_id=mid,
        field_name="workspace_id",
        old_value=ws,
        new_value=new_ws,
        source="identity_authority",
        reason="scrub_internal_workspace",
    )
    return new_ws
