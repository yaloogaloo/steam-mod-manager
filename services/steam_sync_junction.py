"""Read-only Windows Junction helpers for Steam Sync copy short-circuit.

Does not create, delete, or repair Junctions. Independent of
``scripts/steam_workshop_linker`` (no runtime → scripts import).

Steam Workshop linker architecture::

    workshop/<workspace_id>   = Junction
    SMM managed folder        = real destination

``ModFileManager.copy_mod`` would ``rmtree(destination)`` then copytree from
the Workshop source — a self-copy that destroys the only real files.
"""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _file_attributes(path: Path) -> int:
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
    return 0 if raw == INVALID_FILE_ATTRIBUTES else raw


def is_junction(path: Path) -> bool:
    """True when *path itself* is a Junction / directory reparse point.

    Uses ``lstat`` — does not ``Path.resolve()`` first.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    attrs = _file_attributes(path)
    if not (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    try:
        tag = int(getattr(os.lstat(path), "st_reparse_tag", 0) or 0)
    except OSError:
        tag = 0
    if tag == IO_REPARSE_TAG_MOUNT_POINT:
        return True
    return os.name == "nt"


def _strip_nt_prefix(raw: str) -> str:
    text = str(raw or "")
    for prefix in ("\\\\?\\UNC\\", "\\??\\UNC\\"):
        if text.startswith(prefix):
            return "\\\\" + text[len(prefix) :]
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def resolve_junction_target(path: Path) -> Path | None:
    """Return the Junction target path, or None. Does not follow *path* via resolve()."""
    if not is_junction(path):
        return None
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    cleaned = _strip_nt_prefix(str(raw)).rstrip("\\/")
    if not cleaned:
        return None
    return Path(cleaned)


def canonicalize_win_path(path: Path | str) -> str:
    """Case-/slash-insensitive Windows path identity without following Junctions."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def paths_equivalent(left: Path | str, right: Path | str) -> bool:
    return canonicalize_win_path(left) == canonicalize_win_path(right)


@dataclass(frozen=True)
class SteamSyncJunctionDecision:
    skip_physical_copy: bool
    mismatch: bool = False
    expected: str = ""
    actual: str = ""


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def evaluate_steam_sync_update_copy(
    *,
    source: Path,
    destination: Path,
    workspace_id: str,
) -> SteamSyncJunctionDecision:
    """
    Decide whether Steam Sync may skip ``copy_mod`` for an already-registered
    Mod that needs a physical update.

    Skip only when we can prove the Workshop *source* is a Junction whose
    target is this registered SMM *destination* (same ``workspace_id``).

    If *destination* itself is a Junction, skip only when both sides resolve
    to the same target and that mapping is still this workspace_id.

    Anything unproven → do not skip (keep existing overwrite copy).
    """
    wid = str(workspace_id or "").strip()
    if not wid.isdecimal():
        return SteamSyncJunctionDecision(False)
    if not str(source) or not _lexists(destination):
        return SteamSyncJunctionDecision(False)
    if source.name != wid:
        return SteamSyncJunctionDecision(False)

    src_is_link = is_junction(source)
    dest_is_link = is_junction(destination)
    if not src_is_link and not dest_is_link:
        return SteamSyncJunctionDecision(False)

    src_target = resolve_junction_target(source) if src_is_link else None
    dest_target = resolve_junction_target(destination) if dest_is_link else None

    if src_is_link and not dest_is_link:
        actual = str(src_target) if src_target is not None else ""
        if src_target is not None and paths_equivalent(src_target, destination):
            return SteamSyncJunctionDecision(True, expected=str(destination), actual=actual)
        return SteamSyncJunctionDecision(
            False,
            mismatch=src_target is not None,
            expected=str(destination),
            actual=actual,
        )

    if dest_is_link and src_is_link:
        if (
            src_target is not None
            and dest_target is not None
            and paths_equivalent(src_target, dest_target)
        ):
            return SteamSyncJunctionDecision(
                True,
                expected=str(dest_target),
                actual=str(src_target),
            )
        return SteamSyncJunctionDecision(
            False,
            mismatch=True,
            expected=str(dest_target or destination),
            actual=str(src_target or ""),
        )

    # Destination is a Junction, source is a plain directory — not the
    # linker mapping. Do not skip; keep existing overwrite behaviour.
    return SteamSyncJunctionDecision(
        False,
        mismatch=dest_target is not None
        and not paths_equivalent(dest_target, destination),
        expected=str(destination),
        actual=str(dest_target or ""),
    )


def log_steam_sync_junction_decision(
    decision: SteamSyncJunctionDecision,
    *,
    workspace_id: str,
) -> None:
    if decision.skip_physical_copy:
        logger.info(
            "[STEAM SYNC] Skip physical update copy:\n"
            "workspace_id=%s\n"
            "destination is a Junction to the registered SMM Mod directory.",
            workspace_id,
        )
        return
    if decision.mismatch:
        logger.warning(
            "[STEAM SYNC] Junction target mismatch:\n"
            "workspace_id=%s\n"
            "expected=%s\n"
            "actual=%s",
            workspace_id,
            decision.expected,
            decision.actual,
        )
