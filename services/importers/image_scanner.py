"""Pick a primary cover image from an imported Mod source folder."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from services.file_ops import COVER_BASENAME, INFO_DIR_NAME

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# Filename priority (lower index wins). Task 3 order.
_PRIORITY_NAMES = ("cover", "thumbnail", "preview", "icon", "image", "thumb", "header")
_SKIP_DIRS = {".info", "info", ".git", "__pycache__", "node_modules", ".vs"}


def is_image_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _priority_rank(path: Path) -> int:
    stem = path.stem.lower()
    for index, key in enumerate(_PRIORITY_NAMES):
        if key in stem:
            return index
    return 100


def _pixel_area(path: Path) -> int:
    try:
        from PySide6.QtGui import QImage

        img = QImage(str(path))
        if not img.isNull():
            return max(0, img.width()) * max(0, img.height())
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _collect_images(root: Path, *, recursive: bool) -> list[Path]:
    if not root.is_dir():
        return []
    images: list[Path] = []
    iterator: Iterable[Path] = root.rglob("*") if recursive else root.iterdir()
    for path in iterator:
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(p in _SKIP_DIRS for p in parts):
            continue
        images.append(path)
    return images


def find_cover_candidate(
    source_folder: str | Path,
    *,
    recursive: bool = True,
) -> Path | None:
    """
    Scan *source_folder* for a primary cover image.

    Priority:
    1. Filename contains cover / thumbnail / preview / icon / image
    2. Largest image (pixel area, else file size)
    """
    root = Path(source_folder).expanduser().resolve()
    images = _collect_images(root, recursive=recursive)
    if not images:
        return None
    images.sort(key=lambda p: (_priority_rank(p), -_pixel_area(p), p.name.lower()))
    return images[0]


def find_cover_candidate_in_roots(
    roots: Sequence[str | Path],
    *,
    recursive_roots: Sequence[str | Path] | None = None,
    flat_roots: Sequence[str | Path] | None = None,
) -> Path | None:
    """
    Pick the best cover across several folders.

    *roots* / *recursive_roots* → recursive scan (extracted Mod tree).
    *flat_roots* → non-recursive (archive sibling images next to ``.zip``).
    """
    images: list[Path] = []
    seen: set[Path] = set()

    def _add(folder: str | Path, *, recursive: bool) -> None:
        for img in _collect_images(Path(folder).expanduser().resolve(), recursive=recursive):
            key = img.resolve()
            if key in seen:
                continue
            seen.add(key)
            images.append(img)

    for folder in list(roots) + list(recursive_roots or ()):
        _add(folder, recursive=True)
    for folder in flat_roots or ():
        _add(folder, recursive=False)

    if not images:
        return None
    images.sort(key=lambda p: (_priority_rank(p), -_pixel_area(p), p.name.lower()))
    return images[0]


def install_cover_file(candidate: Path, managed_path: str | Path) -> Path | None:
    """Copy *candidate* into ``managed_path/.info/preview.<ext>``."""
    if candidate is None or not Path(candidate).is_file():
        return None
    dest_root = Path(managed_path)
    info = dest_root / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    ext = Path(candidate).suffix.lower() or ".png"
    if ext not in IMAGE_SUFFIXES:
        ext = ".png"
    target = info / f"{COVER_BASENAME}{ext}"
    for old in info.glob(f"{COVER_BASENAME}.*"):
        if old.resolve() != target.resolve():
            try:
                old.unlink()
            except OSError:
                pass
    try:
        shutil.copy2(candidate, target)
    except OSError as exc:
        logger.warning("Failed to install cover %s -> %s: %s", candidate, target, exc)
        return None
    return target


def install_cover_from_source(
    source_folder: str | Path,
    managed_path: str | Path,
    *,
    extra_flat_roots: Sequence[str | Path] | None = None,
    extra_recursive_roots: Sequence[str | Path] | None = None,
) -> Path | None:
    """
    Copy the best cover into ``managed_path/.info/preview.<ext>``.

    Returns the installed path, or ``None`` when no image was found.
    """
    candidate = find_cover_candidate_in_roots(
        [source_folder],
        recursive_roots=extra_recursive_roots,
        flat_roots=extra_flat_roots,
    )
    if candidate is None:
        return None
    return install_cover_file(candidate, managed_path)
