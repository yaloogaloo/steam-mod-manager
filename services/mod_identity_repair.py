"""Identity repair: audit → classify → elect canonical → dry-run / apply → post-audit.

Never auto-deletes filesystem folders. Duplicate DB rows may be removed after
migrating deployment references when ``apply=True``.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.mod_platform import (
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_modio_api_mod_id,
    normalize_platform,
)
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)
from services.importers.duplicate_check import normalize_source_url
from services.mod_identity_authority import (
    ensure_non_polluted_workspace,
    log_identity_mutation,
    sanitize_platform_external_id,
    update_platform_identity,
)
from services.mod_identity_validator import (
    IdentityIssueCode,
    IdentitySeverity,
)
from services.mod_library_integrity_audit import (
    LibraryIntegrityReport,
    audit_mod_library_integrity,
)

logger = logging.getLogger(__name__)


@dataclass
class CanonicalCandidate:
    mod_id: str
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    app_id: int = 0
    folder_present: int = 0
    last_known_path: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class RepairAction:
    action: str
    canonical_mod_id: str = ""
    duplicate_mod_id: str = ""
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairPlan:
    actions: list[RepairAction] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    applied: bool = False
    success: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [asdict(a) for a in self.actions],
            "before": self.before,
            "after": self.after,
            "applied": self.applied,
            "success": self.success,
            "error": self.error,
            "notes": self.notes,
        }


def audit_severity_counts(report: LibraryIntegrityReport) -> dict[str, int]:
    """Map findings to CRITICAL/HIGH/MEDIUM/LOW plus legacy buckets."""
    counts = {
        "CRITICAL": 0,
        "HIGH": 0,
        "MEDIUM": 0,
        "LOW": 0,
        "corrupted": 0,
        "duplicate": 0,
        "conflict": 0,
        "orphan": 0,
        "warning": 0,
        "identity_conflict": 0,
        "ghost": 0,
    }
    findings = list(report.global_findings)
    for mr in report.mod_reports:
        findings.extend(mr.findings)
    for f in findings:
        sev = f.severity
        if sev == IdentitySeverity.CORRUPTED:
            counts["CRITICAL"] += 1
            counts["corrupted"] += 1
        elif sev == IdentitySeverity.DUPLICATE:
            if f.code == IdentityIssueCode.DUPLICATE_DIRECTORY_IDENTITY:
                # Same internal identity bound to multiple live folders — retained
                # on purpose (no auto-delete). Treated as MEDIUM with explicit mark.
                counts["MEDIUM"] += 1
                counts["duplicate"] += 1
                counts["identity_conflict"] += 1
            else:
                counts["HIGH"] += 1
                counts["duplicate"] += 1
        elif sev == IdentitySeverity.CONFLICT:
            counts["HIGH"] += 1
            counts["conflict"] += 1
        elif sev == IdentitySeverity.ORPHAN:
            # Orphan/ghost without identity pollution is LOW (informational).
            counts["LOW"] += 1
            counts["orphan"] += 1
            if "without filesystem" in f.message:
                counts["ghost"] += 1
        elif sev == IdentitySeverity.WARNING:
            if f.code in (
                IdentityIssueCode.INTERNAL_ID_AS_EXTERNAL_ID,
                IdentityIssueCode.MODIO_ID_POLLUTION,
                IdentityIssueCode.STEAM_ID_POLLUTION,
                IdentityIssueCode.WORKSPACE_ID_POLLUTION,
            ):
                counts["CRITICAL"] += 1
            elif f.code in (
                IdentityIssueCode.MISSING_PLATFORM_ID,
                IdentityIssueCode.INVALID_APP_ID,
            ):
                counts["MEDIUM"] += 1
            else:
                counts["LOW"] += 1
            counts["warning"] += 1
    return counts


def _platform_id_from_url(platform: str, url: str) -> str:
    plat = normalize_platform(platform)
    text = str(url or "").strip()
    if not text:
        return ""
    if plat == PLATFORM_NEXUS:
        match = re.search(r"/mods/(\d+)", text.replace("\\", "/"))
        return match.group(1) if match else ""
    if plat == PLATFORM_MODIO:
        # Prefer trailing name_id slug; numeric recovery happens via API elsewhere.
        return text.rstrip("/").rsplit("/", 1)[-1]
    return ""


def score_candidate(row: dict[str, Any]) -> CanonicalCandidate:
    """Higher score = better canonical."""
    mid = str(row["mod_id"])
    plat = normalize_platform(row.get("platform") or "")
    ext = str(row.get("external_id") or "").strip()
    url = normalize_source_url(str(row.get("source_url") or ""))
    cand = CanonicalCandidate(
        mod_id=mid,
        platform=plat,
        external_id=ext,
        source_url=url,
        app_id=int(row.get("app_id") or 0),
        folder_present=int(row.get("folder_present") or 0),
        last_known_path=str(row.get("last_known_path") or ""),
    )
    score = 0
    expected = _platform_id_from_url(plat, url)
    if ext and not is_internal_mod_id(ext) and not ext.startswith("stub:"):
        score += 50
        cand.reasons.append("has_platform_external")
    if expected and ext == expected:
        score += 40
        cand.reasons.append("external_matches_url")
    if plat == PLATFORM_NEXUS and ext.isdigit():
        score += 30
        cand.reasons.append("nexus_numeric")
    if plat == PLATFORM_MODIO and (is_modio_api_mod_id(ext) or (ext and not is_internal_mod_id(ext))):
        score += 20
        cand.reasons.append("modio_plausible")
    if cand.folder_present:
        score += 10
        cand.reasons.append("folder_present")
    if cand.app_id > 0:
        score += 5
        cand.reasons.append("has_app_id")
    if is_internal_mod_id(ext) or ext == mid:
        score -= 100
        cand.reasons.append("polluted_external")
    # Prefer older id slightly when tie (stable).
    try:
        score -= int(mid[-4:]) % 7
    except Exception:  # noqa: BLE001
        pass
    cand.score = score
    return cand


def elect_canonical(rows: list[dict[str, Any]]) -> tuple[CanonicalCandidate, list[CanonicalCandidate]]:
    scored = [score_candidate(r) for r in rows]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[0], scored[1:]


def build_repair_plan(db, library_root: str | Path) -> RepairPlan:
    """Classify issues and build a dry-run plan (no mutations)."""
    root = Path(library_root)
    report = audit_mod_library_integrity(root, db=db)
    plan = RepairPlan(before=audit_severity_counts(report))

    # --- Pollution scrub ---
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT mod_id, platform, external_id, workspace_id, source_url, app_id,
                   folder_present, last_known_path
            FROM mods
            """
        ).fetchall()
        rows = [dict(r) for r in rows]

    for row in rows:
        mid = str(row["mod_id"])
        plat = normalize_platform(row["platform"] or "")
        ext = str(row["external_id"] or "").strip()
        ws = str(row["workspace_id"] or "").strip()
        url = normalize_source_url(str(row["source_url"] or ""))
        if is_internal_mod_id(mid) and (ext == mid or is_internal_mod_id(ext)):
            recovered = _platform_id_from_url(plat, url)
            plan.actions.append(
                RepairAction(
                    action="scrub_polluted_external_id",
                    canonical_mod_id=mid,
                    reason="external_id equals internal mod_id",
                    details={
                        "old_external_id": ext,
                        "new_external_id": recovered,
                        "source_url": url,
                        "platform": plat,
                    },
                )
            )
        if is_internal_mod_id(mid) and plat != PLATFORM_STEAM and ws == mid:
            plan.actions.append(
                RepairAction(
                    action="scrub_polluted_workspace_id",
                    canonical_mod_id=mid,
                    reason="workspace_id equals internal mod_id",
                    details={"old_workspace_id": ws, "platform": plat},
                )
            )

        if is_internal_mod_id(mid) and plat == PLATFORM_STEAM:
            inferred = ""
            if "mod.io" in url.lower():
                inferred = PLATFORM_MODIO
            elif "nexusmods.com" in url.lower():
                inferred = "nexus"
            elif "github.com" in url.lower():
                inferred = "github"
            else:
                inferred = "other"
            plan.actions.append(
                RepairAction(
                    action="fix_steam_on_internal_id",
                    canonical_mod_id=mid,
                    reason="internal mod_id incorrectly marked platform=steam",
                    details={
                        "old_platform": plat,
                        "new_platform": inferred,
                        "source_url": url,
                        "workspace_id": ws,
                    },
                )
            )

    # --- Duplicate source URLs ---
    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        url = normalize_source_url(str(row.get("source_url") or ""))
        if url:
            by_url[url].append(row)
    for url, items in by_url.items():
        unique_ids = {str(i["mod_id"]) for i in items}
        if len(unique_ids) < 2:
            continue
        canonical, dups = elect_canonical(items)
        plan.actions.append(
            RepairAction(
                action="duplicate_source_url",
                canonical_mod_id=canonical.mod_id,
                reason=f"same source_url → {len(unique_ids)} mod_ids",
                details={
                    "source_url": url,
                    "canonical_score": canonical.score,
                    "canonical_reasons": canonical.reasons,
                    "duplicates": [c.mod_id for c in dups],
                    "duplicate_scores": {c.mod_id: c.score for c in dups},
                },
            )
        )
        for dup in dups:
            plan.actions.append(
                RepairAction(
                    action="retire_duplicate_entity",
                    canonical_mod_id=canonical.mod_id,
                    duplicate_mod_id=dup.mod_id,
                    reason="elect_canonical",
                    details={
                        "source_url": url,
                        "duplicate_path": dup.last_known_path,
                        "canonical_path": canonical.last_known_path,
                        "filesystem_policy": "retain_folders_bind_to_canonical",
                    },
                )
            )

    plan.notes.append(f"planned_actions={len(plan.actions)}")
    return plan


def _migrate_deployment_items(db, *, from_mod_id: str, to_mod_id: str) -> int:
    src = int(from_mod_id)
    dst = int(to_mod_id)
    moved = 0
    with db._lock:
        rows = db._conn.execute(
            "SELECT record_id FROM deployment_record_items WHERE mod_id = ?",
            (src,),
        ).fetchall()
        for row in rows:
            rid = int(row["record_id"])
            exists = db._conn.execute(
                "SELECT 1 FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                (rid, dst),
            ).fetchone()
            if exists:
                db._conn.execute(
                    "DELETE FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                    (rid, src),
                )
            else:
                db._conn.execute(
                    "UPDATE deployment_record_items SET mod_id=? WHERE record_id=? AND mod_id=?",
                    (dst, rid, src),
                )
            moved += 1
        db._conn.commit()
    return moved


def _bind_folder_to_canonical(folder: Path, canonical_mod_id: str) -> bool:
    if not folder.is_dir():
        return False
    meta = read_info_metadata_dict(folder) or {}
    pub = str(meta.get("published_file_id") or "").strip()
    if pub == canonical_mod_id:
        return False
    meta["published_file_id"] = canonical_mod_id
    persist_unified_metadata_dict(
        folder, meta, sync_backup=False, sync_reason="identity_repair"
    )
    return True


def apply_repair_plan(
    db,
    library_root: str | Path,
    plan: RepairPlan | None = None,
    *,
    apply: bool = False,
) -> RepairPlan:
    """
    Dry-run (default) or apply a repair plan, then post-validate.

    ``apply=False`` never mutates. ``apply=True`` mutates DB + metadata binding
    but never deletes user folders.
    """
    root = Path(library_root)
    working = plan or build_repair_plan(db, root)
    if not apply:
        working.applied = False
        working.success = True
        working.notes.append("dry_run_only")
        return working

    working.applied = True
    try:
        # Phase order matters: retire duplicates before scrubbing so recovered
        # platform ids do not collide with UNIQUE(platform, app_id, external_id).
        ordered = sorted(
            working.actions,
            key=lambda a: {
                "retire_duplicate_entity": 0,
                "duplicate_source_url": 1,
                "fix_steam_on_internal_id": 2,
                "scrub_polluted_workspace_id": 3,
                "scrub_polluted_external_id": 4,
            }.get(a.action, 9),
        )
        for action in ordered:
            if action.action == "scrub_polluted_external_id":
                mid = action.canonical_mod_id
                if db.get_mod_display_info(mid) is None:
                    continue
                new_ext = str(action.details.get("new_external_id") or "").strip()
                plat = str(action.details.get("platform") or "")
                url = str(action.details.get("source_url") or "")
                before = db.get_mod_display_info(mid)
                old_ext = str(before.external_id or "") if before else ""
                from core.db_manager import _utc_now

                if new_ext:
                    # Skip recovery write when another row already owns this identity.
                    conflict = db.find_mod_by_external(
                        plat or (before.platform if before else ""),
                        new_ext,
                        app_id=int(before.app_id or 0) if before else 0,
                    )
                    if conflict is not None and str(conflict.mod_id) != str(mid):
                        new_ext = ""
                        action.details["recovery_skipped_unique_conflict"] = True
                with db._lock:
                    db._conn.execute(
                        """
                        UPDATE mods SET
                            external_id = ?,
                            platform = CASE WHEN TRIM(?) != '' THEN ? ELSE platform END,
                            source_url = CASE WHEN TRIM(?) != '' THEN ? ELSE source_url END,
                            updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (new_ext, plat, plat, url, url, _utc_now(), int(mid)),
                    )
                    db._conn.commit()
                log_identity_mutation(
                    db,
                    mod_id=mid,
                    field_name="external_id",
                    old_value=old_ext,
                    new_value=new_ext,
                    source="identity_repair",
                    reason="scrub_polluted_external_id",
                )
            elif action.action == "fix_steam_on_internal_id":
                mid = action.canonical_mod_id
                new_plat = str(action.details.get("new_platform") or "other")
                from core.db_manager import _utc_now
                from core.mod_platform import generate_unique_workspace_id

                before = db.get_mod_display_info(mid)
                old_plat = str(before.platform or "") if before else ""
                old_ws = str(before.workspace_id or "") if before else ""
                new_ws = old_ws
                if old_ws == mid or is_internal_mod_id(old_ws):
                    new_ws = generate_unique_workspace_id()
                with db._lock:
                    db._conn.execute(
                        """
                        UPDATE mods SET
                            platform = ?,
                            workspace_id = ?,
                            updated_at = ?
                        WHERE mod_id = ?
                        """,
                        (new_plat, new_ws, _utc_now(), int(mid)),
                    )
                    db._conn.commit()
                log_identity_mutation(
                    db,
                    mod_id=mid,
                    field_name="platform",
                    old_value=old_plat,
                    new_value=new_plat,
                    source="identity_repair",
                    reason="fix_steam_on_internal_id",
                )
            elif action.action == "scrub_polluted_workspace_id":
                ensure_non_polluted_workspace(db, action.canonical_mod_id)
            elif action.action == "retire_duplicate_entity":
                can = action.canonical_mod_id
                # skip if already deleted
                if db.get_mod_display_info(can) is None and db.get_mod(can) is None:
                    continue
                dup = action.duplicate_mod_id
                if not can or not dup or can == dup:
                    continue
                if db.get_mod_display_info(dup) is None and db.get_mod(dup) is None:
                    continue
                moved = _migrate_deployment_items(db, from_mod_id=dup, to_mod_id=can)
                action.details["deployment_items_migrated"] = moved
                with db._lock:
                    row = db._conn.execute(
                        "SELECT last_known_path FROM mods WHERE mod_id=?",
                        (int(dup),),
                    ).fetchone()
                path = (
                    Path(str(row["last_known_path"]))
                    if row and row["last_known_path"]
                    else None
                )
                if path and path.is_dir():
                    bound = _bind_folder_to_canonical(path, can)
                    action.details["folder_bound"] = bound
                    action.details["folder"] = str(path)
                db.delete_mod_record(dup)
                log_identity_mutation(
                    db,
                    mod_id=dup,
                    field_name="mod_id",
                    old_value=dup,
                    new_value=can,
                    source="identity_repair",
                    reason="retire_duplicate_entity",
                )
            elif action.action == "duplicate_source_url":
                continue
            elif action.action == "scrub_polluted_external_id":
                # already handled above in ordered loop - keep structure
                pass

        after_report = audit_mod_library_integrity(root, db=db)
        working.after = audit_severity_counts(after_report)
        before_c = working.before.get("CRITICAL", 0) + working.before.get("HIGH", 0)
        after_c = working.after.get("CRITICAL", 0) + working.after.get("HIGH", 0)
        if working.after.get("CRITICAL", 0) > working.before.get("CRITICAL", 0):
            working.success = False
            working.error = "REPAIR FAILED: CRITICAL count increased"
        elif after_c > before_c and working.after.get("duplicate", 0) > working.before.get(
            "duplicate", 0
        ):
            working.success = False
            working.error = "REPAIR FAILED: duplicates increased"
        else:
            working.success = True
            working.notes.append(
                f"post_audit CRITICAL={working.after.get('CRITICAL')} "
                f"HIGH={working.after.get('HIGH')}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("repair apply failed")
        working.success = False
        working.error = f"REPAIR FAILED: {exc}"
    return working


def repair_mod_library_identity(
    library_root: str | Path,
    *,
    db=None,
    apply: bool = False,
) -> RepairPlan:
    """Public entry: build plan, optionally apply, always attach post counts when apply."""
    from core.db_manager import get_db

    database = db or get_db()
    plan = build_repair_plan(database, library_root)
    return apply_repair_plan(database, library_root, plan, apply=apply)
