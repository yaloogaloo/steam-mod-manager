"""Read-only invalid-entity scanner. Never mutates production."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
                INVALID_STEAM_WORKSPACE,
                "CRITICAL",
                "workspace_id equals internal mod_id",
                "SCRUB workspace_id",
            )
        if is_internal_mod_id(ext) or (ext == mid and is_internal_mod_id(mid) and plat == PLATFORM_STEAM):
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
                    platform=str(raw.get("source_type") or raw.get("platform") or ""),
                    external_id=str(raw.get("external_id") or "").strip(),
                    source_url=url,
                    workshop_id=str(raw.get("workspace_id") or "").strip(),
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
