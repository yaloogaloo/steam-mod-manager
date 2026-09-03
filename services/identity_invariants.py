"""Read-only invalid-entity scanner. Never mutates production."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_provisional_external_id,
    normalize_platform,
)
from core.models import is_unknown_mod_title
from core.paths import database_path, default_mod_library
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict
from services.importers.duplicate_check import normalize_source_url
from services.identity_repair import (
    internal_ids_embedded,
    open_readonly_sqlite,
)
from services.verification_result import (
    NOT_APPLIED,
    UNCHANGED,
    VerificationResult,
)

_EMPTY_MOD_RE = re.compile(r"^Empty Mod [0-9a-fA-F]{8}$")

INVALID_INTERNAL_STEAM_ID = "INVALID_INTERNAL_STEAM_ID"
INVALID_STEAM_WORKSPACE = "INVALID_STEAM_WORKSPACE"
INVALID_STEAM_PUBLISHED_FILE_ID = "INVALID_STEAM_PUBLISHED_FILE_ID"
INVALID_STEAM_SOURCE_URL = "INVALID_STEAM_SOURCE_URL"
DUPLICATE_EXTERNAL_ID = "DUPLICATE_EXTERNAL_ID"
DUPLICATE_CANONICAL_URL = "DUPLICATE_CANONICAL_URL"
DUPLICATE_SOURCE_URL = "DUPLICATE_SOURCE_URL"
ORPHAN_FOLDER = "ORPHAN_FOLDER"
ORPHAN_DB_ROW = "ORPHAN_DB_ROW"
ORPHAN_SIDECAR = "ORPHAN_SIDECAR"
UNKNOWN_MOD_INTERNAL_VISIBLE = "UNKNOWN_MOD_INTERNAL_VISIBLE"
EMPTY_MOD_PLACEHOLDER = "EMPTY_MOD_PLACEHOLDER"
UNRESOLVED_ENTITY_WITH_PLATFORM = "UNRESOLVED_ENTITY_WITH_PLATFORM"
INTERNAL_ID_EXPOSED_AS_PLATFORM_ID = "INTERNAL_ID_EXPOSED_AS_PLATFORM_ID"
STUB_EXTERNAL_ID = "STUB_EXTERNAL_ID"
IDENTITY_FIELDS_CONTRADICT = "IDENTITY_FIELDS_CONTRADICT"
INTERNAL_ID_USED_AS_WORKSPACE_ID = "INTERNAL_ID_USED_AS_WORKSPACE_ID"
INTERNAL_ID_USED_AS_EXTERNAL_ID = "INTERNAL_ID_USED_AS_EXTERNAL_ID"
INTERNAL_ID_USED_IN_PLATFORM_URL = "INTERNAL_ID_USED_IN_PLATFORM_URL"
INTERNAL_ID_EXPOSED_IN_USER_UI = "INTERNAL_ID_EXPOSED_IN_USER_UI"
AMBIGUOUS_MOD_ID_USAGE = "AMBIGUOUS_MOD_ID_USAGE"
FILESYSTEM_PATH_USED_AS_SOLE_MOD_IDENTITY = "FILESYSTEM_PATH_USED_AS_SOLE_MOD_IDENTITY"
FOLDER_RENAME_CAUSES_NEW_MOD = "FOLDER_RENAME_CAUSES_NEW_MOD"
RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF = "RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF"
DUPLICATE_WORKSPACE_ID_CREATION = "DUPLICATE_WORKSPACE_ID_CREATION"
UNSAFE_RECONCILE_IDENTITY_FALLBACK = "UNSAFE_RECONCILE_IDENTITY_FALLBACK"
SIDECAR_REHYDRATES_INVALID_IDENTITY = "SIDECAR_REHYDRATES_INVALID_IDENTITY"
SIDECAR_INTERNAL_ID_USED_AS_PLATFORM_ID = "SIDECAR_INTERNAL_ID_USED_AS_PLATFORM_ID"
INVALID_SOURCE_URL_REHYDRATION = "INVALID_SOURCE_URL_REHYDRATION"
PUBLISHED_FILE_ID_USED_AS_SOLE_IDENTITY = "PUBLISHED_FILE_ID_USED_AS_SOLE_IDENTITY"
NUMERIC_ID_USED_AS_STEAM_PROOF = "NUMERIC_ID_USED_AS_STEAM_PROOF"
INTERNAL_ID_LEAKED_TO_WORKSPACE_ID = "INTERNAL_ID_LEAKED_TO_WORKSPACE_ID"
INTERNAL_ID_LEAKED_TO_PLATFORM_ID = "INTERNAL_ID_LEAKED_TO_PLATFORM_ID"
INTERNAL_ID_LEAKED_TO_PLATFORM_URL = "INTERNAL_ID_LEAKED_TO_PLATFORM_URL"
RECONCILE_CREATE_BEFORE_IDENTITY_RESOLUTION = "RECONCILE_CREATE_BEFORE_IDENTITY_RESOLUTION"
MULTIPLE_FILESYSTEM_PATHS_ONE_ENTITY = "MULTIPLE_FILESYSTEM_PATHS_ONE_ENTITY"
MULTIPLE_VISIBLE_CARDS_ONE_INTERNAL_ID = "MULTIPLE_VISIBLE_CARDS_ONE_INTERNAL_ID"
LEGACY_ID_USED_AS_NEW_IDENTITY_PROOF = "LEGACY_ID_USED_AS_NEW_IDENTITY_PROOF"
CONFLICT_DETECTOR_CREATES_MOD = "CONFLICT_DETECTOR_CREATES_MOD"
CONFLICT_DETECTOR_CREATES_IDENTITY = "CONFLICT_DETECTOR_CREATES_IDENTITY"
CONFLICT_DETECTOR_MUTATES_WORKSPACE_ID = "CONFLICT_DETECTOR_MUTATES_WORKSPACE_ID"
CONFLICT_DETECTOR_AUTO_CREATES_RELATIONSHIP = "CONFLICT_DETECTOR_AUTO_CREATES_RELATIONSHIP"
PATH_OVERLAP_AUTO_MEANS_CONFLICT = "PATH_OVERLAP_AUTO_MEANS_CONFLICT"
CONFLICT_SCAN_OVERWRITES_USER_RESOLUTION = "CONFLICT_SCAN_OVERWRITES_USER_RESOLUTION"


@dataclass
class InvariantFinding:
    entity_id: str
    platform: str = ""
    app_id: int = 0
    external_id: str = ""
    workspace_id: str = ""
    canonical_url: str = ""
    source_url: str = ""
    display_name: str = ""
    folder_path: str = ""
    violation_code: str = ""
    severity: str = "CRITICAL"
    evidence: str = ""
    recommended_action: str = ""


@dataclass
class InvariantScanReport:
    findings: list[InvariantFinding] = field(default_factory=list)
    scanned_db_rows: int = 0
    scanned_folders: int = 0
    verification: VerificationResult = field(default_factory=VerificationResult)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return {
            "scanned_db_rows": self.scanned_db_rows,
            "scanned_folders": self.scanned_folders,
            "counts": counts,
            "CRITICAL": counts.get("CRITICAL", 0),
            "HIGH": counts.get("HIGH", 0),
            "REQUIRES_REVIEW": counts.get("REQUIRES_REVIEW", 0),
            "findings": [asdict(f) for f in self.findings],
            "verification": self.verification.to_dict(),
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_empty_mod_placeholder(name: str) -> bool:
    return bool(_EMPTY_MOD_RE.match(str(name or "").strip()))


def _steam_url_uses_internal(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    embedded = internal_ids_embedded(text)
    if embedded and "steamcommunity.com" in text.lower():
        return embedded[0]
    if embedded:
        return embedded[0]
    return ""


def scan_invalid_entities(
    library_root: str | Path | None = None,
    db_path: str | Path | None = None,
    db: Any | None = None,
) -> InvariantScanReport:
    """Readonly scan. Uses sqlite ``mode=ro`` when *db* is omitted."""
    root = Path(library_root) if library_root else default_mod_library()
    report = InvariantScanReport()
    report.verification = VerificationResult(
        plan_status="NO_PLAN",
        apply_status=NOT_APPLIED,
        production_status=UNCHANGED,
        verification_status="NOT_VERIFIED",
        evidence={"scanner": "identity_invariants", "readonly": True},
    )
    close = False
    if db is None:
        facade = open_readonly_sqlite(Path(db_path) if db_path else database_path())
        db = facade
        close = True
    try:
        _scan_db(db, report)
        _scan_fs(root, db, report)
    finally:
        if close:
            try:
                db._conn.close()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
    return report


def _rows(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db._lock:  # noqa: SLF001
        raw = db._conn.execute(sql, params).fetchall()  # noqa: SLF001
    out: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append({k: row[k] for k in row.keys()})
    return out


def _scan_db(db: Any, report: InvariantScanReport) -> None:
    try:
        rows = _rows(
            db,
            """
            SELECT mod_id, app_id, title, platform, external_id, workspace_id,
                   source_url, last_known_path, display_name
            FROM mods
            """,
        )
    except sqlite3.Error:
        return
    report.scanned_db_rows = len(rows)
    ext_map: dict[tuple[str, str], list[str]] = {}
    url_map: dict[str, list[str]] = {}
    for row in rows:
        mid = _text(row.get("mod_id"))
        plat = normalize_platform(_text(row.get("platform")))
        ext = _text(row.get("external_id"))
        ws = _text(row.get("workspace_id"))
        url = _text(row.get("source_url"))
        title_raw = _text(row.get("title"))
        display = _text(row.get("display_name"))
        title = display or title_raw
        folder = _text(row.get("last_known_path"))
        aid = int(row.get("app_id") or 0)

        def add(code: str, severity: str, evidence: str, action: str) -> None:
            report.findings.append(
                InvariantFinding(
                    entity_id=mid,
                    platform=plat,
                    app_id=aid,
                    external_id=ext,
                    workspace_id=ws,
                    canonical_url=url,
                    source_url=url,
                    display_name=title,
                    folder_path=folder,
                    violation_code=code,
                    severity=severity,
                    evidence=evidence,
                    recommended_action=action,
                )
            )

        if plat == PLATFORM_STEAM and is_internal_mod_id(mid):
            add(
                INVALID_INTERNAL_STEAM_ID,
                "CRITICAL",
                "platform=steam bound to internal mod_id",
                "REMOVE_INVALID_DUPLICATE or unbind Steam platform",
            )
        if plat == PLATFORM_STEAM and is_internal_mod_id(ws):
            add(
                INVALID_STEAM_WORKSPACE,
                "CRITICAL",
                f"workspace_id={ws} is internal",
                "SCRUB workspace_id",
            )
        if is_internal_mod_id(mid) and ws == mid:
            add(
                INTERNAL_ID_USED_AS_WORKSPACE_ID,
                "CRITICAL",
                "workspace_id equals internal mod_id",
                "SCRUB workspace_id; never copy Internal ID",
            )
            add(
                INVALID_STEAM_WORKSPACE,
                "CRITICAL",
                "workspace_id equals internal mod_id",
                "SCRUB workspace_id",
            )
        if is_internal_mod_id(ext) or (ext == mid and is_internal_mod_id(mid) and plat == PLATFORM_STEAM):
            add(
                INTERNAL_ID_USED_AS_EXTERNAL_ID,
                "CRITICAL",
                f"external_id={ext}",
                "sanitize external_id; never copy Internal ID",
            )
            add(
                INTERNAL_ID_EXPOSED_AS_PLATFORM_ID,
                "CRITICAL",
                f"external_id={ext}",
                "sanitize external_id",
            )
        if is_provisional_external_id(ext) and ext.startswith("stub:"):
            add(
                STUB_EXTERNAL_ID,
                "HIGH",
                f"external_id={ext}",
                "bind official external_id or leave unresolved",
            )
        stolen = _steam_url_uses_internal(url)
        if stolen:
            add(
                INTERNAL_ID_USED_IN_PLATFORM_URL,
                "CRITICAL",
                f"source_url embeds internal id ({stolen}): {url}",
                "SCRUB_POLLUTING_SOURCE_URL",
            )
            add(
                INVALID_STEAM_SOURCE_URL,
                "CRITICAL",
                f"source_url embeds internal id ({stolen}): {url}",
                "SCRUB_POLLUTING_SOURCE_URL",
            )
        if is_unknown_mod_title(title, published_file_id=mid) and is_internal_mod_id(mid):
            add(
                UNKNOWN_MOD_INTERNAL_VISIBLE,
                "HIGH",
                f"title={title}",
                "do not mint; treat as missing metadata only",
            )
        if is_empty_mod_placeholder(title_raw) or is_empty_mod_placeholder(display) or is_empty_mod_placeholder(Path(folder).name if folder else ""):
            from services.identity_service import has_official_platform_identity

            official = has_official_platform_identity(
                platform=plat,
                external_id=ext,
                source_url=url,
                workshop_id=ws,
            )
            add(
                EMPTY_MOD_PLACEHOLDER,
                "REQUIRES_REVIEW" if official else "HIGH",
                f"Empty Mod placeholder title/folder title={title} folder={folder}",
                "SCRUB_PLACEHOLDER_TITLE keep entity"
                if official
                else "bind official identity; do not mint",
            )
        if plat and is_internal_mod_id(mid) and not ext and not url and not ws:
            add(
                UNRESOLVED_ENTITY_WITH_PLATFORM,
                "HIGH",
                "platform set without official external identity",
                "bind official identity or clear platform",
            )
        if plat == PLATFORM_STEAM and ext and ext != mid and not is_internal_mod_id(mid):
            add(
                IDENTITY_FIELDS_CONTRADICT,
                "HIGH",
                f"steam mod_id={mid} external_id={ext}",
                "align Steam workshop id fields",
            )
        if folder:
            p = Path(folder)
            if not p.is_dir():
                add(ORPHAN_DB_ROW, "HIGH", f"last_known_path missing: {folder}", "reconcile bind or mark missing")
        key = (plat, ext)
        if plat and ext and not is_provisional_external_id(ext):
            ext_map.setdefault(key, []).append(mid)
        nu = normalize_source_url(url)
        if nu:
            url_map.setdefault(nu, []).append(mid)

    for key, ids in ext_map.items():
        uniq = sorted(set(ids))
        if len(uniq) > 1:
            for mid in uniq:
                report.findings.append(
                    InvariantFinding(
                        entity_id=mid,
                        platform=key[0],
                        external_id=key[1],
                        violation_code=DUPLICATE_EXTERNAL_ID,
                        severity="CRITICAL",
                        evidence=f"duplicate (platform,external_id) ids={uniq}",
                        recommended_action="REMOVE_INVALID_DUPLICATE",
                    )
                )
    for url, ids in url_map.items():
        uniq = sorted(set(ids))
        if len(uniq) > 1:
            code = DUPLICATE_CANONICAL_URL if "steamcommunity.com" in url or "nexusmods.com" in url else DUPLICATE_SOURCE_URL
            for mid in uniq:
                report.findings.append(
                    InvariantFinding(
                        entity_id=mid,
                        source_url=url,
                        canonical_url=url,
                        violation_code=code,
                        severity="HIGH",
                        evidence=f"duplicate url mods={uniq}",
                        recommended_action="REMOVE_INVALID_DUPLICATE or SCRUB",
                    )
                )

    ws_map: dict[tuple[str, str], list[str]] = {}
    ws_any: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        mid = _text(row.get("mod_id"))
        plat = normalize_platform(_text(row.get("platform")))
        ws = _text(row.get("workspace_id"))
        if not ws:
            continue
        ws_map.setdefault((plat, ws), []).append(mid)
        ws_any.setdefault(ws, []).append((mid, plat))
    for (plat, ws), ids in ws_map.items():
        uniq = sorted(set(ids))
        if len(uniq) > 1:
            for mid in uniq:
                report.findings.append(
                    InvariantFinding(
                        entity_id=mid,
                        platform=plat,
                        workspace_id=ws,
                        violation_code=DUPLICATE_WORKSPACE_ID_CREATION,
                        severity="CRITICAL",
                        evidence=f"duplicate (platform, workspace_id) ids={uniq}",
                        recommended_action="bind renamed folder; refuse second INSERT",
                    )
                )
    for ws, pairs in ws_any.items():
        plats = {p for _mid, p in pairs}
        ids = sorted({mid for mid, _p in pairs})
        if len(ids) > 1 and len(plats) > 1:
            for mid, plat in pairs:
                report.findings.append(
                    InvariantFinding(
                        entity_id=mid,
                        platform=plat,
                        workspace_id=ws,
                        violation_code=DUPLICATE_WORKSPACE_ID_CREATION,
                        severity="CRITICAL",
                        evidence=(
                            f"same user-facing workspace_id={ws} on distinct "
                            f"internal ids={ids} platforms={sorted(plats)}"
                        ),
                        recommended_action="bind existing identity; do not mint Steam PK from Nexus workspace_id",
                    )
                )


def _scan_fs(root: Path, db: Any, report: InvariantScanReport) -> None:
    if not root.is_dir():
        return
    known: set[str] = set()
    try:
        for row in _rows(db, "SELECT CAST(mod_id AS TEXT) AS mid, last_known_path FROM mods"):
            known.add(_text(row.get("mid")))
    except sqlite3.Error:
        pass
    for game_dir in root.iterdir():
        if not game_dir.is_dir() or game_dir.name.startswith("."):
            continue
        for folder in game_dir.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            report.scanned_folders += 1
            meta = read_info_metadata_dict(folder) or {}
            pub = _text(meta.get("published_file_id"))
            url = _text(meta.get("url") or meta.get("source_url"))
            title = _text(meta.get("display_name") or meta.get("title") or folder.name)
            if pub and is_internal_mod_id(pub):
                plat = _text(meta.get("source_type") or meta.get("platform"))
                if normalize_platform(plat) == PLATFORM_STEAM or "steamcommunity.com" in url.lower():
                    report.findings.append(
                        InvariantFinding(
                            entity_id=pub,
                            platform=plat,
                            source_url=url,
                            display_name=title,
                            folder_path=str(folder),
                            violation_code=INVALID_STEAM_PUBLISHED_FILE_ID,
                            severity="CRITICAL",
                            evidence="sidecar published_file_id is internal id",
                            recommended_action="SCRUB published_file_id",
                        )
                    )
            stolen = _steam_url_uses_internal(url)
            if stolen:
                report.findings.append(
                    InvariantFinding(
                        entity_id=pub or folder.name,
                        source_url=url,
                        display_name=title,
                        folder_path=str(folder),
                        violation_code=INVALID_STEAM_SOURCE_URL,
                        severity="CRITICAL",
                        evidence=f"sidecar url embeds internal id ({stolen})",
                        recommended_action="SCRUB_POLLUTING_SOURCE_URL",
                    )
                )
            if is_empty_mod_placeholder(folder.name) or is_empty_mod_placeholder(title):
                from services.identity_service import has_official_platform_identity

                official = has_official_platform_identity(
                    platform=str(meta.get("source_type") or meta.get("platform") or ""),
                    external_id=str(meta.get("external_id") or "").strip(),
                    source_url=url,
                    workshop_id=str(meta.get("workspace_id") or "").strip(),
                )
                report.findings.append(
                    InvariantFinding(
                        entity_id=pub or folder.name,
                        display_name=title,
                        folder_path=str(folder),
                        violation_code=EMPTY_MOD_PLACEHOLDER,
                        severity="REQUIRES_REVIEW" if official else "HIGH",
                        evidence="Empty Mod placeholder folder",
                        recommended_action=(
                            "SCRUB_PLACEHOLDER_TITLE keep entity"
                            if official
                            else "bind official identity; do not mint"
                        ),
                    )
                )
            sidecar = folder / INFO_DIR_NAME / METADATA_FILENAME
            if sidecar.is_file() and pub and pub.isdigit() and pub not in known and not is_internal_mod_id(pub):
                report.findings.append(
                    InvariantFinding(
                        entity_id=pub,
                        folder_path=str(folder),
                        violation_code=ORPHAN_SIDECAR,
                        severity="HIGH",
                        evidence="sidecar published_file_id not in mods",
                        recommended_action="bind existing row; do not create",
                    )
                )
            if pub.isdigit() and pub in known:
                continue
            if not pub and folder.name.startswith("Unknown"):
                report.findings.append(
                    InvariantFinding(
                        entity_id=folder.name,
                        folder_path=str(folder),
                        display_name=title,
                        violation_code=ORPHAN_FOLDER,
                        severity="HIGH",
                        evidence="Unknown Mod folder without resolvable identity",
                        recommended_action="do not mint; unresolved",
                    )
                )


_UI_DIR_NAMES = ("ui",)
_FORBIDDEN_USER_UI_LABELS = (
    "内部 ID",
    "Steam Workshop ID",
    "Nexus Mod ID",
    "Nexus ID",
    "Mod ID:",
    "Mod ID：",
)
_ALLOWED_DEBUG_LABEL = "Internal Database ID"
_AMBIGUOUS_ASSIGN_RE = re.compile(
    r"workspace_id\s*=\s*(?:mid|internal_id|str\(\s*mod_id|str\(\s*mid)"
)


def _noncomment_source(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def scan_reconcile_identity_lifecycle(
    project_root: str | Path | None = None,
) -> list[InvariantFinding]:
    """Trace filesystem-discovery → match → create data flow (not name-only grep).

    Flags the class of bug where a folder rename loses exact-path match and
    reconcile mints a second Mod with the same user-facing Workspace ID.
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    findings: list[InvariantFinding] = []

    def add(path: Path, code: str, evidence: str, action: str) -> None:
        findings.append(
            InvariantFinding(
                entity_id=str(path.relative_to(root)).replace("\\", "/"),
                violation_code=code,
                severity="CRITICAL",
                evidence=evidence[:240],
                recommended_action=action,
            )
        )

    reconcile = root / "services" / "library_reconcile.py"
    identity = root / "services" / "mod_identity.py"
    authority = root / "services" / "mod_identity_authority.py"

    rec_text = reconcile.read_text(encoding="utf-8") if reconcile.is_file() else ""
    rec = _noncomment_source(rec_text)
    ident_text = identity.read_text(encoding="utf-8") if identity.is_file() else ""
    ident = _noncomment_source(ident_text)
    auth_text = authority.read_text(encoding="utf-8") if authority.is_file() else ""
    auth = _noncomment_source(auth_text)

    resolver = root / "services" / "mod_metadata_resolver.py"
    res_text = resolver.read_text(encoding="utf-8") if resolver.is_file() else ""
    res = _noncomment_source(res_text)

    create_at = rec.find("create_mod_identity(")
    resolve_at = rec.find("resolve_existing_mod_id(")
    ensure_at = rec.find("ensure_mod_identity(")

    if 'setdefault("published_file_id"' in rec or "setdefault('published_file_id'" in rec:
        add(
            reconcile,
            FILESYSTEM_PATH_USED_AS_SOLE_MOD_IDENTITY,
            "reconcile copies last_known_path match into published_file_id, "
            "using filesystem path as identity",
            "pass path as auxiliary evidence only; never write path into published_file_id",
        )

    if resolve_at >= 0 and create_at >= 0 and create_at < resolve_at:
        add(
            reconcile,
            RECONCILE_CREATE_BEFORE_IDENTITY_RESOLUTION,
            "create_mod_identity appears before resolve_existing_mod_id",
            "resolve existing durable identity before any CREATE",
        )
        add(
            reconcile,
            FOLDER_RENAME_CAUSES_NEW_MOD,
            "create runs before identity resolution so a renamed folder can INSERT",
            "rebind from sidecar workspace_id before create",
        )

    if create_at >= 0 and ensure_at >= 0 and create_at < ensure_at:
        add(
            reconcile,
            FOLDER_RENAME_CAUSES_NEW_MOD,
            "create_mod_identity runs before ensure_mod_identity bind",
            "bind existing entity from sidecar before create",
        )

    if "is_steam_workshop_id(mod_id)" in rec and "upsert_mod(" in rec:
        add(
            reconcile,
            UNSAFE_RECONCILE_IDENTITY_FALLBACK,
            "is_steam_workshop_id(mod_id) → upsert_mod uses any non-internal digit "
            "as Steam PK/workspace_id without proving Steam platform identity",
            "never upsert Steam from Nexus/other workspace_id; bind existing row",
        )
        add(
            reconcile,
            NUMERIC_ID_USED_AS_STEAM_PROOF,
            "reconcile treats is_steam_workshop_id(mod_id) as Steam create proof",
            "require explicit platform==steam plus legal Steam identity",
        )

    if "upsert_mod(" in rec:
        add(
            reconcile,
            RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF,
            "reconcile still calls upsert_mod, which can INSERT mods rows "
            "outside the Identity Creation Gate",
            "create only via create_mod_identity after existing-entity resolution",
        )

    pub_complete = (
        "if pub.isdigit() and not is_internal_mod_id(pub):" in ident
        and "identity_status" in ident
        and "return pub" in ident
        and "_lookup_workspace" not in ident
    )
    if pub_complete:
        add(
            identity,
            RECONCILE_CREATE_WITHOUT_IDENTITY_PROOF,
            "ensure_mod_identity returns published_file_id as complete identity "
            "when no mods row exists — Workspace ID digits become a Steam PK",
            "unmatched published_file_id is unresolved unless platform proof binds",
        )
        add(
            identity,
            PUBLISHED_FILE_ID_USED_AS_SOLE_IDENTITY,
            "published_file_id digits are treated as a complete Mod identity",
            "legacy fields may bind an existing entity only",
        )
        add(
            identity,
            LEGACY_ID_USED_AS_NEW_IDENTITY_PROOF,
            "legacy published_file_id is used as proof to mint a new identity",
            "legacy fields cannot create; only bind existing validated entities",
        )

    if "def resolve_mod_identity" in auth and "find_mod_by_workspace_id" not in auth:
        add(
            authority,
            DUPLICATE_WORKSPACE_ID_CREATION,
            "resolve_mod_identity/create_mod_identity never look up workspace_id; "
            "a second INSERT can reuse the same user-facing Workspace ID",
            "refuse create when workspace_id already bound; bind/rebind instead",
        )
    if (
        "create_mod_identity(" in rec
        and "find_mod_by_workspace_id" not in rec
        and "find_mod_by_workspace_id" not in ident
    ):
        add(
            reconcile,
            DUPLICATE_WORKSPACE_ID_CREATION,
            "reconcile create path does not query existing workspace_id before INSERT",
            "match workspace_id (+ platform/app_id) before create_mod_identity",
        )

    sidecar = root / "services" / "info_sidecar.py"
    side_text = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""
    side = _noncomment_source(side_text)
    ident_svc = root / "services" / "identity_service.py"
    svc_text = ident_svc.read_text(encoding="utf-8") if ident_svc.is_file() else ""
    persist_sanitizes = "source_url_embeds_internal" in svc_text
    reconcile_writes_url = (
        "update_mod_identity_fields(" in rec
        and "source_url=" in rec
        and "persist_identity(" not in rec
    )
    apply_start = side.find("def apply_sidecar_to_db")
    apply_body = side[apply_start : apply_start + 5000] if apply_start >= 0 else ""
    apply_copies_url = (
        'patch["source_url"] = sidecar.url' in apply_body
        and "source_url_embeds_internal" not in apply_body
    )
    if reconcile_writes_url and persist_sanitizes:
        add(
            reconcile,
            INVALID_SOURCE_URL_REHYDRATION,
            "reconcile writes payload url/source_url via update_mod_identity_fields, "
            "bypassing persist_identity Internal-ID Steam URL sanitizer",
            "route sidecar URL through persist_identity; reject id=<internal_id>",
        )
    if apply_copies_url:
        add(
            sidecar,
            SIDECAR_REHYDRATES_INVALID_IDENTITY,
            "apply_sidecar_to_db copies sidecar.url into mods.source_url without "
            "rejecting Internal-ID-derived Steam filedetails URLs",
            "validate sidecar URL against Identity Contract before DB write",
        )
        add(
            sidecar,
            INTERNAL_ID_LEAKED_TO_PLATFORM_URL,
            "sidecar Steam URL can embed Internal ID as published file id",
            "reject Internal ID in platform URLs",
        )
    apply_lacks_internal_url_guard = (
        apply_start >= 0
        and "sidecar.published_file_id" in apply_body
        and "is_internal_mod_id" not in apply_body
        and "source_url_embeds_internal" not in apply_body
    )
    if apply_lacks_internal_url_guard:
        add(
            sidecar,
            SIDECAR_INTERNAL_ID_USED_AS_PLATFORM_ID,
            "apply_sidecar_to_db uses sidecar.published_file_id and copies url "
            "without rejecting Internal ID as Steam Workshop / platform identity",
            "never treat Internal ID as published_file_id or Steam URL id",
        )
        add(
            sidecar,
            INTERNAL_ID_LEAKED_TO_PLATFORM_ID,
            "Internal ID from published_file_id used as platform identity",
            "strip Internal ID from sidecar published_file_id before lookup",
        )

    list_start = res.find("def list_visible_mods")
    list_body = res[list_start : list_start + 3500] if list_start >= 0 else ""
    if list_start >= 0 and "grouped" not in list_body and "out.append(resolved)" in list_body:
        add(
            resolver,
            MULTIPLE_VISIBLE_CARDS_ONE_INTERNAL_ID,
            "list_visible_mods emits one card per folder without grouping by Internal ID",
            "group filesystem observations by resolved internal entity before listing",
        )
        add(
            resolver,
            MULTIPLE_FILESYSTEM_PATHS_ONE_ENTITY,
            "multiple paths for one entity become multiple visible mods",
            "identity grouping / IDENTITY_CONFLICT instead of silent duplicate cards",
        )

    return findings


def scan_id_architecture_source(
    project_root: str | Path | None = None,
) -> list[InvariantFinding]:
    """Static scan for ID semantic violations in production source (not tests)."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    findings: list[InvariantFinding] = []
    ui_root = root / "ui"
    if ui_root.is_dir():
        for path in ui_root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for label in _FORBIDDEN_USER_UI_LABELS:
                if label not in text:
                    continue
                if label in {"内部 ID", "Internal ID"} and _ALLOWED_DEBUG_LABEL in text:
                    # Debug UI may mention Internal Database ID; still flag 内部 ID.
                    if label == "内部 ID":
                        pass
                    else:
                        continue
                findings.append(
                    InvariantFinding(
                        entity_id=str(path.relative_to(root)).replace("\\", "/"),
                        violation_code=INTERNAL_ID_EXPOSED_IN_USER_UI,
                        severity="CRITICAL",
                        evidence=f"user-facing label {label!r} in {path.name}",
                        recommended_action="show Workspace ID only in ordinary UI",
                    )
                )
    identity_files = (
        root / "services" / "identity_service.py",
        root / "services" / "mod_identity_authority.py",
        root / "core" / "mod_platform.py",
        root / "services" / "library_reconcile.py",
    )
    for path in identity_files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lower = stripped.lower()
            if "never" in lower or "must never" in lower or "do not" in lower:
                continue
            if _AMBIGUOUS_ASSIGN_RE.search(line):
                findings.append(
                    InvariantFinding(
                        entity_id=str(path.relative_to(root)).replace("\\", "/"),
                        violation_code=AMBIGUOUS_MOD_ID_USAGE,
                        severity="CRITICAL",
                        evidence=stripped[:160],
                        recommended_action="derive Workspace ID from external_id only",
                    )
                )
                break
    findings.extend(scan_reconcile_identity_lifecycle(root))
    findings.extend(scan_conflict_scheme_b(root))
    return findings


def scan_conflict_scheme_b(
    project_root: str | Path | None = None,
) -> list[InvariantFinding]:
    """Static Scheme B guards: path overlap is diagnostic, not a relationship."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    findings: list[InvariantFinding] = []
    path = root / "services" / "conflict.py"
    if not path.is_file():
        return findings
    text = path.read_text(encoding="utf-8")
    src = _noncomment_source(text)
    rel = str(path.relative_to(root)).replace("\\", "/")

    def add(code: str, evidence: str, action: str) -> None:
        findings.append(
            InvariantFinding(
                entity_id=rel,
                violation_code=code,
                severity="CRITICAL",
                evidence=evidence[:240],
                recommended_action=action,
            )
        )

    if "upsert_mod(" in src or re.search(r"INSERT\s+INTO\s+mods\b", src, re.I):
        add(
            CONFLICT_DETECTOR_CREATES_MOD,
            "ConflictDetector writes mods rows",
            "consume existing identity only; never INSERT mods from conflict scan",
        )
    if "create_mod_identity(" in src or "_ensure_mod_stub(" in src:
        add(
            CONFLICT_DETECTOR_CREATES_IDENTITY,
            "ConflictDetector calls identity create/stub",
            "conflict scan must not mint identity",
        )
    if "persist_workspace_id(" in src or "update_mod_identity_fields(" in src:
        add(
            CONFLICT_DETECTOR_MUTATES_WORKSPACE_ID,
            "ConflictDetector mutates identity/workspace fields",
            "read workspace_id for evidence only",
        )
    if "add_mod_relationship(" in src or re.search(
        r"INSERT\s+INTO\s+mod_relationships\b", src, re.I
    ):
        add(
            CONFLICT_DETECTOR_AUTO_CREATES_RELATIONSHIP,
            "ConflictDetector inserts mod_relationships",
            "relationships are user-declared only",
        )
    if re.search(
        r"ConflictType\.FILE_OVERWRITE\.value\s*,\s*\n?\s*ConflictType\.RELATIONSHIP\.value",
        src,
    ) or re.search(
        r"if c\.conflict_type\s+in\s*\([^)]*FILE_OVERWRITE[\s\S]{0,80}RELATIONSHIP",
        src,
    ):
        add(
            PATH_OVERLAP_AUTO_MEANS_CONFLICT,
            "FILE_OVERWRITE is grouped with RELATIONSHIP for conflict_status",
            "path overlap is diagnostic; only RELATIONSHIP sets conflict status",
        )
    if "conflict_status=status" in src or "conflict_status = status" in src:
        add(
            CONFLICT_SCAN_OVERWRITES_USER_RESOLUTION,
            "persist writes computed status including path-overlap conflict",
            "do not persist FILE_OVERWRITE as conflict_status; leave user resolution",
        )
    return findings


def classify_production_id_row(row: Mapping[str, Any] | Any) -> str:
    """Classify one mods row: VALID / NEEDS_REPAIR / CONFLICT / UNRESOLVED.

    Same numeric Internal/Workspace/external values are not automatically errors.
    Steam/Nexus: workspace must equal the platform id; Internal may coincide.
    """
    mid = _text(row.get("mod_id") if isinstance(row, Mapping) else getattr(row, "mod_id", ""))
    plat = normalize_platform(_text(row.get("platform") if isinstance(row, Mapping) else getattr(row, "platform", "")))
    ext = _text(row.get("external_id") if isinstance(row, Mapping) else getattr(row, "external_id", ""))
    ws = _text(row.get("workspace_id") if isinstance(row, Mapping) else getattr(row, "workspace_id", ""))
    url = _text(row.get("source_url") if isinstance(row, Mapping) else getattr(row, "source_url", ""))
    if is_internal_mod_id(ws) and ws == mid:
        return "NEEDS_REPAIR"
    if is_internal_mod_id(ext) and ext == mid:
        return "NEEDS_REPAIR"
    if url and is_internal_mod_id(mid) and f"id={mid}" in url.replace(" ", ""):
        return "NEEDS_REPAIR"
    if plat == PLATFORM_STEAM:
        if is_internal_mod_id(mid):
            return "CONFLICT"
        if ext and ext.isdigit() and not is_internal_mod_id(ext):
            if ws and ws != ext:
                return "CONFLICT"
            if not ws:
                return "NEEDS_REPAIR"
            return "VALID"
        if not ext and not url:
            return "UNRESOLVED"
        return "NEEDS_REPAIR"
    if plat == "nexus":
        if ext.isdigit() and not is_internal_mod_id(ext):
            if ws and ws != ext:
                return "CONFLICT"
            if not ws:
                return "NEEDS_REPAIR"
            if is_internal_mod_id(mid) and ws == mid:
                return "NEEDS_REPAIR"
            return "VALID"
        if not ext and not url:
            return "UNRESOLVED"
        return "NEEDS_REPAIR"
    if not ws:
        return "UNRESOLVED" if not ext else "NEEDS_REPAIR"
    return "VALID"


def audit_production_id_semantics(db: Any) -> dict[str, Any]:
    """Read-only production ID semantic audit. Never mutates."""
    counts = {"VALID": 0, "NEEDS_REPAIR": 0, "CONFLICT": 0, "UNRESOLVED": 0}
    rows_out: list[dict[str, Any]] = []
    try:
        raw = _rows(
            db,
            """
            SELECT mod_id, platform, external_id, workspace_id, source_url,
                   title, display_name, last_known_path
            FROM mods
            """,
        )
    except Exception:  # noqa: BLE001
        return {"counts": counts, "rows": [], "error": "query_failed"}
    for row in raw:
        status = classify_production_id_row(row)
        counts[status] = counts.get(status, 0) + 1
        item = {
            "internal_id": _text(row.get("mod_id")),
            "workspace_id": _text(row.get("workspace_id")),
            "platform": _text(row.get("platform")),
            "external_id": _text(row.get("external_id")),
            "source_url": _text(row.get("source_url")),
            "display_name": _text(row.get("display_name") or row.get("title")),
            "folder_path": _text(row.get("last_known_path")),
            "status": status,
        }
        if status != "VALID":
            rows_out.append(item)
    return {"counts": counts, "non_valid": rows_out, "scanned": len(raw)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only identity invariant scanner.")
    parser.add_argument("--readonly", action="store_true", default=True)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = scan_invalid_entities(args.library, args.db)
    text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 2 if report.to_dict().get("CRITICAL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
