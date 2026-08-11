"""Directory content-move fallback when atomic rename is denied (Windows).

Used when ``os.rename`` / ``MoveFileExW`` fail with WinError 5 on a specific
folder, but sibling folders rename fine. Does not merge into an existing target.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_access_denied(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror == 5:
        return True
    errno = getattr(exc, "errno", None)
    if errno in (13, 5):
        return True
    text = str(exc).lower()
    return "access is denied" in text or "winerror 5" in text or "拒绝访问" in text


def _count_files(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file():
            total += 1
    return total


def move_directory_with_fallback(source: Path, target: Path) -> Path:
    """
    Create *target*, move all children from *source* into it, remove empty source.

    Raises ``FileExistsError`` if *target* already exists (no merge/overwrite).
    On failure, does not delete *source* (may be partially emptied).
    """
    src = Path(source).expanduser()
    dst = Path(target).expanduser()
    try:
        src = src.resolve()
    except OSError:
        pass
    try:
        dst = dst.resolve()
    except OSError:
        pass

    if not src.is_dir():
        raise FileNotFoundError(f"Fallback move source missing: {src}")
    if dst.exists():
        raise FileExistsError(f"Rename target already exists: {dst}")

    logger.info(
        "Directory rename fallback started: source=%s target=%s",
        src,
        dst,
    )

    moved_entries = 0
    moved_files = 0
    created_target = False

    try:
        dst.mkdir(parents=False, exist_ok=False)
        created_target = True

        children = list(src.iterdir())
        for child in children:
            dest_child = dst / child.name
            before = _count_files(child) if child.is_dir() else (1 if child.is_file() else 0)
            shutil.move(str(child), str(dest_child))
            moved_entries += 1
            moved_files += before

        remaining = list(src.iterdir())
        if remaining:
            names = [p.name for p in remaining]
            raise OSError(f"Source not empty after fallback move: {names}")

        if not dst.is_dir():
            raise OSError(f"Fallback target missing after move: {dst}")

        src.rmdir()
        logger.info(
            "Directory fallback move completed: moved_files=%s moved_entries=%s "
            "source=%s target=%s",
            moved_files,
            moved_entries,
            src,
            dst,
        )
        return dst
    except Exception as exc:
        logger.error(
            "Directory fallback move failed: source=%s target=%s "
            "moved_files=%s moved_entries=%s error=%s",
            src,
            dst,
            moved_files,
            moved_entries,
            exc,
        )
        # Never delete source. Drop an empty unused target we just created.
        if created_target and moved_entries == 0 and dst.is_dir():
            try:
                if not any(dst.iterdir()):
                    dst.rmdir()
            except OSError:
                pass
        raise


def rename_directory_or_fallback(
    source: Path | str,
    target: Path | str,
    *,
    rename_once,
) -> Path:
    """
    Try *rename_once(source, target)* once; on access-denied use content move.

    *rename_once* should perform a single atomic rename attempt (no long retry).
    """
    src = Path(source)
    dst = Path(target)
    try:
        return Path(rename_once(src, dst))
    except OSError as exc:
        if not _is_access_denied(exc):
            raise
        logger.info(
            "Atomic rename denied (%s); switching to content-move fallback",
            exc,
        )
        return move_directory_with_fallback(src, dst)
