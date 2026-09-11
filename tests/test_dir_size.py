"""Directory size accounting: skip rules, hidden files, errors, empty dirs."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from services.dir_size import directory_size, reset_directory_size_cache


def test_directory_size_skips_offline_assets_and_cache(tmp_path: Path) -> None:
    reset_directory_size_cache()
    root = tmp_path / "Mod"
    info = root / ".info"
    (info / "offline" / "assets").mkdir(parents=True)
    (info / "assets").mkdir(parents=True)
    (root / ".cache").mkdir()
    (root / "payload.bin").write_bytes(b"x" * 100)
    (info / "metadata.json").write_bytes(b"y" * 20)
    (info / "offline" / "index.html").write_bytes(b"z" * 5000)
    (info / "offline" / "assets" / "big.png").write_bytes(b"z" * 8000)
    (info / "assets" / "cover.png").write_bytes(b"z" * 3000)
    (root / ".cache" / "tmp").write_bytes(b"z" * 2000)

    total = directory_size(root)
    assert total == 120
    assert directory_size(root) == 120


def test_directory_size_nested_files_and_empty(tmp_path: Path) -> None:
    reset_directory_size_cache()
    empty = tmp_path / "Empty"
    empty.mkdir()
    assert directory_size(empty) == 0

    nested = tmp_path / "Nested"
    (nested / "a" / "b").mkdir(parents=True)
    (nested / "a" / "one.bin").write_bytes(b"x" * 10)
    (nested / "a" / "b" / "two.bin").write_bytes(b"y" * 15)
    assert directory_size(nested) == 25


def test_directory_size_includes_info_cover_and_hidden(tmp_path: Path) -> None:
    reset_directory_size_cache()
    root = tmp_path / "Covered"
    info = root / ".info"
    info.mkdir(parents=True)
    (info / "cover.png").write_bytes(b"c" * 40)
    hidden = root / ".dotfile"
    hidden.write_bytes(b"h" * 8)
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    assert directory_size(root) == 48


def test_directory_size_skips_directory_symlink(tmp_path: Path) -> None:
    reset_directory_size_cache()
    root = tmp_path / "Root"
    other = tmp_path / "Other"
    root.mkdir()
    other.mkdir()
    (root / "local.bin").write_bytes(b"a" * 5)
    (other / "big.bin").write_bytes(b"b" * 500)
    link = root / "linked"
    try:
        link.symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not permitted")
    assert directory_size(root) == 5


def test_directory_size_skips_individual_getsize_oserror(tmp_path: Path) -> None:
    reset_directory_size_cache()
    root = tmp_path / "Err"
    root.mkdir()
    good = root / "ok.bin"
    bad = root / "bad.bin"
    good.write_bytes(b"x" * 11)
    bad.write_bytes(b"y" * 9)
    real = os.path.getsize

    def _getsize(path: str | bytes | os.PathLike[str]) -> int:
        if Path(path) == bad:
            raise OSError("denied")
        return real(path)

    with patch("os.path.getsize", side_effect=_getsize):
        assert directory_size(root) == 11
