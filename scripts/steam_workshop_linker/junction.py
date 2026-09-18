"""Windows Junction helpers. Standard library only."""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


class JunctionError(RuntimeError):
    """Junction create/remove/inspect failed."""


def _lstat_attrs(path: Path) -> int:
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    if attrs:
        return attrs
    if os.name != "nt":
        return 0
    get_attrs = ctypes.windll.kernel32.GetFileAttributesW
    get_attrs.argtypes = [ctypes.c_wchar_p]
    get_attrs.restype = ctypes.c_uint32
    raw = int(get_attrs(str(path)))
    if raw == INVALID_FILE_ATTRIBUTES:
        return 0
    return raw


def is_reparse_point(path: Path) -> bool:
    attrs = _lstat_attrs(path)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    try:
        return path.is_symlink()
    except OSError:
        return False


def is_junction(path: Path) -> bool:
    """True for a Windows Junction (mount-point reparse) or directory reparse link."""
    if not is_reparse_point(path):
        return False
    try:
        tag = int(getattr(os.lstat(path), "st_reparse_tag", 0) or 0)
    except OSError:
        tag = 0
    if tag == IO_REPARSE_TAG_MOUNT_POINT:
        return True
    if os.name == "nt":
        return True
    try:
        return path.is_symlink()
    except OSError:
        return False


def lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def _strip_nt_prefix(raw: str) -> str:
    text = str(raw or "")
    for prefix in ("\\\\?\\UNC\\", "\\??\\UNC\\"):
        if text.startswith(prefix):
            return "\\\\" + text[len(prefix) :]
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def get_junction_target(path: Path) -> Path | None:
    if not is_reparse_point(path):
        return None
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    cleaned = _strip_nt_prefix(str(raw)).rstrip("\\/")
    if not cleaned:
        return None
    return Path(cleaned)


def remove_junction(path: Path) -> None:
    """Delete the Junction / reparse point itself. Never walks the target."""
    if not lexists(path):
        return
    if not is_reparse_point(path):
        raise JunctionError(f"refusing remove_junction on non-reparse path: {path}")
    try:
        os.rmdir(path)
        return
    except OSError:
        pass
    try:
        path.unlink()
    except OSError as exc:
        raise JunctionError(f"failed to remove junction {path}: {exc}") from exc
    if lexists(path):
        raise JunctionError(f"junction still exists after remove: {path}")


def create_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        raise JunctionError("mklink /J requires Windows")
    if not target.is_dir():
        raise JunctionError(f"SMM target is not a directory: {target}")
    if lexists(link):
        raise JunctionError(f"cannot create junction; path already exists: {link}")
    completed = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode != 0 or not is_junction(link):
        raise JunctionError(
            f"mklink /J failed for {link} -> {target}"
            + (f": {detail}" if detail else f" (exit {completed.returncode})")
        )


def paths_point_same(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return lexical_norm(left) == lexical_norm(right)


def lexical_norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def junction_points_at(link: Path, target: Path) -> bool:
    pointed = get_junction_target(link)
    if pointed is not None and paths_point_same(pointed, target):
        return True
    if pointed is not None and lexical_norm(pointed) == lexical_norm(target):
        return True
    try:
        return paths_point_same(link, target)
    except OSError:
        return False
