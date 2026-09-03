"""Nexus offline HTML metadata parser — read-only, no I/O side-effects.

Trigger boundary (enforced by architecture):
  ALLOWED  : called from attach_nexus_offline_page() after HTML has been saved.
  FORBIDDEN: called from refresh_mod(), reconcile_local_state(),
             sync_after_metadata_change(), show_mod(), or any Refresh pipeline.

Public API
----------
parse_nexus_offline_html(index_html_path) -> NexusOfflineCandidates
    Pure parser. Returns candidates; never writes files or touches the DB.

apply_nexus_offline_candidates(mod_id, managed_path, candidates, ...)
    Merge helper. Fills only missing fields. Never overwrites existing values.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candidate dataclass — pure data, no behaviour
# ---------------------------------------------------------------------------

@dataclass
class NexusOfflineCandidates:
    """Metadata extracted from a Nexus offline HTML page.

    All values are best-effort; empty string / None means not found.
    """
    title: str = ""
    source_url: str = ""
    external_id: str = ""          # Nexus numeric Mod ID as string
    cover_asset_path: Path | None = None  # absolute path inside offline/assets/
    description: str = ""
    confidence: dict[str, str] = field(default_factory=dict)

    def any_useful(self) -> bool:
        return bool(
            self.title
            or self.source_url
            or self.external_id
            or self.cover_asset_path
        )


# ---------------------------------------------------------------------------
# Internal extraction helpers (pure functions)
# ---------------------------------------------------------------------------

_NEXUS_MOD_URL_RE = re.compile(
    r"https?://(?:www\.)?nexusmods\.com/[^/]+/mods/(\d+)",
    re.IGNORECASE,
)
_NEXUS_MOD_URL_NO_GAME_RE = re.compile(
    r"https?://(?:www\.)?nexusmods\.com/mods/(\d+)/?(?:[?#]|$)",
    re.IGNORECASE,
)
_LOGIN_TITLE_RE = re.compile(r"^\s*please\s+log\s+in\s*$", re.IGNORECASE)
_EMPTY_MOD_FOLDER_RE = re.compile(r"^Empty Mod [0-9a-f]{8}$", re.IGNORECASE)


def is_valid_nexus_mod_id(value: str = "") -> bool:
    """True when *value* is a numeric Nexus Mod ID."""
    return str(value or "").strip().isdigit()


def _is_local_nexus_external_id(ext: str) -> bool:
    return str(ext or "").strip().startswith("local/")


def _is_placeholder_nexus_external_id(
    ext: str,
    *,
    managed_path: Path | None = None,
) -> bool:
    """True when *ext* is empty or a system placeholder — not a real Nexus Mod ID."""
    text = str(ext or "").strip()
    if not text:
        return True
    if is_valid_nexus_mod_id(text):
        return False
    if _is_local_nexus_external_id(text):
        return True
    if _EMPTY_MOD_FOLDER_RE.match(text):
        return True
    if managed_path is not None:
        folder = Path(managed_path).name.strip()
        if folder and text.casefold() == folder.casefold():
            return True
    return True


def _is_placeholder_nexus_source_url(
    url: str,
    *,
    current_external_id: str = "",
) -> bool:
    """True when *url* is empty or folder-derived — not a canonical Nexus mod URL."""
    cur = _strip_query(str(url or "").strip())
    if not cur:
        return True
    if "nexusmods.com" not in cur.lower() or "/mods/" not in cur.lower():
        return False
    if _extract_mod_id_from_url(cur):
        return False
    ext = str(current_external_id or "").strip()
    if ext and _is_placeholder_nexus_external_id(ext):
        return True
    parts = [p for p in urlparse(cur).path.split("/") if p]
    if "mods" in parts:
        idx = parts.index("mods")
        if idx + 1 < len(parts) and not parts[idx + 1].split("?")[0].isdigit():
            return True
    return False


def _extract_mod_id_from_url(url: str) -> str:
    m = _NEXUS_MOD_URL_RE.search(str(url or "").strip())
    if m:
        return m.group(1)
    m = _NEXUS_MOD_URL_NO_GAME_RE.search(str(url or "").strip())
    if m:
        return m.group(1)
    return ""


def _is_import_default_title(current: str, managed_path: Path) -> bool:
    """True when the DB title is just the materialized folder name (import default)."""
    folder = Path(managed_path).name.strip()
    text = str(current or "").strip()
    return bool(folder and text and text.casefold() == folder.casefold())


def _should_fill_nexus_title(
    current: str,
    *,
    mod_id: str,
    managed_path: Path,
    source_url_will_fill: bool = False,
    external_id_will_fill: bool = False,
) -> bool:
    from core.models import is_unknown_mod_title

    if is_unknown_mod_title(current, published_file_id=mod_id):
        return True
    if source_url_will_fill or external_id_will_fill:
        if _is_import_default_title(current, managed_path):
            return True
        if _EMPTY_MOD_FOLDER_RE.match(str(current or "").strip()):
            return True
    return False


def _should_fill_nexus_source_url(
    current: str,
    candidate: str,
    *,
    current_external_id: str = "",
    managed_path: Path | None = None,
) -> bool:
    """True when offline HTML canonical URL may replace the stored source_url."""
    cand = _strip_query(str(candidate or "").strip())
    if not cand:
        return False
    if not _extract_mod_id_from_url(cand):
        return False
    cur = _strip_query(str(current or "").strip())
    if not cur:
        return True
    if _is_placeholder_nexus_source_url(
        cur,
        current_external_id=current_external_id,
    ):
        return True
    if _NEXUS_MOD_URL_NO_GAME_RE.match(cur):
        return True
    cand_id = _extract_mod_id_from_url(cand)
    cur_id = _extract_mod_id_from_url(cur)
    ext = str(current_external_id or "").strip()
    if cand_id and (cur_id == cand_id or ext == cand_id) and cur != cand:
        return True
    if cand_id and _is_placeholder_nexus_external_id(ext, managed_path=managed_path):
        return True
    return False


def _should_fill_nexus_external_id(
    current: str,
    candidate: str,
    *,
    managed_path: Path | None = None,
) -> bool:
    """True when HTML numeric Mod ID may replace a placeholder external_id."""
    cand = str(candidate or "").strip()
    if not is_valid_nexus_mod_id(cand):
        return False
    cur = str(current or "").strip()
    if not cur:
        return True
    if _is_placeholder_nexus_external_id(cur, managed_path=managed_path):
        return True
    return False


def _strip_query(url: str) -> str:
    try:
        parts = urlparse(url)
        return urlunparse(parts._replace(query="", fragment=""))
    except Exception:
        return url


def _extract_title(soup) -> tuple[str, str]:
    tag = soup.find("meta", property="og:title")
    if tag:
        val = str(tag.get("content") or "").strip()
        if val:
            return val, "high"
    for h1 in soup.find_all("h1"):
        text = h1.get_text(strip=True)
        if text and not _LOGIN_TITLE_RE.match(text):
            return text, "medium"
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        candidate = text.split(" at ")[0].strip() if " at " in text else text.strip()
        if candidate:
            return candidate, "medium"
    return "", "none"


def _extract_nexus_url(soup, metadata_json_path: Path) -> tuple[str, str]:
    tag = soup.find("meta", property="og:url")
    if tag:
        val = str(tag.get("content") or "").strip()
        if val and "nexusmods.com" in val and "/mods/" in val:
            return _strip_query(val), "high"
    canonical = soup.find("link", rel="canonical")
    if canonical:
        val = str(canonical.get("href") or "").strip()
        if val and "nexusmods.com" in val and "/mods/" in val:
            return _strip_query(val), "high"
    mod_id = ""
    el = soup.find(attrs={"data-mod-id": True})
    if el:
        mod_id = str(el.get("data-mod-id") or "").strip()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if "nexusmods.com" not in href or "/mods/" not in href:
            continue
        if mod_id and f"/mods/{mod_id}" not in href:
            continue
        return _strip_query(href), "medium"
    if metadata_json_path.is_file():
        try:
            data = json.loads(metadata_json_path.read_text(encoding="utf-8"))
            orig = str(data.get("original_url") or "").strip()
            if orig and "nexusmods.com" in orig and "/mods/" in orig:
                return _strip_query(orig), "medium"
        except Exception:
            pass
    return "", "none"


def _extract_mod_id(soup, nexus_url: str) -> tuple[str, str]:
    el = soup.find(attrs={"data-mod-id": True})
    if el:
        val = str(el.get("data-mod-id") or "").strip()
        if val.isdigit():
            return val, "high"
    if nexus_url:
        m = _NEXUS_MOD_URL_RE.search(nexus_url)
        if m:
            return m.group(1), "high"
    return "", "none"


def _is_local_asset(src: str) -> bool:
    return src.startswith("./assets/") or src.startswith("assets/")


def _resolve_asset(src: str, index_html_path: Path) -> Path | None:
    try:
        resolved = (index_html_path.parent / src).resolve()
        if resolved.is_file():
            return resolved
    except Exception:
        pass
    return None


def _extract_gallery_image(soup, index_html_path: Path) -> tuple[Path | None, str]:
    """Find first true Mod Gallery image (local ./assets/ only; no remote URLs)."""

    def _first_local(container) -> Path | None:
        if container is None:
            return None
        for img in container.find_all("img"):
            src = str(img.get("src") or "").strip()
            if _is_local_asset(src):
                resolved = _resolve_asset(src, index_html_path)
                if resolved is not None:
                    return resolved
        return None

    # Primary: #sidebargallery > ul.thumbgallery
    gallery = soup.find("div", id="sidebargallery")
    if gallery:
        thumbs = gallery.find("ul", class_=re.compile(r"\bgallery\b|\bthumbgallery\b"))
        if thumbs:
            found = _first_local(thumbs)
            if found:
                return found, "high"
        found = _first_local(gallery)
        if found:
            return found, "high"

    # Fallback 1: ul.thumbgallery anywhere
    thumbs = soup.find("ul", class_=re.compile(r"\bgallery\b|\bthumbgallery\b"))
    if thumbs:
        found = _first_local(thumbs)
        if found:
            return found, "medium"

    # Fallback 2: new Tailwind UI containers
    for div in soup.find_all("div", class_=True):
        cls = " ".join(div.get("class") or [])
        if "group/image" in cls or "aspect-video" in cls:
            found = _first_local(div)
            if found:
                return found, "medium"

    return None, "none"


def _extract_description(soup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag:
            val = str(tag.get("content") or "").strip()
            if val:
                return val
    return ""


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------

def parse_nexus_offline_html(index_html_path: Path) -> NexusOfflineCandidates:
    """Parse a Nexus offline ``index.html`` and return metadata candidates.

    PURE function: reads the HTML file only, writes nothing, touches no DB,
    makes no network requests.

    This function MUST only be invoked from the offline-HTML-import path
    (``attach_nexus_offline_page`` → ``_apply_nexus_offline_metadata``).
    It must NEVER be called from refresh_mod, reconcile, show_mod, or any
    ordinary Refresh pipeline.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[NEXUS_SCRAPER] BeautifulSoup unavailable; skipping parse")
        return NexusOfflineCandidates()

    try:
        html_text = index_html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[NEXUS_SCRAPER] cannot read %s: %s", index_html_path, exc)
        return NexusOfflineCandidates()

    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] HTML parse failed: %s", exc)
        return NexusOfflineCandidates()

    c = NexusOfflineCandidates()
    metadata_json = index_html_path.parent / "metadata.json"

    # title
    try:
        c.title, c.confidence["title"] = _extract_title(soup)
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] failed to parse title: %s", exc)
        c.confidence["title"] = "error"

    # source_url
    try:
        c.source_url, c.confidence["source_url"] = _extract_nexus_url(soup, metadata_json)
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] failed to parse source_url: %s", exc)
        c.confidence["source_url"] = "error"

    # external_id
    try:
        c.external_id, c.confidence["external_id"] = _extract_mod_id(soup, c.source_url)
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] failed to parse external_id: %s", exc)
        c.confidence["external_id"] = "error"

    # gallery cover
    try:
        c.cover_asset_path, c.confidence["cover"] = _extract_gallery_image(soup, index_html_path)
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] failed to parse gallery image: %s", exc)
        c.confidence["cover"] = "error"

    # description
    try:
        c.description = _extract_description(soup)
        c.confidence["description"] = "medium" if c.description else "none"
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] failed to parse description: %s", exc)
        c.confidence["description"] = "error"

    return c


# ---------------------------------------------------------------------------
# Merge helper — fills only missing/placeholder fields
# ---------------------------------------------------------------------------

def _patch_mod_db_identity(
    database,
    mod_id: str,
    *,
    title: str | None = None,
    display_name: str | None = None,
    source_url: str | None = None,
    external_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Patch identity columns on ``mods`` (bypasses COALESCE-only title updates)."""
    from core.db_manager import _utc_now

    mid = int(str(mod_id).strip())
    sets: list[str] = ["updated_at = ?"]
    params: list[object] = [_utc_now()]
    if title is not None and str(title).strip():
        sets.append("title = ?")
        params.append(str(title).strip())
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(str(display_name).strip())
    if source_url is not None:
        sets.append("source_url = ?")
        params.append(str(source_url).strip())
    if external_id is not None:
        sets.append("external_id = ?")
        params.append(str(external_id).strip())
    if workspace_id is not None:
        sets.append("workspace_id = ?")
        params.append(str(workspace_id).strip())
    if len(sets) == 1:
        return
    params.append(mid)
    with database._lock:
        database._conn.execute(
            f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
            tuple(params),
        )
        database._conn.commit()


def apply_nexus_offline_candidates(
    mod_id: str | int,
    managed_path: str | Path,
    candidates: NexusOfflineCandidates,
    *,
    db=None,
) -> None:
    """Apply *candidates* to an existing Mod, filling ONLY missing fields.

    Merge rules
    -----------
    title        — write when placeholder or import-default folder title
    display_name — write when user display override empty and not overridden
    source_url   — write when empty, incomplete, or HTML canonical differs
    external_id  — write when empty or system placeholder (folder/local)
    description  — write only when currently empty AND no user override
    cover        — copy asset only when no cover file exists AND no user override

    This function MUST only be called from the offline-HTML-import path.
    NEVER call it from refresh_mod / reconcile / sync / show_mod.

    Directory naming is intentionally *not* performed here. Filling title /
    URL / id in DB + sidecar is a metadata merge. Canonical
    ``Empty Mod <random>`` → parsed-title rename is owned by
    ``attach_nexus_offline_page`` via path_lifecycle so filesystem, DB path,
    sidecar, and identity stay aligned. Do not add an isolated rename here.
    """
    from core.db_manager import get_db
    from core.models import is_unknown_mod_title
    from services.metadata_ownership import (
        FIELD_COVER,
        FIELD_DESCRIPTION,
        FIELD_DISPLAY_NAME,
        merge_official_sidecar_fields,
        should_apply_official_field,
    )

    logger.info(
        "[NEXUS_SCRAPER] SCRAPER TRIGGERED BY: OFFLINE_HTML_IMPORT mod_id=%s", mod_id
    )

    mid = str(mod_id).strip()
    database = db if db is not None else get_db()
    dest = Path(managed_path)

    try:
        info = database.get_mod_display_info(mid)
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] cannot read mod info: %s", exc)
        return

    if info is None:
        logger.warning("[NEXUS_SCRAPER] mod_id=%s not found in DB; skipping", mid)
        return

    try:
        overrides = database.get_user_override_fields(mid)
    except Exception:
        overrides = {}

    try:
        from services.file_ops import read_info_metadata_dict
        meta_dict: dict = read_info_metadata_dict(dest) or {}
    except Exception:
        meta_dict = {}

    current_title = str(getattr(info, "steam_name", "") or "").strip()
    current_user_display = str(getattr(info, "user_display_name", "") or "").strip()
    current_source_url = str(getattr(info, "source_url", "") or "").strip()
    current_external_id = str(getattr(info, "external_id", "") or "").strip()
    current_description = str(getattr(info, "description", "") or "").strip()

    source_url_will_fill = bool(
        candidates.source_url
        and _should_fill_nexus_source_url(
            current_source_url,
            candidates.source_url,
            current_external_id=current_external_id,
            managed_path=dest,
        )
    )
    external_id_will_fill = bool(
        candidates.external_id
        and _should_fill_nexus_external_id(
            current_external_id,
            candidates.external_id,
            managed_path=dest,
        )
    )

    # Sidecar merge (always records official title; user fields obey overrides).
    updated_meta = merge_official_sidecar_fields(
        meta_dict,
        mod_id=mid,
        overrides=overrides,
        official_title=candidates.title,
        official_description=candidates.description,
    )
    if source_url_will_fill:
        updated_meta["url"] = candidates.source_url
    if external_id_will_fill and candidates.external_id:
        updated_meta["workspace_id"] = candidates.external_id

    db_title: str | None = None
    db_display: str | None = None
    db_source_url: str | None = None
    db_external_id: str | None = None
    db_workspace_id: str | None = None

    # ---- title / display_name ----
    if candidates.title:
        if _should_fill_nexus_title(
            current_title,
            mod_id=mid,
            managed_path=dest,
            source_url_will_fill=source_url_will_fill,
            external_id_will_fill=external_id_will_fill,
        ):
            db_title = candidates.title
            logger.info(
                "[NEXUS_SCRAPER] title filled: %r (conf=%s)",
                candidates.title,
                candidates.confidence.get("title"),
            )
        else:
            logger.info("[NEXUS_SCRAPER] title already exists, skipped")

        display_is_placeholder = (
            not current_user_display
            or _is_import_default_title(current_user_display, dest)
            or _EMPTY_MOD_FOLDER_RE.match(current_user_display)
            or _is_placeholder_nexus_external_id(current_user_display, managed_path=dest)
        )
        if should_apply_official_field(
            FIELD_DISPLAY_NAME,
            overrides=overrides,
            local_value=current_user_display,
            mod_id=mid,
        ) and (
            db_title is not None
            or is_unknown_mod_title(current_title, published_file_id=mid)
            or (display_is_placeholder and (source_url_will_fill or external_id_will_fill))
        ):
            db_display = candidates.title
            logger.info("[NEXUS_SCRAPER] display_name filled: %r", candidates.title)
        else:
            logger.info("[NEXUS_SCRAPER] display_name already exists or overridden, skipped")

    # ---- source_url ----
    if source_url_will_fill:
        db_source_url = candidates.source_url
        logger.info(
            "[NEXUS_SCRAPER] source_url filled: %r (conf=%s)",
            candidates.source_url,
            candidates.confidence.get("source_url"),
        )
    elif candidates.source_url:
        logger.info("[NEXUS_SCRAPER] source_url already exists, skipped")

    # ---- external_id / workspace_id ----
    if external_id_will_fill:
        db_external_id = candidates.external_id
        db_workspace_id = candidates.external_id
        logger.info(
            "[NEXUS_SCRAPER] external_id filled: %r (conf=%s)",
            candidates.external_id,
            candidates.confidence.get("external_id"),
        )
    elif candidates.external_id:
        logger.info("[NEXUS_SCRAPER] external_id already exists, skipped")

    # ---- description (metadata only; DB description untouched here) ----
    if candidates.description and should_apply_official_field(
        FIELD_DESCRIPTION,
        overrides=overrides,
        local_value=current_description,
    ):
        logger.info("[NEXUS_SCRAPER] description filled")
    elif candidates.description:
        logger.info("[NEXUS_SCRAPER] description already exists or overridden, skipped")

    # ---- persist DB identity fields ----
    if any(v is not None for v in (db_title, db_display, db_source_url, db_external_id)):
        try:
            _patch_mod_db_identity(
                database,
                mid,
                title=db_title,
                display_name=db_display,
                source_url=db_source_url,
                external_id=db_external_id,
                workspace_id=db_workspace_id,
            )
        except Exception as exc:
            logger.warning("[NEXUS_SCRAPER] DB identity update failed: %s", exc)

    # ---- persist metadata.json ----
    if updated_meta != meta_dict:
        try:
            from services.file_ops import persist_unified_metadata_dict
            persist_unified_metadata_dict(dest, updated_meta, sync_backup=False)
        except Exception as exc:
            logger.warning("[NEXUS_SCRAPER] metadata.json write failed: %s", exc)

    # ---- cover ----
    if candidates.cover_asset_path and should_apply_official_field(
        FIELD_COVER,
        overrides=overrides,
        local_value=str(getattr(info, "cover_path", "") or "").strip(),
    ):
        _install_offline_cover(mid, dest, candidates.cover_asset_path)
    elif candidates.cover_asset_path:
        logger.info("[NEXUS_SCRAPER] cover already exists or overridden, skipped")
    else:
        logger.info("[NEXUS_SCRAPER] no gallery cover found, skipped")


def _install_offline_cover(
    mod_id: str,
    managed_path: Path,
    asset_path: Path,
) -> None:
    try:
        from services.importers.image_picker import apply_cover_to_mod
        rel = apply_cover_to_mod(
            managed_path,
            asset_path,
            mod_id=mod_id,
            update_db=True,
            sync_backup=False,
            mark_user_override=False,
        )
        if rel:
            logger.info("[NEXUS_SCRAPER] cover filled from asset: %s", asset_path.name)
        else:
            logger.warning("[NEXUS_SCRAPER] cover copy failed (empty rel path)")
    except Exception as exc:
        logger.warning("[NEXUS_SCRAPER] cover install failed: %s", exc)
