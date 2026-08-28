"""Deploy path / manifest security boundaries (orchestration-layer checks).

Does not change DeployStrategy interfaces. Call from ``ModDeployer`` before
destructive undeploy / after plan / before save_manifest.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

logger = logging.getLogger(__name__)


class ManifestSecurityError(ValueError):
    """Manifest or path failed a deploy security boundary check."""


def _resolve(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _is_under(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def collect_allowed_target_roots(ctx: DeployContext) -> list[Path]:
    """Roots under which deploy targets may live for this context."""
    roots: list[Path] = []
    custom = str(ctx.custom_deploy_path or "").strip()
    if custom:
        try:
            roots.append(_resolve(custom))
        except OSError:
            pass
    mod_path = str(ctx.config.mod_path or "").strip()
    if mod_path:
        try:
            roots.append(_resolve(mod_path))
        except OSError:
            pass
    install = str(getattr(ctx.config, "install_path", "") or "").strip()
    if install:
        try:
            roots.append(_resolve(install))
        except OSError:
            pass

    # Strategy-owned external roots (Anno stamps → Documents/…)
    deploy_type = str(ctx.deploy_type or "").strip().lower()
    app_id = int(getattr(ctx, "app_id", 0) or 0)
    if deploy_type in {"anno_1800", "anno"} or app_id == 916440:
        try:
            from services.deploy_rules.anno import resolve_anno_stamps_dir

            stamps = resolve_anno_stamps_dir()
            roots.append(_resolve(stamps))
            # Parent ``Anno 1800`` docs folder is also a valid stop/root
            roots.append(_resolve(stamps.parent))
        except OSError:
            pass

    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def collect_protected_roots(ctx: DeployContext) -> list[Path]:
    """Directories that must never be removed by empty-parent pruning."""
    protected: list[Path] = []
    for raw in (
        str(getattr(ctx.config, "install_path", "") or "").strip(),
        str(ctx.config.mod_path or "").strip(),
        str(ctx.custom_deploy_path or "").strip(),
    ):
        if not raw:
            continue
        try:
            protected.append(_resolve(raw))
        except OSError:
            continue
    try:
        protected.append(_resolve(ctx.library_folder()))
    except OSError:
        pass
    try:
        protected.append(_resolve(ctx.content_root()))
    except OSError:
        pass
    deploy_type = str(ctx.deploy_type or "").strip().lower()
    app_id = int(getattr(ctx, "app_id", 0) or 0)
    if deploy_type in {"anno_1800", "anno"} or app_id == 916440:
        try:
            from services.deploy_rules.anno import resolve_anno_stamps_dir

            stamps = _resolve(resolve_anno_stamps_dir())
            protected.append(stamps)
            protected.append(stamps.parent)
        except OSError:
            pass
    for p in list(protected):
        try:
            if p.anchor:
                protected.append(Path(p.anchor))
        except Exception:  # noqa: BLE001
            pass
    return protected


def validate_manifest_mod_id(
    manifest: DeployManifest,
    expected_mod_id: str,
) -> None:
    """Refuse manifests that claim a different Mod identity."""
    mid = str(expected_mod_id or "").strip()
    claimed = str(manifest.mod_id or "").strip()
    if not mid:
        raise ManifestSecurityError("expected mod_id is empty")
    if claimed and claimed != mid:
        raise ManifestSecurityError(
            f"manifest mod_id mismatch: expected={mid} claimed={claimed}"
        )


def validate_manifest_targets(
    manifest: DeployManifest,
    *,
    allowed_roots: Iterable[Path],
    planned_targets: Iterable[str | Path] | None = None,
) -> None:
    """
    Every ``files[].target`` must be an absolute path under an allowed root
    (or exactly match a planned deploy target). Rejects ``..`` traversal.
    """
    roots: list[Path] = []
    for r in allowed_roots:
        try:
            roots.append(_resolve(r))
        except OSError:
            continue
    if not roots and not planned_targets:
        raise ManifestSecurityError(
            "no allowed target roots for undeploy validation"
        )

    planned: set[str] = set()
    for raw in planned_targets or ():
        try:
            planned.add(str(_resolve(raw)))
        except OSError:
            planned.add(str(Path(raw)))

    for entry in manifest.files:
        raw = str(entry.target or "").strip()
        if not raw:
            raise ManifestSecurityError("manifest entry has empty target")
        path = Path(raw)
        if not path.is_absolute():
            raise ManifestSecurityError(f"target must be absolute: {raw}")
        if ".." in path.parts:
            raise ManifestSecurityError(f"target path traversal rejected: {raw}")
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ManifestSecurityError(f"cannot resolve target: {raw}") from exc

        key = str(resolved)
        if key in planned:
            continue
        if any(_is_under(resolved, root) or resolved == root for root in roots):
            continue
        raise ManifestSecurityError(
            f"target outside allowed deploy roots: {raw}"
        )


def validate_manifest_sources(
    manifest: DeployManifest,
    *,
    workspace_roots: Iterable[Path],
) -> None:
    """Every non-empty ``source`` must resolve under a workspace / content root."""
    roots: list[Path] = []
    for r in workspace_roots:
        try:
            roots.append(_resolve(r))
        except OSError:
            continue
    if not roots:
        raise ManifestSecurityError("no workspace roots for source validation")

    for entry in manifest.files:
        raw = str(entry.source or "").strip()
        if not raw:
            continue
        if ".." in Path(raw).parts:
            raise ManifestSecurityError(f"source path traversal rejected: {raw}")
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError as exc:
            raise ManifestSecurityError(f"cannot resolve source: {raw}") from exc
        if not any(_is_under(resolved, root) or resolved == root for root in roots):
            raise ManifestSecurityError(f"source outside mod workspace: {raw}")


def validate_entry_for_save(
    entry: ManifestFileEntry,
    *,
    managed: Path,
    workspace_roots: Iterable[Path],
    allowed_target_roots: Iterable[Path],
) -> None:
    """Per-entry checks before persisting a deploy manifest."""
    validate_manifest_targets(
        DeployManifest(
            mod_id="_",
            deploy_time="",
            deploy_type="",
            files=[entry],
        ),
        allowed_roots=allowed_target_roots,
    )
    raw_src = str(entry.source or "").strip()
    entry_type = str(entry.type or "").strip().lower()
    generated = entry_type in {"archive", "generated", "virtual"}
    if raw_src:
        validate_manifest_sources(
            DeployManifest(
                mod_id="_",
                deploy_time="",
                deploy_type="",
                files=[entry],
            ),
            workspace_roots=workspace_roots,
        )
        src = Path(raw_src)
        if not generated and not src.exists():
            raise ManifestSecurityError(f"source missing: {raw_src}")
    elif not generated:
        # Empty source only allowed for explicitly generated/archive entries
        raise ManifestSecurityError("source missing and entry is not archive/generated")
    if entry.backup is not None:
        from services.backup_manager import BackupManager

        BackupManager(managed).resolve_backup_file(entry.backup)


def validate_manifest_for_save(
    manifest: DeployManifest,
    *,
    managed: Path,
    ctx: DeployContext,
) -> None:
    """Full manifest validation before ``save_manifest``."""
    validate_manifest_mod_id(manifest, ctx.mod_id)
    roots = collect_allowed_target_roots(ctx)
    workspace = [
        _resolve(ctx.library_folder()),
        _resolve(ctx.content_root()),
    ]
    # Targets already produced by the active strategy for this deploy are
    # accepted when they match the entry list (absolute / no ``..`` still enforced).
    validate_manifest_targets(
        manifest,
        allowed_roots=roots,
        planned_targets=[e.target for e in manifest.files],
    )
    for entry in manifest.files:
        validate_entry_for_save(
            entry,
            managed=managed,
            workspace_roots=workspace,
            allowed_target_roots=roots,
        )


def validate_planned_sources(
    entries: Iterable[ManifestFileEntry],
    *,
    workspace_roots: Iterable[Path],
) -> None:
    """Refuse plan/deploy when any source escapes the mod workspace."""
    probe = DeployManifest(
        mod_id="_",
        deploy_time="",
        deploy_type="",
        files=list(entries),
    )
    validate_manifest_sources(probe, workspace_roots=workspace_roots)


__all__ = (
    "ManifestSecurityError",
    "collect_allowed_target_roots",
    "collect_protected_roots",
    "validate_entry_for_save",
    "validate_manifest_for_save",
    "validate_manifest_mod_id",
    "validate_manifest_sources",
    "validate_manifest_targets",
    "validate_planned_sources",
)
