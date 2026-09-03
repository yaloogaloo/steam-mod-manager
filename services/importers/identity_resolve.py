"""Import identity resolution — before duplicate gate / materialize / DB write.

Pipeline order (enforced by callers):
  source → stage → identity resolve → duplicate check → materialize → DB write

Offline page snapshots are an identity *source*, not a post-import scrape step
for the gate. Metadata scrape/attach may still run after a successful import.

For Nexus Offline HTML, parsed title from ``parse_offline_page_identity``
feeds ``canonical_nexus_offline_import_title`` so the library directory is
named after the Mod title, not the ``Empty Mod <random>`` stub folder.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    normalize_platform,
)
from services.importers.importer_base import ImportResult

logger = logging.getLogger(__name__)

MISSING_OFFICIAL_IDENTITY = "缺少官方 Mod 身份（ID 或官网链接）"


@dataclass
class ImportIdentity:
    """Resolved import identity ready for duplicate check + register."""

    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    workshop_id: str = ""
    title: str = ""

    def has_gate_fields(self) -> bool:
        """True when at least one identity field is present for the gate."""
        return bool(
            str(self.workshop_id or "").strip()
            or str(self.external_id or "").strip()
            or str(self.source_url or "").strip()
        )

    def has_official_identity(self) -> bool:
        """True when identity is suitable for steam/nexus/mod.io/github writes."""
        plat = normalize_platform(self.platform)
        wid = str(self.workshop_id or "").strip()
        ext = str(self.external_id or "").strip()
        url = str(self.source_url or "").strip()
        if plat == PLATFORM_STEAM:
            return wid.isdigit() or (ext.isdigit() and not ext.startswith("local/"))
        if plat == PLATFORM_NEXUS:
            if url and "nexusmods.com" in url.lower() and "/mods/" in url.lower():
                return True
            return ext.isdigit()
        if plat == PLATFORM_GITHUB:
            return bool(url) or ("/" in ext and not ext.startswith("local/"))
        if plat == PLATFORM_MODIO:
            return bool(url) or ext.isdigit() or bool(ext)
        if plat == PLATFORM_OTHER:
            return True
        return self.has_gate_fields()


def parse_offline_page_identity(path: str | Path) -> ImportIdentity:
    """Extract source_url / external_id / title from a saved offline HTML/MHTML page."""
    src = Path(path).expanduser()
    if not src.is_file():
        return ImportIdentity()

    suffix = src.suffix.lower()
    try:
        from services.offline.nexus_html_parser import parse_nexus_offline_html
    except Exception:
        return ImportIdentity()

    try:
        if suffix in {".html", ".htm"}:
            candidates = parse_nexus_offline_html(src)
        elif suffix in {".mhtml", ".mht"}:
            from services.offline.mhtml import extract_mhtml

            html_text, _assets, _cid = extract_mhtml(src)
            if not str(html_text or "").strip():
                return ImportIdentity()
            with tempfile.TemporaryDirectory(prefix="smm_id_gate_") as tmp:
                index = Path(tmp) / "index.html"
                index.write_text(html_text, encoding="utf-8", errors="replace")
                candidates = parse_nexus_offline_html(index)
        else:
            return ImportIdentity()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IDENTITY] offline page parse failed: %s", exc)
        return ImportIdentity()

    return ImportIdentity(
        source_url=str(getattr(candidates, "source_url", "") or "").strip(),
        external_id=str(getattr(candidates, "external_id", "") or "").strip(),
        title=str(getattr(candidates, "title", "") or "").strip(),
    )


def canonical_nexus_offline_import_title(
    *,
    user_title: str = "",
    parsed_title: str = "",
    folder_name: str = "",
) -> str:
    """Choose the title used for Nexus identity + canonical library directory name.

    Why this order exists (do not drop it in a future import-flow refactor):

    Offline HTML is an identity *source*. The parser runs before materialize.
    A valid parsed Mod title MUST become the canonical directory name
    (``<game>/<parsed title>/``). Temp stub folders named ``Empty Mod <random>``
    exist only so an empty local path can still copy a payload; they are not
    the final filesystem identity.

    ``Empty Mod <random>`` remains a fallback only when no usable title exists.
    Rename after materialize, if still needed, must go through path_lifecycle
    so filesystem / DB / sidecar / identity stay aligned — never an isolated
    ``os.rename`` / ``shutil.move``.
    """
    from core.models import is_unknown_mod_title
    from services.identity_service import is_empty_mod_placeholder

    def _usable(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if is_empty_mod_placeholder(text):
            return ""
        if is_unknown_mod_title(text):
            return ""
        return text

    return (
        _usable(user_title)
        or _usable(parsed_title)
        or _usable(folder_name)
        or str(user_title or "").strip()
        or str(parsed_title or "").strip()
        or str(folder_name or "").strip()
    )


def resolve_import_identity(
    platform: str,
    *,
    workshop_id: str = "",
    nexus_url: str = "",
    nexus_id: str = "",
    github_url: str = "",
    modio_url: str = "",
    modio_id: str = "",
    source_url: str = "",
    offline_html: str = "",
    allow_local_fallback: bool = False,
) -> ImportIdentity | ImportResult:
    """
    Resolve identity from caller fields, then offline page if still incomplete.

    Returns :class:`ImportIdentity` when the gate may proceed, or an error
    :class:`ImportResult` when an official platform lacks identity and local
    fallback is not allowed.
    """
    plat = normalize_platform(platform)
    identity = ImportIdentity(platform=plat)

    if plat == PLATFORM_STEAM:
        identity.workshop_id = str(workshop_id or "").strip()
        identity.external_id = identity.workshop_id
        if identity.workshop_id.isdigit():
            from core.mod_platform import steam_workshop_url

            identity.source_url = steam_workshop_url(identity.workshop_id)
    elif plat == PLATFORM_NEXUS:
        from services.importers.nexus import parse_nexus_id

        raw_url = str(nexus_url or "").strip()
        raw_id = str(nexus_id or "").strip()
        identity.source_url = raw_url if not raw_url.isdigit() else ""
        identity.external_id = parse_nexus_id(raw_url, raw_id)
        if raw_url.isdigit() and not raw_id:
            identity.external_id = raw_url
            identity.source_url = f"https://www.nexusmods.com/mods/{raw_url}"
    elif plat == PLATFORM_GITHUB:
        identity.source_url = str(github_url or "").strip()
        if identity.source_url:
            from services.importers.github import GithubImporter

            repo = GithubImporter.parse_repo(identity.source_url)
            identity.external_id = repo
    elif plat == PLATFORM_MODIO:
        from services.importers.modio import parse_modio_id

        identity.source_url = str(modio_url or "").strip()
        identity.external_id = parse_modio_id(identity.source_url, modio_id) or str(
            modio_id or ""
        ).strip()
    elif plat == PLATFORM_OTHER:
        identity.source_url = str(source_url or "").strip()
        allow_local_fallback = True
    else:
        identity.source_url = str(source_url or "").strip()

    # Offline snapshot fills missing official URL / id (Nexus HTML is the
    # primary case; any platform may supply a page that yields URL + id).
    offline = str(offline_html or "").strip()
    if offline and (
        not identity.source_url
        or not identity.external_id
        or (plat == PLATFORM_NEXUS and not str(identity.external_id).isdigit())
    ):
        from_page = parse_offline_page_identity(offline)
        if not identity.source_url and from_page.source_url:
            identity.source_url = from_page.source_url
        if (not identity.external_id or not str(identity.external_id).isdigit()) and (
            from_page.external_id
        ):
            identity.external_id = from_page.external_id
        if not identity.title and from_page.title:
            identity.title = from_page.title
        # If page gave a Nexus URL but id still empty, re-parse id from URL.
        if plat == PLATFORM_NEXUS and identity.source_url and not str(
            identity.external_id
        ).isdigit():
            from services.importers.nexus import parse_nexus_id

            nid = parse_nexus_id(identity.source_url, "")
            if nid:
                identity.external_id = nid

    gate = require_identity_for_write(
        identity, allow_local_fallback=allow_local_fallback
    )
    if gate is not None:
        return gate
    return identity


def require_identity_for_write(
    identity: ImportIdentity,
    *,
    allow_local_fallback: bool = False,
) -> ImportResult | None:
    """
    Return an error result when write must not proceed.

    Official platforms (steam / nexus / github / mod.io) require official
    identity unless *allow_local_fallback* (batch / local / 其它).
    """
    plat = normalize_platform(identity.platform)
    if allow_local_fallback or plat == PLATFORM_OTHER:
        return None
    if identity.has_official_identity():
        return None
    return ImportResult(
        success=False,
        error=MISSING_OFFICIAL_IDENTITY,
        platform=plat,
        external_id=str(identity.external_id or ""),
        source_url=str(identity.source_url or ""),
    )


def apply_directory_import_identity(
    identity: ImportIdentity,
    *,
    folder: str | Path,
    platform: str = "",
) -> ImportIdentity:
    """
    Unify directory-import identity for batch and single flows.

    Priority:
      1. Official ID / URL already on *identity* (unchanged)
      2. Else use ``folder.name`` as ``external_id`` (and Steam workshop id
         when the folder name is numeric) — same key batch mode already uses

    Archives / non-directory imports must not call this helper.
    Does not invent platform URLs.
    """
    plat = normalize_platform(platform or identity.platform)
    out = ImportIdentity(
        platform=plat,
        external_id=str(identity.external_id or "").strip(),
        source_url=str(identity.source_url or "").strip(),
        workshop_id=str(identity.workshop_id or "").strip(),
        title=str(identity.title or "").strip(),
    )
    folder_name = Path(folder).name.strip()
    if not folder_name:
        return out

    # Priority 1 — keep resolved official identity.
    if out.has_official_identity():
        return out

    # Priority 2 — directory name as external identity (matches batch).
    # Overwrite non-official ids (e.g. parent-folder params leaked into batch).
    if plat == PLATFORM_STEAM:
        if folder_name.isdigit():
            from core.mod_platform import steam_workshop_url

            out.workshop_id = folder_name
            out.external_id = folder_name
            out.source_url = steam_workshop_url(folder_name)
        return out

    out.external_id = folder_name
    return out
