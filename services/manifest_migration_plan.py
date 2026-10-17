"""In-memory production manifest migration PLAN (no writes).

Path migration and deployment-state repair are intentionally separate.
This module never calls save_manifest / delete_manifest / deploy APIs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from services.deploy_paths import (
    MANIFEST_SCHEMA_VERSION,
    DeployPathError,
    classify_absolute_target,
    derive_legacy_relative,
    iter_typed_allowed_roots,
    project_relative,
    remap_entry_target,
)
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

TAG_SAFE_PATH = "SAFE_PATH_MIGRATION"
TAG_UNSAFE_PATH = "UNSAFE_PATH_MIGRATION"
TAG_STATE_MISMATCH = "STATE_MISMATCH"
TAG_SOURCE_DRIFT = "SOURCE_DRIFT"

REASON_UNKNOWN_ROOT = "UNKNOWN_ROOT"
REASON_AMBIGUOUS_ROOT = "AMBIGUOUS_ROOT"
REASON_CROSS_DRIVE_UNRELATED = "CROSS_DRIVE_UNRELATED"
REASON_ABSOLUTE_ESCAPE = "ABSOLUTE_ESCAPE"
REASON_INVALID_RELATIVE = "INVALID_RELATIVE"


@dataclass
class PlannedEntry:
    root_kind: str
    relative: str
    old_target: str
    projected_target: str
    type: str = ""
    source: str = ""
    source_relative: str = ""


@dataclass
class MigrationCandidate:
    """Path-migration candidate only — never implies deploy_status repair."""

    mod_id: str
    workspace_id: str
    app_id: int
    game: str
    manifest_path: str
    managed_path: str
    old_schema: int
    old_target_examples: list[str]
    new_schema_version: int
    root_kind: str
    relative_examples: list[str]
    current_projected_target_examples: list[str]
    source_path_status: str
    filesystem_status: str
    db_deploy_status: str
    path_migration_safe: bool
    deployment_state_consistent: bool
    tags: list[str] = field(default_factory=list)
    planned_entries: list[PlannedEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class UnsafeManifestReport:
    mod_id: str
    manifest_path: str
    reason: str
    representative_target: str
    possible_root_candidates: list[str]
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceHistoricalReport:
    mod_id: str
    manifest_path: str
    managed_path: str
    last_known_path: str
    manifest_source_path: str
    entry_source_examples: list[str]
    existing: bool
    missing: bool
    resolvable_via_managed: bool
    conflicting: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _drive_of(path: Path | str) -> str:
    try:
        p = Path(path)
        drive = (p.drive or "").upper()
        if drive:
            return drive if drive.endswith(":") else f"{drive}:"
        anchor = str(p.anchor or "")
        if len(anchor) >= 2 and anchor[1] == ":":
            return anchor[:2].upper()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _exists(path: Path | str) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).replace("\\", "/").casefold() == str(b).replace("\\", "/").casefold()


def unsafe_reason_for_target(
    target: str,
    ctx: DeployContext,
) -> str:
    """Classify why a legacy absolute target cannot be safely remapped."""
    raw = str(target or "").strip()
    if not raw:
        return REASON_INVALID_RELATIVE
    if ".." in Path(raw).parts:
        return REASON_ABSOLUTE_ESCAPE

    typed = iter_typed_allowed_roots(ctx)
    if not typed:
        return REASON_UNKNOWN_ROOT

    if classify_absolute_target(raw, ctx) is not None:
        return ""  # safe under current root

    matches: list[tuple[str, Path, str]] = []
    for kind, root in typed:
        derived = derive_legacy_relative(raw, root)
        if not derived:
            continue
        try:
            project_relative(root, derived)
        except DeployPathError:
            continue
        matches.append((kind, root, derived))

    if len(matches) > 1:
        projections = set()
        for kind, root, derived in matches:
            try:
                projections.add(str(project_relative(root, derived)))
            except DeployPathError:
                return REASON_AMBIGUOUS_ROOT
        if len(projections) > 1:
            return REASON_AMBIGUOUS_ROOT
        return ""  # same projection — safe

    if len(matches) == 1:
        return ""

    tgt_drive = _drive_of(raw)
    current_drives = {_drive_of(r) for _, r in typed}
    current_drives.discard("")
    if tgt_drive and current_drives and tgt_drive not in current_drives:
        return REASON_CROSS_DRIVE_UNRELATED
    return REASON_UNKNOWN_ROOT


def plan_entry_to_v2(
    entry: ManifestFileEntry,
    ctx: DeployContext,
) -> PlannedEntry:
    """Derive one canonical entry in memory. Raises DeployPathError if unsafe."""
    remapped = remap_entry_target(entry, ctx)
    return PlannedEntry(
        root_kind=str(remapped.root_kind or ""),
        relative=str(remapped.relative or "").replace("\\", "/"),
        old_target=str(entry.target or ""),
        projected_target=str(remapped.target or ""),
        type=str(entry.type or ""),
        source=str(entry.source or ""),
        source_relative=str(getattr(entry, "source_relative", "") or ""),
    )


def build_v2_manifest_in_memory(
    manifest: DeployManifest,
    ctx: DeployContext,
) -> DeployManifest:
    """
    Construct a schema-v2 DeployManifest from legacy/v2 input.

    Never writes disk. Preserves audit fields (backup, hashes, types, sources).
    Absolute ``target`` is the *current* projection only (not identity).
    """
    planned_files: list[ManifestFileEntry] = []
    for entry in manifest.files:
        remapped = remap_entry_target(entry, ctx)
        planned_files.append(
            ManifestFileEntry(
                source=entry.source,
                target=str(remapped.target),
                type=entry.type,
                backup=entry.backup,
                source_hash=entry.source_hash,
                root_kind=str(remapped.root_kind or ""),
                relative=str(remapped.relative or "").replace("\\", "/"),
                source_relative=str(getattr(entry, "source_relative", "") or ""),
            )
        )
    return DeployManifest(
        mod_id=manifest.mod_id,
        deploy_time=manifest.deploy_time,
        deploy_type=manifest.deploy_type,
        files=planned_files,
        content_fingerprint=manifest.content_fingerprint,
        source_path=manifest.source_path,
        internal_id=manifest.internal_id,
        schema_version=MANIFEST_SCHEMA_VERSION,
    )


def migration_is_noop(
    manifest: DeployManifest,
    ctx: DeployContext,
) -> bool:
    """True when re-planning yields identical canonical relatives + root_kinds."""
    if int(getattr(manifest, "schema_version", 0) or 0) < MANIFEST_SCHEMA_VERSION:
        return False
    planned = build_v2_manifest_in_memory(manifest, ctx)
    if len(planned.files) != len(manifest.files):
        return False
    for left, right in zip(manifest.files, planned.files, strict=True):
        if str(left.root_kind or "") != str(right.root_kind or ""):
            return False
        if str(left.relative or "").replace("\\", "/") != str(right.relative or "").replace(
            "\\", "/"
        ):
            return False
    return True


def filesystem_status_for_plan(
    planned_entries: Iterable[PlannedEntry],
) -> str:
    """Classify whether projected current targets exist on disk (read-only)."""
    entries = list(planned_entries)
    if not entries:
        return "empty"
    present = sum(1 for e in entries if _exists(e.projected_target))
    old_present = sum(1 for e in entries if e.old_target and _exists(e.old_target))
    if present == len(entries):
        return "projected_all_present"
    if present == 0 and old_present > 0:
        return "historical_present_projected_missing"
    if present == 0 and old_present == 0:
        return "none_present"
    return "projected_partial"


def deployment_state_consistent(
    *,
    db_deploy_status: str,
    filesystem_status: str,
    projected_any_present: bool,
) -> bool:
    """
    Separate from path_migration_safe.

    Consistent means DB deployed ↔ current projected files roughly agree.
    Mismatch does NOT make path migration unsafe.
    """
    status = str(db_deploy_status or "").strip().lower()
    deployed = status == "deployed"
    if deployed and filesystem_status in {
        "projected_all_present",
        "projected_partial",
    }:
        return filesystem_status == "projected_all_present"
    if deployed and not projected_any_present:
        return False
    if not deployed and projected_any_present:
        return False
    if not deployed and filesystem_status in {"none_present", "empty"}:
        return True
    return False


def classify_source_historical(
    *,
    managed: Path,
    manifest: DeployManifest,
    last_known_path: str = "",
) -> SourceHistoricalReport:
    """
    Report-only source drift. Manifest source_path is never treated as identity.
    """
    managed = Path(managed)
    lkp = str(last_known_path or "").strip()
    man_source = str(getattr(manifest, "source_path", "") or "").strip()
    entry_sources = [
        str(e.source or "").strip()
        for e in manifest.files
        if str(e.source or "").strip()
    ][:8]

    managed_ok = managed.is_dir()
    lkp_ok = bool(lkp) and Path(lkp).is_dir()
    resolvable = managed_ok
    conflicting = False
    if lkp_ok and managed_ok and not _same_path(lkp, managed):
        conflicting = True

    # Historical absolute sources outside managed (not archive-under-managed).
    historical_existing = False
    historical_missing = False
    for src in [man_source, *entry_sources]:
        if not src:
            continue
        try:
            p = Path(src)
            if not p.is_absolute():
                continue
            if managed_ok:
                try:
                    p.resolve().relative_to(managed.resolve())
                    continue  # under current managed — not drift identity
                except (ValueError, OSError):
                    pass
            if p.exists():
                historical_existing = True
            else:
                historical_missing = True
        except OSError:
            historical_missing = True

    note = (
        "manifest source_path is audit-only; identity remains managed_path / DB"
    )
    return SourceHistoricalReport(
        mod_id=str(manifest.mod_id or ""),
        manifest_path="",
        managed_path=str(managed),
        last_known_path=lkp,
        manifest_source_path=man_source,
        entry_source_examples=entry_sources[:5],
        existing=historical_existing,
        missing=historical_missing,
        resolvable_via_managed=resolvable,
        conflicting=conflicting,
        note=note,
    )


def plan_manifest_migration(
    *,
    manifest: DeployManifest,
    ctx: DeployContext,
    manifest_path: Path | str,
    managed_path: Path | str,
    workspace_id: str = "",
    game_name: str = "",
    db_deploy_status: str = "",
    last_known_path: str = "",
) -> tuple[MigrationCandidate | None, UnsafeManifestReport | None]:
    """
    Build a SAFE path-migration candidate or an unsafe report.

    Never writes. Never mutates DB. Never treats state mismatch as path-unsafe.
    """
    mpath = str(manifest_path)
    managed = Path(managed_path)
    old_schema = int(getattr(manifest, "schema_version", 0) or 0)
    tags: list[str] = []

    source_report = classify_source_historical(
        managed=managed,
        manifest=manifest,
        last_known_path=last_known_path,
    )
    if (
        source_report.existing
        or source_report.missing
        or source_report.conflicting
        or (
            str(getattr(manifest, "source_path", "") or "").strip()
            and not _same_path(
                str(getattr(manifest, "source_path", "") or ""), managed
            )
        )
    ):
        # Drift present — report tag only; does not cancel safe path migration.
        if not source_report.resolvable_via_managed or source_report.conflicting:
            tags.append(TAG_SOURCE_DRIFT)
        elif source_report.existing or source_report.missing:
            tags.append(TAG_SOURCE_DRIFT)

    # Already v2 with stable relatives → still a candidate only if planning works;
    # idempotent path yields same relatives.
    entry_errors: list[tuple[str, str]] = []
    planned_entries: list[PlannedEntry] = []
    root_kinds: set[str] = set()

    for entry in manifest.files:
        raw = str(entry.target or "").strip()
        try:
            planned = plan_entry_to_v2(entry, ctx)
            if not planned.root_kind or not planned.relative:
                raise DeployPathError("empty root_kind/relative after remap")
            planned_entries.append(planned)
            root_kinds.add(planned.root_kind)
        except DeployPathError as exc:
            reason = unsafe_reason_for_target(raw, ctx) or REASON_UNKNOWN_ROOT
            msg = str(exc).lower()
            if "ambiguous" in msg:
                reason = REASON_AMBIGUOUS_ROOT
            if "traversal" in msg or "absolute relative" in msg:
                reason = REASON_ABSOLUTE_ESCAPE
            entry_errors.append((reason, raw))

    if entry_errors or not planned_entries:
        reason = entry_errors[0][0] if entry_errors else REASON_INVALID_RELATIVE
        rep = entry_errors[0][1] if entry_errors else ""
        roots = [str(r) for _, r in iter_typed_allowed_roots(ctx)]
        tags.append(TAG_UNSAFE_PATH)
        return None, UnsafeManifestReport(
            mod_id=str(manifest.mod_id or ctx.mod_id or ""),
            manifest_path=mpath,
            reason=reason,
            representative_target=rep,
            possible_root_candidates=roots,
            tags=tags,
        )

    fs_status = filesystem_status_for_plan(planned_entries)
    projected_any = any(_exists(e.projected_target) for e in planned_entries)
    state_ok = deployment_state_consistent(
        db_deploy_status=db_deploy_status,
        filesystem_status=fs_status,
        projected_any_present=projected_any,
    )
    if not state_ok:
        tags.append(TAG_STATE_MISMATCH)

    tags.append(TAG_SAFE_PATH)
    # Dominant root_kind when uniform; else list first.
    root_kind = next(iter(root_kinds)) if len(root_kinds) == 1 else ",".join(sorted(root_kinds))

    source_status = "SOURCE_CURRENT"
    if TAG_SOURCE_DRIFT in tags:
        if source_report.conflicting:
            source_status = "SOURCE_CONFLICT"
        elif source_report.missing and not source_report.existing:
            source_status = "SOURCE_MISSING"
        else:
            source_status = "SOURCE_HISTORICAL"
    elif source_report.resolvable_via_managed:
        source_status = "SOURCE_CURRENT"

    candidate = MigrationCandidate(
        mod_id=str(manifest.mod_id or ctx.mod_id or ""),
        workspace_id=str(workspace_id or ""),
        app_id=int(ctx.app_id or 0),
        game=str(game_name or getattr(ctx.config, "name", "") or ""),
        manifest_path=mpath,
        managed_path=str(managed),
        old_schema=old_schema,
        old_target_examples=[e.old_target for e in planned_entries[:5]],
        new_schema_version=MANIFEST_SCHEMA_VERSION,
        root_kind=root_kind,
        relative_examples=[e.relative for e in planned_entries[:8]],
        current_projected_target_examples=[
            e.projected_target for e in planned_entries[:5]
        ],
        source_path_status=source_status,
        filesystem_status=fs_status,
        db_deploy_status=str(db_deploy_status or ""),
        path_migration_safe=True,
        deployment_state_consistent=state_ok,
        tags=sorted(set(tags)),
        planned_entries=planned_entries,
        notes=[
            "path_migration_safe independent of deployment_state_consistent",
            "manifest source_path is not source identity",
        ],
    )
    return candidate, None


def assert_plan_module_has_no_write_calls() -> None:
    """Source-level guard: plan module must not call save/delete/deploy APIs."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "save_manifest",
        "delete_manifest",
        "deploy_mod",
        "undeploy_mod",
        "redeploy_mod",
        "update_mod_deploy_status",
        "update_game_deploy_config",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in forbidden:
                raise AssertionError(
                    f"manifest_migration_plan must not call {name}()"
                )


# Future migration phase design (documentation constants — not executed).
MIGRATION_PIPELINE = (
    "legacy manifest",
    "derive canonical root_kind + relative",
    "validate",
    "construct v2 manifest in memory",
    "preserve audit metadata",
    "write only if explicit migration phase later approves",
)

MIGRATION_GUARDS = (
    "no filesystem mutation of game targets",
    "no DB deployment status mutation",
    "no deploy/undeploy/redeploy",
    "no source identity rewrite",
    "no allowed-root expansion",
)

MIGRATION_SAFETY_MECHANISM = {
    "precondition": (
        "path_migration_safe==true; schema legacy or non-idempotent v2; "
        "backup destination available under tools/_migration_backup (not game tree)"
    ),
    "backup": "copy deploy_manifest.json → sidecar .bak before write (future phase)",
    "atomic_write": "write temp file in .info then replace",
    "validation": "reload + migration_is_noop must be true",
    "rollback": "restore .bak on validation failure",
    "postcondition": "schema_version==2; relatives stable; DB deploy_status unchanged",
}
