"""Windows MoveFileExW rename helper."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="MoveFileExW is Windows-only")
def test_move_directory_movefile_ex_renames(tmp_path: Path) -> None:
    from services.windows_rename import move_directory_movefile_ex

    src = tmp_path / "源目录 [A]"
    src.mkdir()
    (src / "f.txt").write_text("ok", encoding="utf-8")
    dest = tmp_path / "Target Dir [B]"

    move_directory_movefile_ex(str(src.resolve()), str(dest.resolve()))
    assert dest.is_dir()
    assert not src.exists()
    assert (dest / "f.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="MoveFileExW is Windows-only")
def test_move_directory_movefile_ex_missing_source(tmp_path: Path) -> None:
    from services.windows_rename import move_directory_movefile_ex

    src = tmp_path / "missing"
    dest = tmp_path / "dest"
    with pytest.raises(OSError):
        move_directory_movefile_ex(str(src), str(dest))
