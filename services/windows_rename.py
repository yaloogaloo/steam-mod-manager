"""Windows-native directory rename (MoveFileExW).

Used as a fallback when ``os.rename`` hits transient WinError 5.
Does not use ``shutil.move`` (copy/delete).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

# winbase.h
MOVEFILE_REPLACE_EXISTING = 0x00000001


def move_directory_movefile_ex(src: str, dst: str) -> None:
    """
    Rename/move a directory via ``MoveFileExW``.

    Raises ``OSError`` / ``PermissionError`` on failure.
    On non-Windows platforms raises ``OSError``.
    """
    if sys.platform != "win32":
        raise OSError("MoveFileExW is only available on Windows")

    src_s = os.fspath(src)
    dst_s = os.fspath(dst)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex_w = kernel32.MoveFileExW
    move_file_ex_w.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    move_file_ex_w.restype = wintypes.BOOL

    ok = bool(move_file_ex_w(src_s, dst_s, MOVEFILE_REPLACE_EXISTING))
    if ok:
        logger.debug("MoveFileExW ok: %s → %s", src_s, dst_s)
        return

    err = int(ctypes.get_last_error() or 0)
    # ERROR_ACCESS_DENIED = 5
    if err == 5:
        raise PermissionError(err, "Access is denied", src_s)
    raise ctypes.WinError(err)
