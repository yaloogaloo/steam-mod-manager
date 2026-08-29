"""Read-only library-wide Mod identity / filesystem integrity audit."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.mod_platform import normalize_platform
from services.file_ops import ModFileManager, read_info_metadata_dict
from services.importers.duplicate_check import normalize_source_url
from services.mod_identity_validator import (
    IdentityFinding,
    IdentityIssueCode,
    IdentitySeverity,
    ModIdentityReport,
    validate_db_row_identity,
    validate_mod_identity,
)

logger = logging.getLogger(__name__)


@dataclass
class LibraryIntegrityReport:
    scanned_folders: int = 0
    scanned_db_rows: int = 0
    healthy: int = 0
    warning: int = 0
    conflict: int = 0
    corrupted: int = 0
    duplicate: int = 0
    orphan: int = 0
    mod_reports: list[ModIdentityReport] = field(default_factory=list)
    global_findings: list[IdentityFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return str(value or "").strip()


def audit_mod_library_integrity(
    library_root: str | Path,
    *,
    db=None,
) -> LibraryIntegrityReport:
    """
    Scan DB + ``mod/<game>/<mod>/`` + metadata sidecars.

    Pure read-only — never repairs or deletes.
    """
    from core.db_manager import get_db

    root = Path(library_root).expanduser().resolve()
    db = db or get_db()
    report = LibraryIntegrityReport()

    manager = ModFileManager(root)
    id_to_folders: dict[str, list[Path]] = defaultdict(list)
    url_to_mod_ids: dict[str, list[str]] = defaultdict(list)
    platform_ext_to_mod_ids: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    folder_to_mod_id: dict[str, str] = {}

    for folder in manager.list_managed_mods():
        report.scanned_folders += 1
        meta = read_info_metadata_dict(folder) or {}
        pub = _text(meta.get("published_file_id"))
        mid = pub if pub.isdigit() else ""

        if not mid:
            from services.mod_identity import resolve_existing_mod_id

            mid = resolve_existing_mod_id(meta)

        if mid:
            id_to_folders[mid].append(folder)
            folder_to_mod_id[str(folder.resolve())] = mid

        row = None
        if mid:
            try:
                row = db.get_mod(mid)
            except Exception:  # noqa: BLE001
                row = None

        db_dict: dict[str, Any] = {}
        if row is not None:
            db_dict = {
                "platform": getattr(row, "platform", "") or "",
                "external_id": getattr(row, "external_id", "") or "",
                "source_url": getattr(row, "url", "") or "",
                "app_id": int(getattr(row, "app_id", 0) or 0),
                "workspace_id": getattr(row, "workspace_id", "") or "",
            }
        elif mid:
            try:
                info = db.get_mod_display_info(mid)
                if info is not None:
                    db_dict = {
                        "platform": info.platform or "",
                        "external_id": info.external_id or "",
                        "source_url": info.source_url or "",
                        "app_id": int(info.app_id or 0),
                        "workspace_id": info.workspace_id or "",
                    }
            except Exception:  # noqa: BLE001
                pass

        mod_report = validate_mod_identity(
            mod_id=mid or "unknown",
            folder=str(folder),
            db_row=db_dict or None,
            metadata=meta,
        )
        if not mid:
            mod_report.findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.MISSING_PLATFORM_ID,
                    severity=IdentitySeverity.ORPHAN,
                    message="folder has no resolvable mod_id",
                    folder=str(folder),
                )
            )
        report.mod_reports.append(mod_report)

        url = normalize_source_url(
            _text(db_dict.get("source_url"))
            or _text(meta.get("url") or meta.get("source_url"))
        )
        if url and mid:
            url_to_mod_ids[url].append(mid)

        plat = normalize_platform(_text(db_dict.get("platform") or meta.get("platform")))
        ext = _text(db_dict.get("external_id") or meta.get("external_id"))
        aid = int(db_dict.get("app_id") or meta.get("app_id") or 0)
        if plat and ext and mid and not ext.startswith("stub:"):
            platform_ext_to_mod_ids[(plat, aid, ext)].append(mid)

    # DB rows without folders
    try:
        with db._lock:
            rows = db._conn.execute(
                """
                SELECT mod_id, platform, external_id, source_url, app_id,
                       last_known_path, folder_present, workspace_id
                FROM mods
                """
            ).fetchall()
        report.scanned_db_rows = len(rows)
        for row in rows:
            mid = _text(row["mod_id"])
            path = _text(row["last_known_path"])
            folder_present = int(row["folder_present"] or 0)
            # Validate every DB row identity (not only folders with metadata).
            for finding in validate_db_row_identity(
                mod_id=mid,
                platform=_text(row["platform"]),
                external_id=_text(row["external_id"]),
                source_url=_text(row["source_url"]),
                app_id=int(row["app_id"] or 0),
                workspace_id=_text(row["workspace_id"]),
            ):
                report.global_findings.append(finding)
            resolved_path = str(Path(path).resolve()) if path else ""
            on_disk = resolved_path in folder_to_mod_id if resolved_path else False
            if folder_present and not on_disk and mid not in id_to_folders:
                report.global_findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.MISSING_PLATFORM_ID,
                        severity=IdentitySeverity.ORPHAN,
                        message="DB row marked folder_present but path not in scan",
                        mod_id=mid,
                        folder=path,
                    )
                )
            if mid and mid not in id_to_folders and not folder_present:
                report.global_findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.MISSING_PLATFORM_ID,
                        severity=IdentitySeverity.ORPHAN,
                        message="DB row without filesystem folder",
                        mod_id=mid,
                        folder=path,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB scan failed during integrity audit: %s", exc)
        report.notes.append(f"DB scan error: {exc}")

    # Duplicate internal mod_id → multiple folders
    for mid, folders in id_to_folders.items():
        if len(folders) > 1:
            report.global_findings.append(
                IdentityFinding(
                    code=IdentityIssueCode.DUPLICATE_DIRECTORY_IDENTITY,
                    severity=IdentitySeverity.DUPLICATE,
                    message=f"mod_id {mid} bound to {len(folders)} directories",
                    mod_id=mid,
                    folder="; ".join(str(f) for f in folders),
                )
            )

    # DB-level duplicate source_url / platform identity (includes rows without folders)
    try:
        with db._lock:
            db_rows = db._conn.execute(
                """
                SELECT mod_id, platform, external_id, source_url, app_id
                FROM mods
                WHERE source_url IS NOT NULL AND TRIM(source_url) != ''
                """
            ).fetchall()
        db_url_to_mids: dict[str, list[str]] = defaultdict(list)
        db_plat_ext_to_mids: dict[tuple[str, int, str], list[str]] = defaultdict(list)
        for row in db_rows:
            mid = _text(row["mod_id"])
            url = normalize_source_url(_text(row["source_url"]))
            if url and mid:
                db_url_to_mids[url].append(mid)
            plat = normalize_platform(_text(row["platform"]))
            ext = _text(row["external_id"])
            aid = int(row["app_id"] or 0)
            if plat and ext and mid and not ext.startswith("stub:"):
                db_plat_ext_to_mids[(plat, aid, ext)].append(mid)
        for url, mids in db_url_to_mids.items():
            unique = sorted(set(mids))
            if len(unique) > 1:
                report.global_findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.DUPLICATE_SOURCE_URL,
                        severity=IdentitySeverity.DUPLICATE,
                        message=f"source_url maps to {len(unique)} mod_ids (DB)",
                        source_url=url,
                        expected=unique[0],
                        actual=", ".join(unique),
                    )
                )
        for key, mids in db_plat_ext_to_mids.items():
            unique = sorted(set(mids))
            if len(unique) > 1:
                plat, aid, ext = key
                report.global_findings.append(
                    IdentityFinding(
                        code=IdentityIssueCode.DUPLICATE_PLATFORM_ID,
                        severity=IdentitySeverity.DUPLICATE,
                        message=f"platform identity maps to {len(unique)} mod_ids (DB)",
                        mod_id=unique[0],
                        platform=plat,
                        external_id=ext,
                        expected=unique[0],
                        actual=", ".join(unique),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB duplicate scan failed: %s", exc)

    # Tally severities
    all_findings: list[IdentityFinding] = list(report.global_findings)
    for mr in report.mod_reports:
        all_findings.extend(mr.findings)

    severity_counts = defaultdict(int)
    for f in all_findings:
        severity_counts[f.severity] += 1

    report.healthy = max(0, report.scanned_folders - len({f.mod_id or f.folder for f in all_findings if f.severity != IdentitySeverity.HEALTHY}))
    report.warning = severity_counts[IdentitySeverity.WARNING]
    report.conflict = severity_counts[IdentitySeverity.CONFLICT]
    report.corrupted = severity_counts[IdentitySeverity.CORRUPTED]
    report.duplicate = severity_counts[IdentitySeverity.DUPLICATE]
    report.orphan = severity_counts[IdentitySeverity.ORPHAN]

    return report
