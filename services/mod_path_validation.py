"""Mod root path validation — Path Lifecycle / Import gate.

Structural rules (not a temporary path blacklist):

- The application / repository root must never be a Mod root.
- The managed library root and ``data/`` root must never be a Mod root.
- A managed ``last_known_path`` must live under the library as
  ``<library>/<game>/<mod>/`` (depth >= 2).

Import *sources* may live outside the library (Workshop, user folders), but
still may not be the application root, library root, or data root.
"""

from __future__ import annotations

from pathlib import Path

from core.paths import data_dir, default_mod_library, project_root

__all__ = [
    "InvalidModRootError",
    "FORBIDDEN_MOD_ROOT_REASON",
    "is_application_tree_root",
    "is_forbidden_mod_root",
    "is_valid_managed_mod_path",
    "validate_import_source_root",
    "validate_managed_mod_path",
]

FORBIDDEN_MOD_ROOT_REASON = (
    "工程目录 / 库根 / 数据根不能成为 Mod root（Path Lifecycle 校验）"
)


class InvalidModRootError(ValueError):
    """Raised when a path is structurally forbidden as a Mod root."""


def _resolve(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return None


def is_application_tree_root(path: str | Path) -> bool:
    """True when *path* is the Steam Mod Manager application / project root."""
    resolved = _resolve(path)
    if resolved is None:
        return False
    try:
        return resolved == project_root().resolve()
    except OSError:
        return False


def is_forbidden_mod_root(
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> bool:
    """
    True when *path* must never be treated as a Mod root / ``last_known_path``.

    Forbidden roots: application root, ``data/``, managed library root.
    """
    resolved = _resolve(path)
    if resolved is None:
        return True
    try:
        app = project_root().resolve()
        data = data_dir().resolve()
        lib = (
            Path(library_root).expanduser().resolve()
            if library_root is not None
            else default_mod_library().resolve()
        )
    except OSError:
        return True
    return resolved in {app, data, lib}


def is_valid_managed_mod_path(
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> bool:
    """
    True when *path* is a managed Mod folder under the library.

    Shape: ``<library_root>/<game_folder>/<mod_folder>/`` (depth >= 2).
    """
    resolved = _resolve(path)
    if resolved is None or is_forbidden_mod_root(resolved, library_root=library_root):
        return False
    try:
        lib = (
            Path(library_root).expanduser().resolve()
            if library_root is not None
            else default_mod_library().resolve()
        )
        rel = resolved.relative_to(lib)
    except (OSError, ValueError):
        return False
    return len(rel.parts) >= 2


def validate_import_source_root(
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> Path:
    """
    Gate for Import / directory discovery source folders.

    External sources are allowed; application / library / data roots are not.
    """
    resolved = _resolve(path)
    if resolved is None or not resolved.is_dir():
        raise InvalidModRootError("Mod 源目录不存在")
    if is_forbidden_mod_root(resolved, library_root=library_root):
        raise InvalidModRootError(FORBIDDEN_MOD_ROOT_REASON)
    return resolved


def validate_managed_mod_path(
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> Path:
    """Gate for Path Lifecycle commits that bind ``last_known_path``."""
    resolved = _resolve(path)
    if resolved is None or not resolved.is_dir():
        raise InvalidModRootError("目标目录不存在")
    if not is_valid_managed_mod_path(resolved, library_root=library_root):
        raise InvalidModRootError(FORBIDDEN_MOD_ROOT_REASON)
    return resolved
