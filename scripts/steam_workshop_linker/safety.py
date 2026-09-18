"""Delete-path guards. Never delete an SMM Mod directory."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from junction import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    is_reparse_point,
    lexical_norm,
    lexists,
    paths_point_same,
    remove_junction,
)


class SafetyError(RuntimeError):
    """Refuse an operation that could destroy SMM or Workshop-root data."""


def is_strict_direct_child(path: Path, parent: Path) -> bool:
    """True when *path* is a direct child of *parent* without following *path*."""
    try:
        parent_key = os.path.normcase(str(parent.resolve()))
        actual_parent = os.path.normcase(str(path.parent.resolve()))
    except OSError:
        parent_key = lexical_norm(parent)
        actual_parent = lexical_norm(path.parent)
    return actual_parent == parent_key and path.name not in {"", ".", ".."}


def is_under_resolved(child: Path, parent: Path) -> bool:
    try:
        child_key = os.path.normcase(str(child.resolve()))
        parent_key = os.path.normcase(str(parent.resolve()))
    except OSError:
        child_key = lexical_norm(child)
        parent_key = lexical_norm(parent)
    if child_key == parent_key:
        return True
    sep = os.sep
    if not parent_key.endswith(sep):
        parent_key += sep
    return child_key.startswith(parent_key)


def assert_roots_exist(workshop_root: Path, smm_mod_root: Path) -> None:
    if not workshop_root.exists() or not workshop_root.is_dir():
        raise SafetyError(f"workshop_root does not exist or is not a directory: {workshop_root}")
    if not smm_mod_root.exists() or not smm_mod_root.is_dir():
        raise SafetyError(f"smm_mod_root does not exist or is not a directory: {smm_mod_root}")
    if lexical_norm(workshop_root) == lexical_norm(smm_mod_root):
        raise SafetyError("workshop_root and smm_mod_root must be different directories")
    if is_under_resolved(workshop_root, smm_mod_root):
        raise SafetyError("workshop_root must not be inside smm_mod_root")
    if is_under_resolved(smm_mod_root, workshop_root):
        raise SafetyError("smm_mod_root must not be inside workshop_root")


def assert_smm_target_exists(smm_target: Path) -> None:
    if not smm_target.exists() or not smm_target.is_dir():
        raise SafetyError(f"SMM Mod directory does not exist: {smm_target}")


def assert_workshop_path_in_root(workshop_path: Path, workshop_root: Path) -> None:
    if lexical_norm(workshop_path) == lexical_norm(workshop_root):
        raise SafetyError(f"refusing to operate on workshop_root itself: {workshop_path}")
    if not is_strict_direct_child(workshop_path, workshop_root):
        raise SafetyError(
            f"workshop path is not a direct child of workshop_root: {workshop_path}"
        )


def assert_target_is_not_smm_source(
    delete_path: Path,
    smm_target: Path,
    smm_mod_root: Path,
    *,
    workshop_root: Path,
) -> None:
    """Refuse any delete whose real object is the SMM Mod (or its tree)."""
    assert_workshop_path_in_root(delete_path, workshop_root)
    if lexical_norm(delete_path) == lexical_norm(smm_target):
        raise SafetyError(f"workshop path equals SMM target; refusing delete: {delete_path}")
    if lexical_norm(delete_path) == lexical_norm(smm_mod_root):
        raise SafetyError(f"refusing to delete smm_mod_root: {delete_path}")
    if is_reparse_point(delete_path):
        return
    try:
        resolved = delete_path.resolve()
    except OSError as exc:
        raise SafetyError(f"cannot resolve workshop path {delete_path}: {exc}") from exc
    if paths_point_same(resolved, smm_target) or is_under_resolved(resolved, smm_mod_root):
        raise SafetyError(
            f"resolved delete path is inside SMM; refusing delete: {resolved}"
        )
    if not is_under_resolved(resolved, workshop_root):
        raise SafetyError(
            f"resolved delete path is outside workshop_root: {resolved}"
        )


def remove_tree_nofollow(path: Path) -> None:
    """Delete a real directory tree. Junctions/symlinks are unlinked, never followed.

    Intentionally not ``shutil.rmtree``: rmtree has followed Windows reparse
    points in some Python versions and could destroy the Junction target.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT or stat.S_ISLNK(st.st_mode):
        remove_junction(path)
        return
    if stat.S_ISDIR(st.st_mode):
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            raise SafetyError(f"cannot scan {path}: {exc}") from exc
        for entry in entries:
            remove_tree_nofollow(Path(entry.path))
        try:
            os.rmdir(path)
        except OSError as exc:
            raise SafetyError(f"cannot rmdir {path}: {exc}") from exc
        return
    try:
        os.unlink(path)
    except OSError as exc:
        raise SafetyError(f"cannot unlink {path}: {exc}") from exc


def assert_target_is_safe(
    smm_target: Path,
    *,
    workshop_path: Path,
    workshop_root: Path,
    smm_mod_root: Path,
) -> None:
    """SMM target is master storage: never a Workshop path, never a reparse point.

    Do not ``resolve()`` a Workshop Junction first — a correct Junction
    already points at the SMM target, so follow-through would look like
    ``target == workshop_path``.
    """
    assert_smm_target_exists(smm_target)
    if is_reparse_point(smm_target):
        raise SafetyError(f"SMM target is a Junction/reparse point: {smm_target}")
    if lexical_norm(smm_target) == lexical_norm(workshop_path):
        raise SafetyError(f"SMM target equals Workshop path: {smm_target}")
    if lexical_norm(smm_target) == lexical_norm(workshop_root):
        raise SafetyError(f"SMM target equals workshop_root: {smm_target}")
    if lexical_norm(smm_target) == lexical_norm(smm_mod_root):
        raise SafetyError(f"SMM target equals smm_mod_root: {smm_target}")
    if not is_under_resolved(smm_target, smm_mod_root):
        raise SafetyError(f"SMM target is not under smm_mod_root: {smm_target}")
    if is_reparse_point(workshop_path):
        return
    if paths_point_same(smm_target, workshop_path):
        raise SafetyError(f"SMM target equals Workshop path: {smm_target}")
    if paths_point_same(smm_target, workshop_root):
        raise SafetyError(f"SMM target equals workshop_root: {smm_target}")
    if is_under_resolved(smm_target, workshop_root):
        raise SafetyError(f"SMM target is inside workshop_root: {smm_target}")


def delete_workshop_path(
    workshop_path: Path,
    *,
    workshop_root: Path,
    smm_target: Path,
    smm_mod_root: Path,
) -> str:
    """Delete a Workshop folder or Junction. Returns ``junction`` or ``directory``."""
    if not lexists(workshop_path):
        return "missing"
    assert_roots_exist(workshop_root, smm_mod_root)
    assert_target_is_safe(
        smm_target,
        workshop_path=workshop_path,
        workshop_root=workshop_root,
        smm_mod_root=smm_mod_root,
    )
    assert_target_is_not_smm_source(
        workshop_path,
        smm_target,
        smm_mod_root,
        workshop_root=workshop_root,
    )
    if is_reparse_point(workshop_path):
        remove_junction(workshop_path)
        return "junction"
    remove_tree_nofollow(workshop_path)
    if lexists(workshop_path):
        raise SafetyError(f"workshop path still exists after delete: {workshop_path}")
    return "directory"
