"""Unified Identity Repair entry — detect / validate / repair / report.

Architecture convergence only. Repair never creates identity, never rewrites
surviving ``mods.mod_id``, and never writes Internal ID into
``published_file_id``.

Implementation engines retained under this facade:
  - ``services.identity_repair`` — entity ghost / invalid-duplicate / quarantine
  - ``services.identity_pollution`` — detect-only pollution scan
  - field scrub / canonical election (migrated from deleted ``mod_identity_repair``)

All mutating apply paths run under ``repair_no_allocate_scope`` and prefer
IdentityService / authority helpers for field writes.
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
    generate_unique_workspace_id,
    is_internal_mod_id,
    is_modio_api_mod_id,
    normalize_platform,
)
from services.identity_pollution import (
    IdentityPollutionReport,
    plan_identity_pollution_repair,
    scan_identity_pollution,
)
from services.identity_repair import (
    RepairPlan as EntityRepairPlan,
    apply_identity_repair,
    format_repair_plan,
    plan_identity_repair,
)
from services.identity_service import (
    RepairMustNotAllocateError,
    persist_identity,
    repair_no_allocate_scope,
)
from services.importers.duplicate_check import normalize_source_url
from services.mod_identity_authority import (
    ensure_non_polluted_workspace,
    log_identity_mutation,
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


# ---------------------------------------------------------------------------
# Severity / election helpers (migrated from mod_identity_repair)
# ---------------------------------------------------------------------------


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
class FieldRepairAction:
    action: str
    canonical_mod_id: str = ""
    duplicate_mod_id: str = ""
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _platform_id_from_url(platform: str, url: str) -> str:
    plat = normalize_platform(platform)
    text = str(url or "").strip()
    if not text:
        return ""
    if plat == PLATFORM_NEXUS:
        match = re.search(r"/mods/(\d+)", text.replace("\\", "/"))
        return match.group(1) if match else ""
    if plat == PLATFORM_MODIO:
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
    if plat == PLATFORM_MODIO and (
        is_modio_api_mod_id(ext) or (ext and not is_internal_mod_id(ext))
    ):
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
    try:
        score -= int(mid[-4:]) % 7
    except Exception:  # noqa: BLE001
        pass
    cand.score = score
    return cand


def elect_canonical(
    rows: list[dict[str, Any]],
) -> tuple[CanonicalCandidate, list[CanonicalCandidate]]:
    scored = [score_candidate(r) for r in rows]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[0], scored[1:]


def plan_field_scrubs(db: Any, library_root: str | Path) -> list[FieldRepairAction]:
    """Detect polluted fields / duplicate URLs (no mutations)."""
    root = Path(library_root)
    _ = root  # library scoped for API symmetry / future FS checks
    actions: list[FieldRepairAction] = []
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
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
        external_polluted = ext == mid and (
            is_internal_mod_id(mid) or plat != PLATFORM_STEAM
        )
        if external_polluted or (is_internal_mod_id(mid) and is_internal_mod_id(ext)):
            recovered = _platform_id_from_url(plat, url)
            actions.append(
                FieldRepairAction(
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
        workspace_polluted = ws == mid and (
            is_internal_mod_id(mid) or plat != PLATFORM_STEAM
        )
        if workspace_polluted:
            actions.append(
                FieldRepairAction(
                    action="scrub_polluted_workspace_id",
                    canonical_mod_id=mid,
                    reason="workspace_id equals internal mod_id",
                    details={"old_workspace_id": ws, "platform": plat},
                )
            )
        if is_internal_mod_id(mid) and plat == PLATFORM_STEAM:
            url_l = url.lower()
            steamish = (
                not url
                or "steamcommunity.com" in url_l
                or "steampowered.com" in url_l
            )
            if steamish:
                continue
            if "mod.io" in url_l:
                inferred = PLATFORM_MODIO
            elif "nexusmods.com" in url_l:
                inferred = PLATFORM_NEXUS
            elif "github.com" in url_l:
                inferred = "github"
            else:
                inferred = "other"
            actions.append(
                FieldRepairAction(
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
        for dup in dups:
            actions.append(
                FieldRepairAction(
                    action="retire_duplicate_entity",
                    canonical_mod_id=canonical.mod_id,
                    duplicate_mod_id=dup.mod_id,
                    reason="elect_canonical",
                    details={
                        "source_url": url,
                        "duplicate_path": dup.last_known_path,
                        "canonical_path": canonical.last_known_path,
                        "filesystem_policy": "retain_folders_no_pfi_rewrite",
                    },
                )
            )
    return actions


def _migrate_deployment_items(db: Any, *, from_mod_id: str, to_mod_id: str) -> int:
    src = int(from_mod_id)
    dst = int(to_mod_id)
    moved = 0
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT record_id FROM deployment_record_items WHERE mod_id = ?",
            (src,),
        ).fetchall()
        for row in rows:
            rid = int(row["record_id"])
            exists = db._conn.execute(  # noqa: SLF001
                "SELECT 1 FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                (rid, dst),
            ).fetchone()
            if exists:
                db._conn.execute(  # noqa: SLF001
                    "DELETE FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                    (rid, src),
                )
            else:
                db._conn.execute(  # noqa: SLF001
                    "UPDATE deployment_record_items SET mod_id=? "
                    "WHERE record_id=? AND mod_id=?",
                    (dst, rid, src),
                )
            moved += 1
        db._conn.commit()  # noqa: SLF001
    return moved


def _apply_field_scrubs(db: Any, actions: list[FieldRepairAction]) -> dict[str, Any]:
    """Apply safe field / duplicate retirement. Never writes PFI = Internal ID."""
    result: dict[str, Any] = {"applied": [], "skipped": []}
    ordered = sorted(
        actions,
        key=lambda a: {
            "retire_duplicate_entity": 0,
            "fix_steam_on_internal_id": 1,
            "scrub_polluted_workspace_id": 2,
            "scrub_polluted_external_id": 3,
        }.get(a.action, 9),
    )
    for action in ordered:
        if action.action == "scrub_polluted_external_id":
            mid = action.canonical_mod_id
            if db.get_mod_display_info(mid) is None:
                result["skipped"].append({"action": action.action, "mod_id": mid})
                continue
            new_ext = str(action.details.get("new_external_id") or "").strip()
            plat = str(action.details.get("platform") or "")
            url = str(action.details.get("source_url") or "")
            before = db.get_mod_display_info(mid)
            old_ext = str(before.external_id or "") if before else ""
            if new_ext:
                conflict = db.find_mod_by_external(
                    plat or (before.platform if before else ""),
                    new_ext,
                    app_id=int(before.app_id or 0) if before else 0,
                )
                if conflict is not None and str(conflict.mod_id) != str(mid):
                    new_ext = ""
                    action.details["recovery_skipped_unique_conflict"] = True
            persist_identity(
                db,
                mid,
                source="identity_repair_service",
                reason="scrub_polluted_external_id",
                external_id=new_ext,
                platform=plat or None,
                source_url=url or None,
            )
            log_identity_mutation(
                db,
                mod_id=mid,
                field_name="external_id",
                old_value=old_ext,
                new_value=new_ext,
                source="identity_repair_service",
                reason="scrub_polluted_external_id",
            )
            result["applied"].append(
                {"action": action.action, "mod_id": mid, "external_id": new_ext}
            )
        elif action.action == "fix_steam_on_internal_id":
            mid = action.canonical_mod_id
            new_plat = str(action.details.get("new_platform") or "other")
            before = db.get_mod_display_info(mid)
            if before is None:
                result["skipped"].append({"action": action.action, "mod_id": mid})
                continue
            old_plat = str(before.platform or "")
            old_ws = str(before.workspace_id or "")
            new_ws = old_ws
            if old_ws == mid or is_internal_mod_id(old_ws):
                new_ws = generate_unique_workspace_id()
            persist_identity(
                db,
                mid,
                source="identity_repair_service",
                reason="fix_steam_on_internal_id",
                platform=new_plat,
                workspace_id=new_ws,
            )
            log_identity_mutation(
                db,
                mod_id=mid,
                field_name="platform",
                old_value=old_plat,
                new_value=new_plat,
                source="identity_repair_service",
                reason="fix_steam_on_internal_id",
            )
            result["applied"].append(
                {
                    "action": action.action,
                    "mod_id": mid,
                    "platform": new_plat,
                    "workspace_id": new_ws,
                }
            )
        elif action.action == "scrub_polluted_workspace_id":
            mid = action.canonical_mod_id
            ensure_non_polluted_workspace(db, mid)
            result["applied"].append({"action": action.action, "mod_id": mid})
        elif action.action == "retire_duplicate_entity":
            can = action.canonical_mod_id
            dup = action.duplicate_mod_id
            if not can or not dup or can == dup:
                result["skipped"].append({"action": action.action, "reason": "bad_ids"})
                continue
            if db.get_mod_display_info(can) is None and db.get_mod(can) is None:
                result["skipped"].append({"action": action.action, "reason": "no_canonical"})
                continue
            if db.get_mod_display_info(dup) is None and db.get_mod(dup) is None:
                result["skipped"].append({"action": action.action, "reason": "no_duplicate"})
                continue
            moved = _migrate_deployment_items(db, from_mod_id=dup, to_mod_id=can)
            # Contract: never write published_file_id = canonical Internal ID.
            db.delete_mod_record(dup)
            log_identity_mutation(
                db,
                mod_id=dup,
                field_name="retired",
                old_value=dup,
                new_value=can,
                source="identity_repair_service",
                reason="retire_duplicate_entity",
            )
            result["applied"].append(
                {
                    "action": action.action,
                    "canonical_mod_id": can,
                    "duplicate_mod_id": dup,
                    "deployment_items_migrated": moved,
                }
            )
        else:
            result["skipped"].append({"action": action.action, "reason": "unknown"})
    return result


def _apply_pollution_repairs(
    db: Any,
    report: IdentityPollutionReport,
) -> dict[str, Any]:
    """Apply pollution opcodes under IdentityService boundary where possible."""
    actions = report.actions or plan_identity_pollution_repair(report)
    result: dict[str, Any] = {
        "planned": len(actions),
        "applied": [],
        "skipped": [],
    }
    taken = {
        str(r["workspace_id"] or "").strip()
        for r in db._conn.execute(  # noqa: SLF001
            "SELECT workspace_id FROM mods "
            "WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) != ''"
        ).fetchall()
    }
    taken.discard("")

    for action in actions:
        mid = int(action.mod_id)
        if action.action == "INFER_APP_ID":
            new_app = int(action.details.get("new_app_id") or 0)
            plat = str(action.details.get("platform") or "")
            ext = str(action.details.get("external_id") or "")
            if new_app <= 0 or not plat or not ext:
                result["skipped"].append({"mod_id": mid, "reason": "incomplete"})
                continue
            conflict = db.find_mod_by_external(plat, ext, app_id=new_app)
            if conflict is not None and str(conflict.mod_id) != str(mid):
                result["skipped"].append(
                    {
                        "mod_id": mid,
                        "reason": "identity_already_owned",
                        "owner": str(conflict.mod_id),
                    }
                )
                continue
            persist_identity(
                db,
                mid,
                source="identity_repair_service",
                reason="INFER_APP_ID",
                app_id=new_app,
            )
            result["applied"].append(
                {"mod_id": mid, "action": "INFER_APP_ID", "app_id": new_app}
            )
        elif action.action == "REASSIGN_WORKSPACE_UNIQUE":
            new_ws = generate_unique_workspace_id(taken)
            taken.add(new_ws)
            persist_identity(
                db,
                mid,
                source="identity_repair_service",
                reason="REASSIGN_WORKSPACE_UNIQUE",
                workspace_id=new_ws,
            )
            result["applied"].append(
                {
                    "mod_id": mid,
                    "action": "REASSIGN_WORKSPACE_UNIQUE",
                    "workspace_id": new_ws,
                }
            )
        elif action.action == "MARK_UNRESOLVED":
            with db._lock:  # noqa: SLF001
                db._conn.execute(  # noqa: SLF001
                    "UPDATE mods SET identity_status = ? WHERE mod_id = ?",
                    ("unresolved", mid),
                )
                db._conn.commit()  # noqa: SLF001
            result["applied"].append({"mod_id": mid, "action": "MARK_UNRESOLVED"})
        else:
            result["skipped"].append(
                {"mod_id": mid, "reason": f"no_op:{action.action}"}
            )
    return result


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


@dataclass
class IdentityRepairDetection:
    entity_plan: EntityRepairPlan | None = None
    pollution: IdentityPollutionReport | None = None
    field_actions: list[FieldRepairAction] = field(default_factory=list)
    severity_before: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_candidates": (
                len(self.entity_plan.candidates) if self.entity_plan else 0
            ),
            "pollution_counts": (
                self.pollution.to_dict().get("counts") if self.pollution else {}
            ),
            "field_actions": [asdict(a) for a in self.field_actions],
            "severity_before": dict(self.severity_before),
            "notes": list(self.notes),
        }


@dataclass
class IdentityRepairValidation:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IdentityRepairResult:
    applied: bool = False
    success: bool = False
    error: str = ""
    detection: IdentityRepairDetection | None = None
    validation: IdentityRepairValidation | None = None
    entity: EntityRepairPlan | None = None
    pollution_apply: dict[str, Any] = field(default_factory=dict)
    field_apply: dict[str, Any] = field(default_factory=dict)
    severity_after: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "success": self.success,
            "error": self.error,
            "detection": self.detection.to_dict() if self.detection else {},
            "validation": self.validation.to_dict() if self.validation else {},
            "entity_applied_counts": (
                dict(self.entity.applied_counts)
                if self.entity and getattr(self.entity, "applied_counts", None)
                else {}
            ),
            "pollution_apply": self.pollution_apply,
            "field_apply": self.field_apply,
            "severity_after": dict(self.severity_after),
            "notes": list(self.notes),
        }


class IdentityRepairService:
    """Sole production/tooling entry for identity repair."""

    def detect(
        self,
        db: Any,
        library_root: str | Path,
    ) -> IdentityRepairDetection:
        """Read-only discovery. Never mutates DB or filesystem."""
        root = Path(library_root)
        detection = IdentityRepairDetection()
        detection.entity_plan = plan_identity_repair(db, root)
        detection.pollution = scan_identity_pollution(db)
        # Strip apply hint from pollution notes for detect-only clarity
        detection.pollution.notes = [
            n
            for n in detection.pollution.notes
            if "Apply via" not in n
        ]
        detection.pollution.notes.append(
            "Detect-only: apply via IdentityRepairService.repair()."
        )
        detection.field_actions = plan_field_scrubs(db, root)
        try:
            report = audit_mod_library_integrity(root, db=db)
            detection.severity_before = audit_severity_counts(report)
        except Exception as exc:  # noqa: BLE001
            detection.notes.append(f"severity_audit_skipped:{exc}")
        detection.notes.append("detect_side_effect_free")
        return detection

    def validate(
        self,
        detection: IdentityRepairDetection,
    ) -> IdentityRepairValidation:
        """Gate repairs against Identity Contract (no allocate / no mod_id rewrite)."""
        out = IdentityRepairValidation(ok=True)
        # Static contract checks on planned field actions
        for action in detection.field_actions:
            if action.action == "retire_duplicate_entity":
                if action.canonical_mod_id == action.duplicate_mod_id:
                    out.ok = False
                    out.errors.append("retire_duplicate refuses same mod_id")
            details = action.details or {}
            # Forbid any planned PFI = Internal ID binding
            if str(details.get("published_file_id") or "") == str(
                action.canonical_mod_id or ""
            ):
                out.ok = False
                out.errors.append("forbidden published_file_id=mod_id plan")
            if details.get("filesystem_policy") == "retain_folders_bind_to_canonical":
                out.ok = False
                out.errors.append("legacy PFI-bind filesystem policy rejected")
        if detection.entity_plan and detection.entity_plan.allocations:
            out.ok = False
            out.errors.append("entity plan reports allocations (forbidden)")
        if not out.ok:
            out.warnings.append("repair blocked until validation passes")
        return out

    def repair(
        self,
        db: Any,
        library_root: str | Path,
        *,
        apply: bool = False,
        detection: IdentityRepairDetection | None = None,
        quarantine_root: str | Path | None = None,
        include_entity: bool = True,
        include_pollution: bool = True,
        include_field_scrubs: bool = True,
    ) -> IdentityRepairResult:
        """
        Controlled repair. ``apply=False`` is dry-run.

        Never calls ``create_mod_identity``, never INSERT mods, never rewrites
        surviving ``mod_id``, never sets ``published_file_id`` to Internal ID.
        """
        root = Path(library_root)
        detected = detection or self.detect(db, root)
        validation = self.validate(detected)
        result = IdentityRepairResult(
            applied=False,
            success=validation.ok,
            detection=detected,
            validation=validation,
            notes=list(detected.notes),
        )
        if not validation.ok:
            result.error = "; ".join(validation.errors) or "validation_failed"
            result.success = False
            return result

        if not apply:
            result.success = True
            result.notes.append("dry_run_only")
            if detected.entity_plan:
                result.entity = detected.entity_plan
            result.pollution_apply = {
                "apply": False,
                "planned": len(detected.pollution.actions) if detected.pollution else 0,
                "actions": (
                    [a.to_dict() for a in detected.pollution.actions]
                    if detected.pollution
                    else []
                ),
            }
            result.field_apply = {
                "apply": False,
                "planned": len(detected.field_actions),
                "actions": [asdict(a) for a in detected.field_actions],
            }
            return result

        result.applied = True
        try:
            with repair_no_allocate_scope():
                if include_field_scrubs and detected.field_actions:
                    result.field_apply = _apply_field_scrubs(db, detected.field_actions)
                if include_pollution and detected.pollution is not None:
                    result.pollution_apply = _apply_pollution_repairs(
                        db, detected.pollution
                    )
                    result.pollution_apply["apply"] = True
                if include_entity:
                    result.entity = apply_identity_repair(
                        db,
                        root,
                        detected.entity_plan,
                        apply=True,
                        quarantine_root=quarantine_root,
                    )
                    if result.entity and not result.entity.success:
                        result.success = False
                        result.error = result.entity.error or "entity_repair_failed"
                        return result
            try:
                after = audit_mod_library_integrity(root, db=db)
                result.severity_after = audit_severity_counts(after)
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"post_audit_skipped:{exc}")
            result.success = True
            result.notes.append("repair_completed_no_allocate")
        except RepairMustNotAllocateError as exc:
            result.success = False
            result.error = str(exc)
            result.notes.append("repair_aborted_allocate_forbidden")
        except Exception as exc:  # noqa: BLE001
            logger.exception("IdentityRepairService.repair failed")
            result.success = False
            result.error = f"REPAIR FAILED: {exc}"
        return result

    def report(
        self,
        result: IdentityRepairResult | IdentityRepairDetection,
        *,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Serialize detect/repair outcome; optional JSON write under ``path``."""
        if isinstance(result, IdentityRepairDetection):
            payload = {"kind": "detect", **result.to_dict()}
            if result.entity_plan is not None:
                payload["entity_plan_text"] = format_repair_plan(result.entity_plan)
        else:
            payload = {"kind": "repair", **result.to_dict()}
            if result.entity is not None:
                payload["entity_plan_text"] = format_repair_plan(result.entity)
        if path:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return payload


def get_identity_repair_service() -> IdentityRepairService:
    return IdentityRepairService()
