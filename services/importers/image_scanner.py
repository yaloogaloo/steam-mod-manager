"""Backward-compatible exports — cover auto-scan removed.

Use :mod:`services.importers.image_picker` for validate / install / suggest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from services.importers.image_picker import (
    IMAGE_EXTENSIONS,
    IMAGE_SUFFIXES,
    apply_cover_to_mod,
    cleanup_old_auto_cover,
    install_cover_file,
    is_image_path,
    relative_cover_path,
    resolve_cover_file,
    suggest_sibling_covers,
    validate_cover_image,
)

__all__ = [
    "IMAGE_EXTENSIONS",
    "IMAGE_SUFFIXES",
    "apply_cover_to_mod",
    "cleanup_old_auto_cover",
    "find_cover_candidate",
    "find_cover_candidate_in_roots",
    "install_cover_file",
    "install_cover_from_source",
    "is_image_path",
    "relative_cover_path",
    "resolve_cover_file",
    "suggest_sibling_covers",
    "validate_cover_image",
]


def find_cover_candidate(
    source_folder: str | Path,
    *,
    recursive: bool = True,
) -> Path | None:
    """Deprecated: auto cover discovery removed — always returns ``None``."""
    del source_folder, recursive
    return None


def find_cover_candidate_in_roots(
    roots: Sequence[str | Path],
    *,
    recursive_roots: Sequence[str | Path] | None = None,
    flat_roots: Sequence[str | Path] | None = None,
) -> Path | None:
    """Deprecated: auto cover discovery removed — always returns ``None``."""
    del roots, recursive_roots, flat_roots
    return None


def install_cover_from_source(
    source_folder: str | Path,
    managed_path: str | Path,
    *,
    extra_flat_roots: Sequence[str | Path] | None = None,
    extra_recursive_roots: Sequence[str | Path] | None = None,
    cover_source: str | Path | None = None,
) -> Path | None:
    """
    Install an **explicit** cover only.

    Auto-scan of *source_folder* / sibling roots is disabled. Pass *cover_source*.
    """
    del source_folder, extra_flat_roots, extra_recursive_roots
    if cover_source is None:
        return None
    return install_cover_file(cover_source, managed_path)
