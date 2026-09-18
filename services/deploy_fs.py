"""Filesystem helpers for deploy — bounded walks without symlink/reparse loops."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path


def _should_skip_path(path: Path) -> bool:
    """Skip symlinks and Windows reparse points (junctions) during deploy scans."""
    from services.deploy_op_profile import record_op

    t0 = time.perf_counter()
    try:
        try:
            if path.is_symlink():
                return True
        except OSError:
            return True
        if os.name == "nt":
            try:
                attrs = path.lstat().st_file_attributes
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
            except (AttributeError, OSError):
                pass
        return False
    finally:
        record_op(
            "lstat",
            (time.perf_counter() - t0) * 1000.0,
            path=str(path),
        )


def safe_iter_files(
    root: Path,
    *,
    suffix: str | None = None,
    name: str | None = None,
    predicate: Callable[[Path], bool] | None = None,
) -> Iterator[Path]:
    """
    Yield regular files under *root* without following symlinks/junctions.

    Uses ``os.walk(followlinks=False)`` — one bounded tree walk, no ``rglob``.

    Optional filters (all AND-ed when set):
    - ``suffix``: case-insensitive extension (e.g. ``\".pak\"``)
    - ``name``: case-insensitive exact filename (e.g. ``\"info.ini\"``)
    - ``predicate``: extra callable; return False to skip
    """
    base = Path(root).expanduser()
    try:
        base = base.resolve()
    except OSError:
        return
    if not base.is_dir():
        return

    name_l = name.lower() if name else None
    suffix_l = suffix.lower() if suffix else None

    from services.deploy_op_profile import note_tree, record_op

    walk_ms = 0.0
    file_count = 0
    dir_count = 0
    t_seg = time.perf_counter()
    try:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            dir_count += 1
            dirnames[:] = [
                dname
                for dname in dirnames
                if not _should_skip_path(current / dname)
            ]
            for fname in filenames:
                path = current / fname
                if _should_skip_path(path):
                    continue
                if name_l is not None and fname.lower() != name_l:
                    continue
                if suffix_l is not None and Path(fname).suffix.lower() != suffix_l:
                    continue
                if predicate is not None:
                    try:
                        if not predicate(path):
                            continue
                    except OSError:
                        continue
                file_count += 1
                walk_ms += (time.perf_counter() - t_seg) * 1000.0
                yield path
                t_seg = time.perf_counter()
        walk_ms += (time.perf_counter() - t_seg) * 1000.0
    finally:
        record_op("os.walk", walk_ms, path=str(base))
        note_tree(
            stage="walk",
            kind="source",
            root=str(base),
            file_count=file_count,
            directory_count=dir_count,
        )


def safe_iter_dirs(
    root: Path,
    *,
    name: str | None = None,
) -> Iterator[Path]:
    """
    Yield directories under *root* (not including *root*) without following links.

    ``name`` matches the directory basename case-insensitively when set.
    """
    base = Path(root).expanduser()
    try:
        base = base.resolve()
    except OSError:
        return
    if not base.is_dir():
        return

    name_l = name.lower() if name else None

    for dirpath, dirnames, _filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for dname in dirnames:
            child = current / dname
            if _should_skip_path(child):
                continue
            kept.append(dname)
            if name_l is not None and dname.lower() != name_l:
                continue
            yield child
        dirnames[:] = kept


def safe_has_any_file(root: Path) -> bool:
    """True if *root* contains at least one regular file (bounded walk)."""
    for _ in safe_iter_files(root):
        return True
    return False
