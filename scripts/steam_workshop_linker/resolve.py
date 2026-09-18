"""Resolve the real SMM Mod directory for a registered Steam entity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from database import ModRow
from junction import is_reparse_point
from metadata import (
    disk_internal_id,
    normalize_workspace_id,
    read_metadata,
    scan_smm_workspace_index,
)
from safety import is_under_resolved, lexical_norm


@dataclass(frozen=True)
class PathResolution:
    path: Path | None
    reason: str = ""


def _info_identity_problem(
    mod_dir: Path,
    *,
    workspace_id: str,
    db_internal_id: str,
) -> str:
    meta_path = mod_dir / ".info" / "metadata.json"
    if not meta_path.is_file():
        return "PATH_IDENTITY_MISMATCH"
    meta = read_metadata(meta_path)
    if meta is None:
        return "PATH_IDENTITY_MISMATCH"
    disk_ws = normalize_workspace_id(meta.get("workspace_id"))
    if disk_ws != workspace_id:
        return "PATH_IDENTITY_MISMATCH"
    disk_iid, iid_conflict = disk_internal_id(mod_dir, meta)
    if iid_conflict:
        return "IDENTITY_MISMATCH"
    db_iid = str(db_internal_id or "").strip()
    if disk_iid and db_iid and disk_iid != db_iid:
        return "IDENTITY_MISMATCH"
    return ""


def _candidate_usable(
    path: Path,
    *,
    workspace_id: str,
    db_internal_id: str,
    workshop_path: Path,
    workshop_root: Path,
    smm_mod_root: Path,
) -> str:
    """Return empty if *path* is a valid Junction target, else a reason code."""
    if not path.exists() or not path.is_dir():
        return "SMM_PATH_NOT_FOUND"
    if is_reparse_point(path):
        return "INVALID_TARGET"
    if lexical_norm(path) == lexical_norm(workshop_path) or lexical_norm(path) == lexical_norm(
        workshop_root
    ):
        return "INVALID_TARGET"
    if lexical_norm(path) == lexical_norm(smm_mod_root):
        return "INVALID_TARGET"
    if not is_under_resolved(path, smm_mod_root):
        return "INVALID_TARGET"
    if is_under_resolved(path, workshop_root):
        return "INVALID_TARGET"
    return _info_identity_problem(
        path, workspace_id=workspace_id, db_internal_id=db_internal_id
    )


def resolve_smm_mod_path(
    row: ModRow,
    *,
    workspace_id: str,
    workshop_path: Path,
    workshop_root: Path,
    smm_mod_root: Path,
    fs_index: dict[str, list[Path]] | None = None,
) -> PathResolution:
    """
    1. Validate ``mods.last_known_path`` + ``.info`` identity.
    2. If invalid, fallback-scan ``smm_mod_root`` by ``workspace_id``.
    """
    db_path = str(row.last_known_path or "").strip()
    if db_path:
        candidate = Path(db_path)
        problem = _candidate_usable(
            candidate,
            workspace_id=workspace_id,
            db_internal_id=row.internal_id,
            workshop_path=workshop_path,
            workshop_root=workshop_root,
            smm_mod_root=smm_mod_root,
        )
        if not problem:
            return PathResolution(candidate)
        db_problem = problem
    else:
        db_problem = "SMM_PATH_NOT_FOUND"

    index = fs_index if fs_index is not None else scan_smm_workspace_index(smm_mod_root)
    matches = list(index.get(workspace_id) or [])
    usable: list[Path] = []
    identity_hit = False
    for path in matches:
        problem = _candidate_usable(
            path,
            workspace_id=workspace_id,
            db_internal_id=row.internal_id,
            workshop_path=workshop_path,
            workshop_root=workshop_root,
            smm_mod_root=smm_mod_root,
        )
        if not problem:
            usable.append(path)
        elif problem == "IDENTITY_MISMATCH":
            identity_hit = True
    if len(usable) == 1:
        return PathResolution(usable[0])
    if len(usable) > 1:
        return PathResolution(None, "AMBIGUOUS_FILESYSTEM_MAPPING")
    if identity_hit:
        return PathResolution(None, "IDENTITY_MISMATCH")
    if db_problem == "PATH_IDENTITY_MISMATCH":
        return PathResolution(None, "PATH_IDENTITY_MISMATCH")
    if db_problem == "IDENTITY_MISMATCH":
        return PathResolution(None, "IDENTITY_MISMATCH")
    if db_problem == "INVALID_TARGET" and not matches:
        return PathResolution(None, "INVALID_TARGET")
    return PathResolution(None, "SMM_PATH_NOT_FOUND")
