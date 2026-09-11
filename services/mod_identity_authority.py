"""Canonical Mod Identity Authority — single write entry for identity fields.

ID contract (Frozen Minimal Model):

* ``ResolvedIdentity.internal_id`` — durable business Entity Identity (TEXT).
* ``ResolvedIdentity.mod_id`` — SQLite PK / FK handle, not business identity.
* Workspace ID is derived from platform ``external_id`` (Steam/Nexus) or
  generated uniquely (GitHub/mod.io/其它). Never ``workspace_id = internal_id``.
  Never ``workspace_id = mod_id``.
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
    find_mod_by_source_url,
    normalize_source_url,
)

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIdentity:
    mod_id: str = ""  # SQLite PK (FK handle) — not Frozen entity identity
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    app_id: int = 0
    workspace_id: str = ""
    created: bool = False
    reused: bool = False
    notes: list[str] = field(default_factory=list)
    internal_id: str = ""  # durable business Entity Identity (TEXT)


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
    workspace_id: str = "",
) -> ResolvedIdentity:
    """Locate an existing Mod without creating one.

    Registration rematch: ``(platform, app_id, workspace_id)`` with ``app_id > 0``.
    Entity bind elsewhere uses ``internal_id`` only.
    """
    from services.mod_identity import extract_workspace_id

    out = ResolvedIdentity(
        platform=normalize_platform(platform),
        external_id=sanitize_platform_external_id(
            platform, external_id, mod_id=mod_id
        ),
        source_url=normalize_source_url(source_url),
        app_id=int(app_id or 0),
    )
    if out.app_id <= 0 and out.platform != PLATFORM_STEAM:
        return out

    if str(mod_id or "").strip().isdigit():
        info = db.get_mod_display_info(str(mod_id).strip())
        if info is not None:
            out.mod_id = str(info.mod_id)
            out.reused = True
            out.notes.append("matched_mod_id")
            return out

    ws = extract_workspace_id(
        workspace_id=str(workspace_id or ""),
        external_id=out.external_id,
        source_url=out.source_url,
        legacy_token=str(workshop_id or ""),
    )
    out.workspace_id = ws

    # Steam with app_id: registration requires game scope when available.
    aid = out.app_id
    if out.platform == PLATFORM_STEAM and aid <= 0 and ws:
        # Steam Workshop IDs are globally unique; still require registration API
        # with app_id when known. Without app_id, fall through to URL only.
        pass

    if ws and aid > 0 and out.platform:
        hit = db.find_mod_for_registration(out.platform, aid, ws)
        if hit is not None and int(getattr(hit, "app_id", 0) or 0) == aid:
            from services.importers.duplicate_check import nexus_source_urls_compatible

            if out.platform == PLATFORM_NEXUS and not nexus_source_urls_compatible(
                str(getattr(hit, "source_url", "") or ""),
                out.source_url,
            ):
                pass
            else:
                out.mod_id = str(hit.mod_id)
                out.reused = True
                out.notes.append("matched_registration")
                return out

    dup = find_duplicate_mod(
        db,
        platform=out.platform,
        external_id=out.external_id,
        source_url=out.source_url,
        workshop_id=str(workshop_id or ""),
        app_id=out.app_id,
        workspace_id=ws,
    )
    if dup is not None:
        row_app = int(getattr(dup, "app_id", 0) or 0)
        if row_app <= 0 and out.platform != PLATFORM_STEAM:
            return out
        if out.app_id > 0 and row_app > 0 and row_app != out.app_id:
            return out
        from services.importers.duplicate_check import nexus_source_urls_compatible

        if out.platform == PLATFORM_NEXUS and not nexus_source_urls_compatible(
            str(getattr(dup, "source_url", "") or ""),
            out.source_url,
        ):
            return out
        out.mod_id = str(dup.mod_id)
        out.reused = True
        out.notes.append("matched_duplicate_gate")
        return out

    if out.source_url and out.app_id > 0:
        hit = find_mod_by_source_url(
            db,
            out.source_url,
            platform=out.platform,
            app_id=out.app_id,
        )
        if hit is not None:
            row_app = int(getattr(hit, "app_id", 0) or 0)
            if row_app == out.app_id:
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

    Never reuse a row whose ``app_id`` differs from the requested game scope
    (including dirty ``app_id=0`` Nexus rows with a colliding Mod ID).
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
        info = db.get_mod_display_info(existing.mod_id)
        row_app = int(getattr(info, "app_id", 0) or 0) if info else 0
        want_app = int(app_id or 0)
        from services.importers.duplicate_check import nexus_source_urls_compatible

        plat_norm = normalize_platform(platform)
        url_in = normalize_source_url(source_url)
        row_url = str(getattr(info, "source_url", "") or "") if info else ""
        cross_game = want_app > 0 and row_app != want_app
        nexus_slug_conflict = (
            plat_norm == PLATFORM_NEXUS
            and url_in
            and not nexus_source_urls_compatible(row_url, url_in)
        )
        dirty_zero_app = (
            row_app <= 0 and want_app > 0 and plat_norm != PLATFORM_STEAM
        )
        if cross_game or nexus_slug_conflict or dirty_zero_app:
            # Cross-game / dirty app_id=0 / Nexus URL game mismatch — new entity.
            existing = ResolvedIdentity(
                platform=plat_norm,
                external_id=sanitize_platform_external_id(platform, external_id),
                source_url=url_in,
                app_id=want_app,
            )
        else:
            # Same-scope reuse: refresh import metadata onto the existing entity.
            # Never rewrite app_id here — scope was already verified equal.
            if info is not None and (title or url_in):
                try:
                    db.update_mod_platform_info(
                        existing.mod_id,
                        source_url=url_in or None,
                        title=str(title or "").strip() or None,
                        touch_updated_at=True,
                        updated_at_reason="user_import",
                    )
                    refreshed = db.get_mod_display_info(existing.mod_id)
                    if refreshed is not None:
                        existing.workspace_id = str(refreshed.workspace_id or "")
                        existing.source_url = str(refreshed.source_url or url_in)
                        existing.external_id = str(
                            refreshed.external_id or existing.external_id
                        )
                        existing.app_id = int(refreshed.app_id or want_app or 0)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "import metadata refresh failed for %s",
                        existing.mod_id,
                        exc_info=True,
                    )
            existing.reused = True
            existing.notes.append("reused_same_scope_refreshed")
            return existing

    plat = normalize_platform(platform)
    ext = sanitize_platform_external_id(plat, external_id)
    url = normalize_source_url(source_url)
    wid = str(workshop_id or "").strip()

    if plat == PLATFORM_STEAM and (wid.isdigit() or (ext.isdigit() and not is_internal_mod_id(ext))):
        steam_id = wid if wid.isdigit() else ext
        from core.models import ModMetadata

        # IdentityService create path — allow_insert only here.
        # Catalog refresh must never reach this branch.
        db.upsert_mod(
            ModMetadata(
                published_file_id=steam_id,
                title=title or f"Unknown_Mod_{steam_id}",
                app_id=int(app_id or 0),
                url=url,
            ),
            allow_insert=True,
        )
        # Historical Steam scheme: Internal PK digits may equal Workshop ID.
        created_mid = (
            db.resolve_steam_entity_mod_id(
                steam_id, app_id=int(app_id or 0)
            )
            or steam_id
        )
        if url:
            db.update_mod_platform_info(
                created_mid,
                platform=PLATFORM_STEAM,
                source_url=url,
                external_id=steam_id,
            )
        log_identity_mutation(
            db,
            mod_id=created_mid,
            field_name="mod_id",
            old_value="",
            new_value=created_mid,
            source="identity_authority",
            reason="create_steam",
        )
        return ResolvedIdentity(
            mod_id=created_mid,
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
