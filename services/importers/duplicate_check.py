"""Pre-write import duplicate detection (workshop id / external id / source_url)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from core.db_manager import DatabaseManager, ModDisplayInfo
from core.mod_platform import (
    PLATFORM_NEXUS,
    normalize_platform,
)
from services.importers.importer_base import ImportResult

logger = logging.getLogger(__name__)

DUPLICATE_STATUS = "duplicate"
DUPLICATE_MESSAGE = "该Mod已经存在"

# Diagnostics-only: last find_duplicate_mod hit (for check_import_duplicate DEBUG).
_LAST_DUP_DIAG: dict[str, str] = {}


def nexus_url_game_slug(url: str) -> str:
    """Nexus game slug from ``…/<game>/mods/<id>`` (empty when absent)."""
    text = str(url or "").strip()
    if not text or "nexusmods.com" not in text.lower():
        return ""
    try:
        parts = [p for p in urlparse(text).path.split("/") if p]
        if "mods" in parts:
            idx = parts.index("mods")
            if idx > 0:
                return parts[idx - 1].lower()
    except Exception:
        return ""
    return ""


def nexus_source_urls_compatible(existing_url: str, incoming_url: str) -> bool:
    """False when both URLs name different Nexus games (cross-game collision)."""
    a = nexus_url_game_slug(existing_url)
    b = nexus_url_game_slug(incoming_url)
    if a and b and a != b:
        return False
    return True


def normalize_source_url(url: str) -> str:
    """Strip whitespace / fragment; drop non-identity query params.

    Steam Workshop identity lives in ``?id=`` — keep that query.
    Nexus / GitHub / others: strip ``?tab=`` and similar tracking params.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlparse(text)
        host = (parts.netloc or "").lower()
        path = parts.path or ""
        if "steamcommunity.com" in host and "filedetails" in path:
            return urlunparse(parts._replace(fragment="")).rstrip("/")
        return urlunparse(parts._replace(query="", fragment="")).rstrip("/")
    except Exception:
        return text.rstrip("/")


def find_mod_by_source_url(
    db: DatabaseManager,
    source_url: str,
    *,
    platform: str = "",
    app_id: int = 0,
) -> ModDisplayInfo | None:
    """
    Locate an existing Mod by normalized ``source_url`` for live import duplicate checks.

    Strictly scoped by ``(platform, app_id)`` when *platform* is set — same contract
    as :func:`find_duplicate_mod` (``platform + app_id + identity``).

    DO NOT add ``app_id=0`` / cross-app fallbacks here. Historical dirty-row recovery
    belongs in identity-repair tooling, never in the real-time import path
    (regression: 6c64d51).
    """
    target = normalize_source_url(source_url)
    if not target:
        return None
    plat = normalize_platform(platform) if platform else ""
    aid = int(app_id or 0)

    def _scan_rows(rows) -> ModDisplayInfo | None:
        for row in rows:
            if normalize_source_url(str(row["source_url"] or "")) == target:
                return db.get_mod_display_info(row["mod_id"])
        return None

    with db._lock:
        if plat:
            rows = db._conn.execute(
                """
                SELECT mod_id, platform, source_url, external_id, app_id
                FROM mods
                WHERE platform = ?
                  AND app_id = ?
                  AND source_url IS NOT NULL
                  AND TRIM(source_url) != ''
                """,
                (plat, aid),
            ).fetchall()
        else:
            rows = db._conn.execute(
                """
                SELECT mod_id, platform, source_url, external_id, app_id
                FROM mods
                WHERE source_url IS NOT NULL
                  AND TRIM(source_url) != ''
                """
            ).fetchall()
    return _scan_rows(rows)


def find_duplicate_mod(
    db: DatabaseManager,
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
    workshop_id: str = "",
    app_id: int = 0,
    folder_path: str = "",
    workspace_id: str = "",
) -> ModDisplayInfo | None:
    """
    Return an existing Mod for Sync/Import registration.

    Registration uniqueness::

        (platform, app_id, workspace_id) with app_id > 0

    ``external_id`` / ``workshop_id`` are temporary parse aliases for
    ``workspace_id`` only — never unscoped identity keys.
    *folder_path* is diagnostics-only.
    """
    _LAST_DUP_DIAG.clear()
    plat = normalize_platform(platform)
    aid = int(app_id or 0)
    folder = str(folder_path or "").strip()
    from services.mod_identity import extract_workspace_id

    ws = extract_workspace_id(
        workspace_id=str(workspace_id or ""),
        external_id=str(external_id or ""),
        source_url=str(source_url or ""),
        legacy_token=str(workshop_id or ""),
    )
    url = str(source_url or "").strip()

    if ws and aid > 0 and plat:
        existing = db.find_mod_for_registration(plat, aid, ws)
        if existing is not None:
            row_app = int(getattr(existing, "app_id", 0) or 0)
            if row_app == aid:
                if plat == PLATFORM_NEXUS and not nexus_source_urls_compatible(
                    str(getattr(existing, "source_url", "") or ""),
                    url,
                ):
                    existing = None
                else:
                    _log_duplicate_match(
                        matched_by="registration_workspace",
                        matched=existing,
                        input_external_id=ws,
                        input_source_url=source_url,
                        input_folder_path=folder,
                    )
                    return existing

    if url and aid > 0:
        existing = find_mod_by_source_url(db, url, platform=plat, app_id=aid)
        if existing is not None:
            if plat == PLATFORM_NEXUS and not nexus_source_urls_compatible(
                str(getattr(existing, "source_url", "") or ""),
                url,
            ):
                existing = None
            else:
                _log_duplicate_match(
                    matched_by="source_url",
                    matched=existing,
                    input_external_id=ws or str(external_id or ""),
                    input_source_url=url,
                    input_folder_path=folder,
                )
                return existing
    return None


def _matched_title(matched: ModDisplayInfo) -> str:
    return str(
        matched.display_name or matched.steam_name or getattr(matched, "title", "") or ""
    )


def _log_duplicate_match(
    *,
    matched_by: str,
    matched: ModDisplayInfo,
    input_external_id: str = "",
    input_source_url: str = "",
    input_folder_path: str = "",
) -> None:
    """DEBUG-only diagnostics — must not affect callers."""
    by = (
        matched_by
        if matched_by in {"external_id", "source_url", "workshop_id"}
        else "unknown"
    )
    _LAST_DUP_DIAG.clear()
    _LAST_DUP_DIAG.update(
        matched_by=by,
        matched_mod_id=str(matched.mod_id or ""),
        matched_title=_matched_title(matched),
        matched_platform=str(getattr(matched, "platform", "") or ""),
        matched_app_id=str(int(getattr(matched, "app_id", 0) or 0)),
        matched_external_id=str(matched.external_id or ""),
        matched_source_url=str(matched.source_url or ""),
        input_external_id=str(input_external_id or ""),
        input_source_url=str(input_source_url or ""),
        input_folder_path=str(input_folder_path or ""),
    )
    logger.debug(
        "import duplicate match "
        "matched_by=%s matched_mod_id=%s matched_title=%s "
        "matched_platform=%s matched_app_id=%s "
        "matched_external_id=%s matched_source_url=%s "
        "input_external_id=%s input_source_url=%s input_folder_path=%s",
        _LAST_DUP_DIAG["matched_by"],
        _LAST_DUP_DIAG["matched_mod_id"],
        _LAST_DUP_DIAG["matched_title"],
        _LAST_DUP_DIAG["matched_platform"],
        _LAST_DUP_DIAG["matched_app_id"],
        _LAST_DUP_DIAG["matched_external_id"],
        _LAST_DUP_DIAG["matched_source_url"],
        _LAST_DUP_DIAG["input_external_id"],
        _LAST_DUP_DIAG["input_source_url"],
        _LAST_DUP_DIAG["input_folder_path"],
    )


def duplicate_import_result(
    existing: ModDisplayInfo,
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
) -> ImportResult:
    """Build a skip result — no overwrite, no new folder."""
    return ImportResult(
        success=False,
        status=DUPLICATE_STATUS,
        error=DUPLICATE_MESSAGE,
        platform=normalize_platform(platform),
        mod_id=str(existing.mod_id or ""),
        external_id=str(external_id or existing.external_id or ""),
        source_url=str(source_url or existing.source_url or ""),
        title=str(existing.display_name or existing.steam_name or ""),
        display=existing,
    )


def check_import_duplicate(
    db: DatabaseManager,
    *,
    platform: str,
    external_id: str = "",
    source_url: str = "",
    workshop_id: str = "",
    app_id: int = 0,
    folder_path: str = "",
    title: str = "",
) -> ImportResult | None:
    """Return a duplicate :class:`ImportResult` when the Mod already exists.

    Same-scope hits re-verify source identity (app_id + Nexus URL game slug)
    and refresh title/source_url before reporting duplicate. Cross-game /
    Nexus URL slug conflicts are **not** duplicates — return ``None`` so
    import creates a distinct entity (never rewrite the foreign game's row).

    This gate never allocates a new entity.
    """
    existing = find_duplicate_mod(
        db,
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        workshop_id=workshop_id,
        app_id=app_id,
        folder_path=folder_path,
    )
    if existing is None:
        _LAST_DUP_DIAG.clear()
        return None

    plat = normalize_platform(platform)
    want_app = int(app_id or 0)
    row_app = int(getattr(existing, "app_id", 0) or 0)
    url_in = normalize_source_url(source_url)
    row_url = str(getattr(existing, "source_url", "") or "")

    # Refuse reuse without allocating — import must create a distinct entity.
    if want_app > 0 and row_app > 0 and row_app != want_app:
        _LAST_DUP_DIAG.clear()
        return None
    if row_app <= 0 and want_app > 0 and plat != "steam":
        _LAST_DUP_DIAG.clear()
        return None
    if plat == PLATFORM_NEXUS and not nexus_source_urls_compatible(row_url, url_in):
        _LAST_DUP_DIAG.clear()
        return None

    # Same-scope reuse: refresh import metadata onto the existing entity.
    name = str(title or "").strip()
    if name or url_in:
        try:
            db.update_mod_platform_info(
                existing.mod_id,
                source_url=url_in or None,
                title=name or None,
                touch_updated_at=True,
                updated_at_reason="user_import",
            )
            refreshed = db.get_mod_display_info(existing.mod_id)
            if refreshed is not None:
                existing = refreshed
        except Exception:  # noqa: BLE001
            logger.debug(
                "check_import_duplicate metadata refresh failed for %s",
                existing.mod_id,
                exc_info=True,
            )

    # find_duplicate_mod already filled _LAST_DUP_DIAG + DEBUG; echo at check site.
    logger.debug(
        "check_import_duplicate returning duplicate "
        "matched_by=%s matched_mod_id=%s matched_title=%s "
        "matched_platform=%s matched_app_id=%s "
        "matched_external_id=%s matched_source_url=%s "
        "input_external_id=%s input_source_url=%s input_folder_path=%s",
        _LAST_DUP_DIAG.get("matched_by", "unknown"),
        existing.mod_id,
        _matched_title(existing),
        getattr(existing, "platform", "") or "",
        int(getattr(existing, "app_id", 0) or 0),
        existing.external_id or "",
        existing.source_url or "",
        external_id or workshop_id or "",
        source_url or "",
        folder_path or "",
    )
    return duplicate_import_result(
        existing,
        platform=platform,
        external_id=external_id or workshop_id,
        source_url=source_url,
    )
