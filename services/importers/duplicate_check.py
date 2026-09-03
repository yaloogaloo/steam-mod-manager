"""Pre-write import duplicate detection (workshop id / external id / source_url)."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from core.db_manager import DatabaseManager, ModDisplayInfo
from core.mod_platform import (
    is_internal_mod_id,
    is_modio_external_id_pollution,
    is_provisional_external_id,
    normalize_platform,
)
from services.importers.importer_base import ImportResult

DUPLICATE_STATUS = "duplicate"
DUPLICATE_MESSAGE = "该Mod已经存在"


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
    """Locate an existing Mod by normalized ``source_url`` (no schema change)."""
    return find_mod_by_source_url_relaxed(
        db,
        source_url,
        platform=platform,
        app_id=app_id,
        relax_app_id=False,
    )


def find_mod_by_source_url_relaxed(
    db: DatabaseManager,
    source_url: str,
    *,
    platform: str = "",
    app_id: int = 0,
    relax_app_id: bool = True,
) -> ModDisplayInfo | None:
    """
    Locate an existing Mod by normalized ``source_url``.

    When *relax_app_id* is True, also matches rows with ``app_id=0`` or any
    ``app_id`` under the same platform (identity recovery after bad backfill).
    """
    target = normalize_source_url(source_url)
    if not target:
        return None
    plat = normalize_platform(platform) if platform else ""

    def _scan_rows(rows) -> ModDisplayInfo | None:
        for row in rows:
            if normalize_source_url(str(row["source_url"] or "")) == target:
                return db.get_mod_display_info(row["mod_id"])
        return None

    with db._lock:
        if plat:
            aid = int(app_id or 0)
            if aid > 0:
                hit = _scan_rows(
                    db._conn.execute(
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
                )
                if hit is not None:
                    return hit
            if relax_app_id and aid > 0:
                hit = _scan_rows(
                    db._conn.execute(
                        """
                        SELECT mod_id, platform, source_url, external_id, app_id
                        FROM mods
                        WHERE platform = ?
                          AND (app_id = 0 OR app_id IS NULL)
                          AND source_url IS NOT NULL
                          AND TRIM(source_url) != ''
                        """,
                        (plat,),
                    ).fetchall()
                )
                if hit is not None:
                    return hit
            rows = db._conn.execute(
                """
                SELECT mod_id, platform, source_url, external_id, app_id
                FROM mods
                WHERE platform = ?
                  AND source_url IS NOT NULL
                  AND TRIM(source_url) != ''
                """,
                (plat,),
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
) -> ModDisplayInfo | None:
    """
    Return an existing Mod when workshop / external id / source_url already exists.

    Platform IDs (Nexus / mod.io / …) are scoped by ``app_id`` (game context).
    Checks happen before materialize / DB write. Empty URL is ignored.
    """
    plat = normalize_platform(platform)
    aid = int(app_id or 0)
    wid = str(workshop_id or "").strip()
    if plat == "steam" and wid.isdigit() and not is_internal_mod_id(wid):
        existing = db.get_mod_display_info(wid)
        if existing is not None:
            return existing

    ext = str(external_id or "").strip()
    if ext and not is_provisional_external_id(ext):
        if not (ext.isdigit() and is_internal_mod_id(ext)):
            if not is_modio_external_id_pollution(ext):
                existing = db.find_mod_by_external(plat, ext, app_id=aid)
                if existing is not None:
                    return existing
                if aid > 0:
                    existing = db.find_mod_by_external(plat, ext, app_id=0)
                    if existing is not None:
                        return existing
                if aid == 0:
                    with db._lock:
                        row = db._conn.execute(
                            """
                            SELECT mod_id FROM mods
                            WHERE platform = ? AND external_id = ?
                            LIMIT 1
                            """,
                            (plat, ext),
                        ).fetchone()
                    if row is not None:
                        info = db.get_mod_display_info(row["mod_id"])
                        if info is not None:
                            return info

    url = str(source_url or "").strip()
    if url:
        existing = find_mod_by_source_url(db, url, platform=plat, app_id=aid)
        if existing is not None:
            return existing
    return None


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
) -> ImportResult | None:
    """Return a duplicate :class:`ImportResult` when the Mod already exists."""
    existing = find_duplicate_mod(
        db,
        platform=platform,
        external_id=external_id,
        source_url=source_url,
        workshop_id=workshop_id,
        app_id=app_id,
    )
    if existing is None:
        return None
    return duplicate_import_result(
        existing,
        platform=platform,
        external_id=external_id or workshop_id,
        source_url=source_url,
    )
