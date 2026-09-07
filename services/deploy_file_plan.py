"""Deploy FilePlan — single authoritative file list for Backup/Apply/Verify/Manifest.

ARCHITECTURE CONTRACT (permanent)

DeployFilePlan.files is the SINGLE SOURCE OF TRUTH for which files participate
in a Deploy.

Backup, Apply, Verify, and Manifest MUST consume this same plan.

Do NOT reconstruct the deploy file list by:
- scanning the target after Apply
- before/after filesystem snapshots
- re-enumerating extracted trees for accounting
- a second Strategy-specific "deployed files" list

Existing target files are still valid deploy targets.
Success MUST NOT require a file to be newly created on disk.

See also ``docs/DEPLOYMENT_ARCHITECTURE.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from services.deploy_rules.base import DeployContext, StrategyResult
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry


OP_COPY = "copy"
OP_EXTRACT_MEMBER = "extract_member"

SOURCE_FOLDER = "folder"
SOURCE_ZIP = "zip"
SOURCE_RAR = "rar"
SOURCE_7Z = "7z"
SOURCE_MIXED = "mixed"


@dataclass
class DeployFilePlanEntry:
    """One planned deploy unit from FilePlan (never invent via after−before)."""

    source_relative: str = ""
    target_relative: str = ""
    target_absolute: str = ""
    source: str = ""
    op: str = OP_COPY
    type: str = ""
    root_kind: str = ""
    required: bool = True


@dataclass
class DeployFilePlanDiagnostics:
    """Pipeline counters — must survive failure (no silent files=0 / target=)."""

    planned_files: int = 0
    backed_up_files: int = 0
    applied_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
    failed_details: list[str] = field(default_factory=list)
    stage: str = ""
    archive_entry_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned_files": self.planned_files,
            "backed_up_files": self.backed_up_files,
            "applied_files": self.applied_files,
            "verified_files": self.verified_files,
            "failed_files": self.failed_files,
            "failed_details": list(self.failed_details),
            "stage": self.stage,
            "archive_entry_count": self.archive_entry_count,
        }


@dataclass
class DeployFilePlan:
    """
    Authoritative deploy file list for one Mod operation.

    ``files`` is the only legal deploy inventory for this run.
    """

    internal_id: str = ""
    deploy_type: str = ""
    source: str = ""
    source_kind: str = SOURCE_FOLDER
    content_root: str = ""
    managed_path: str = ""
    target_root: str = ""
    target_root_kind: str = ""
    archives: list[str] = field(default_factory=list)
    files: list[DeployFilePlanEntry] = field(default_factory=list)
    diagnostics: DeployFilePlanDiagnostics = field(
        default_factory=DeployFilePlanDiagnostics
    )

    def target_absolutes(self) -> list[str]:
        return [e.target_absolute for e in self.files if e.target_absolute]

    def refresh_planned_count(self) -> None:
        self.diagnostics.planned_files = len(self.files)

    def diagnostics_dict(self) -> dict[str, Any]:
        d = self.diagnostics.as_dict()
        d["source"] = self.source
        d["source_kind"] = self.source_kind
        d["target"] = self.target_root
        d["target_root"] = self.target_root
        d["target_root_kind"] = self.target_root_kind
        d["archives"] = list(self.archives)
        d["planned_target_count"] = len(self.target_absolutes())
        return d


def _is_archive_file(path: Path) -> bool:
    from services.importers.archive import is_archive_path

    try:
        return path.is_file() and bool(is_archive_path(path))
    except OSError:
        return False


def _suffix_kind(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".zip":
        return SOURCE_ZIP
    if suf == ".rar":
        return SOURCE_RAR
    if suf == ".7z":
        return SOURCE_7Z
    return SOURCE_FOLDER


def _rel_to(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def file_plan_from_strategy_result(
    planned: StrategyResult,
    ctx: DeployContext,
    *,
    archives: Iterable[str | Path] | None = None,
) -> DeployFilePlan:
    """
    Compat bridge (Phase 2): promote ``strategy.plan()`` output into a FilePlan.

    Phase 3 will build FilePlan without Strategy extract/enumerate ownership.
    """
    target_root = str(planned.target or "").strip()
    content_root = str(ctx.content_root())
    managed = str(ctx.library_folder())
    archive_list = [str(Path(p)) for p in (archives or ()) if str(p).strip()]

    entries: list[DeployFilePlanEntry] = []
    kinds: set[str] = set()

    for raw in list(planned.files or []):
        source = str(getattr(raw, "source", "") or "").strip()
        target_abs = str(getattr(raw, "target", "") or "").strip()
        entry_type = str(getattr(raw, "type", "") or "").strip()
        root_kind = str(getattr(raw, "root_kind", "") or "").strip()
        source_rel = str(getattr(raw, "source_relative", "") or "").strip()
        target_rel = str(getattr(raw, "relative", "") or "").strip()

        src_path = Path(source) if source else None
        tgt_path = Path(target_abs) if target_abs else None
        root_path = Path(target_root) if target_root else None

        if not target_rel and tgt_path is not None and root_path is not None:
            target_rel = _rel_to(tgt_path, root_path)

        is_arch = bool(src_path and _is_archive_file(src_path))
        op = OP_COPY
        if is_arch and entry_type in {"archive", "stamps"}:
            op = OP_EXTRACT_MEMBER
            if not source_rel and target_rel and entry_type == "archive":
                # Anno-style: zip members map 1:1 under mods_root.
                source_rel = target_rel
            if entry_type == "archive" and src_path is not None:
                kinds.add(_suffix_kind(src_path))
                if source and source not in archive_list:
                    archive_list.append(source)
            if entry_type == "stamps" and src_path is not None:
                kinds.add(_suffix_kind(src_path))
                if source and source not in archive_list:
                    archive_list.append(source)
        elif is_arch and not entry_type:
            # Untyped archive source — treat as extract when relative is known.
            if target_rel or source_rel:
                op = OP_EXTRACT_MEMBER
                source_rel = source_rel or target_rel
                kinds.add(_suffix_kind(src_path))  # type: ignore[arg-type]
                if source and source not in archive_list:
                    archive_list.append(source)
        else:
            if not source_rel and src_path is not None and content_root:
                source_rel = _rel_to(src_path, Path(content_root))
            kinds.add(SOURCE_FOLDER)

        if root_kind == "" and planned.deploy_type:
            # Best-effort; attach_canonical_targets still runs later.
            pass

        entries.append(
            DeployFilePlanEntry(
                source_relative=source_rel,
                target_relative=target_rel,
                target_absolute=target_abs,
                source=source,
                op=op,
                type=entry_type,
                root_kind=root_kind,
                required=True,
            )
        )

    if not kinds:
        source_kind = SOURCE_FOLDER
    elif kinds == {SOURCE_FOLDER}:
        source_kind = SOURCE_FOLDER
    elif len(kinds) == 1:
        source_kind = next(iter(kinds))
    else:
        source_kind = SOURCE_MIXED

    plan = DeployFilePlan(
        internal_id=str(ctx.internal_id),
        deploy_type=str(planned.deploy_type or ctx.deploy_type or ""),
        source=managed or content_root,
        source_kind=source_kind,
        content_root=content_root,
        managed_path=managed,
        target_root=target_root,
        target_root_kind=str(getattr(ctx, "target_root_kind", "") or ""),
        archives=archive_list,
        files=entries,
    )
    plan.refresh_planned_count()
    if archive_list:
        plan.diagnostics.archive_entry_count = plan.diagnostics.planned_files
    return plan


def file_plan_core_applicable(plan: DeployFilePlan) -> bool:
    """
    True when Core Apply can execute every required entry without Strategy.deploy.

    Extract members need a non-empty ``source_relative`` and an archive file.
    Copy entries need a readable non-missing source file.
    """
    if not plan.files:
        return False
    for entry in plan.files:
        if not entry.required:
            continue
        if not entry.target_absolute:
            return False
        if entry.op == OP_EXTRACT_MEMBER:
            if not entry.source_relative:
                return False
            if not _is_archive_file(Path(entry.source)):
                return False
        elif entry.op == OP_COPY:
            src = Path(entry.source)
            try:
                if not src.is_file():
                    return False
            except OSError:
                return False
            # Never "copy" an archive blob as a deploy payload via copy op.
            if _is_archive_file(src) and entry.type in {"archive", "stamps"}:
                return False
        else:
            return False
    return True


def manifest_from_file_plan(
    plan: DeployFilePlan,
    *,
    deploy_time: str,
) -> DeployManifest:
    """
    Build DeployManifest strictly from FilePlan.files (no target rescan).

    Manifest file count must match planned FilePlan entries on success.
    """
    files: list[ManifestFileEntry] = []
    for entry in plan.files:
        files.append(
            ManifestFileEntry(
                source=entry.source,
                target=entry.target_absolute,
                type=entry.type,
                root_kind=entry.root_kind,
                relative=entry.target_relative,
                source_relative=entry.source_relative,
            )
        )
    return DeployManifest(
        mod_id=plan.internal_id,
        internal_id=plan.internal_id,
        deploy_time=deploy_time,
        deploy_type=plan.deploy_type,
        files=files,
        source_path=plan.managed_path or plan.source,
    )


def strategy_result_from_file_plan(
    plan: DeployFilePlan,
    *,
    deploy_time: str,
    success: bool,
    error: str = "",
) -> StrategyResult:
    """Adapt FilePlan outcome back to StrategyResult for existing orchestrator glue."""
    manifest = manifest_from_file_plan(plan, deploy_time=deploy_time) if success else None
    return StrategyResult(
        success=success,
        error=error,
        target=plan.target_root,
        copied_files=plan.diagnostics.applied_files if success else 0,
        deploy_type=plan.deploy_type,
        deploy_time=deploy_time if success else "",
        files=list(manifest.files) if manifest is not None else [
            ManifestFileEntry(
                source=e.source,
                target=e.target_absolute,
                type=e.type,
                root_kind=e.root_kind,
                relative=e.target_relative,
                source_relative=e.source_relative,
            )
            for e in plan.files
        ],
        manifest=manifest,
    )
