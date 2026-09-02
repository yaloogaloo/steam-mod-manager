"""Tests for deploy filesystem helpers — bounded safe traversal."""

from __future__ import annotations

import os
from pathlib import Path

from services.deploy_fs import safe_has_any_file, safe_iter_dirs, safe_iter_files
from services.deploy_rules.generic import _iter_deployable_files


def test_safe_iter_files_finds_nested(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "sub" / "c.txt").write_text("c", encoding="utf-8")
    names = {p.name for p in safe_iter_files(root)}
    assert names == {"a.txt", "b.txt", "c.txt"}


def test_safe_iter_files_skips_symlink(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    root.mkdir()
    real = root / "real.txt"
    real.write_text("ok", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(real, link)
    except OSError:
        return
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "inner.txt").write_text("x", encoding="utf-8")

    names = {p.name for p in safe_iter_files(root)}
    assert "real.txt" in names
    assert "inner.txt" in names
    assert "link.txt" not in names


def test_safe_iter_files_self_symlink_loop_terminates(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    root.mkdir()
    (root / "real").mkdir()
    (root / "real" / "f.txt").write_text("x", encoding="utf-8")
    loop = root / "loop"
    try:
        os.symlink(root, loop, target_is_directory=True)
    except OSError:
        return
    files = list(safe_iter_files(root))
    assert any(p.name == "f.txt" for p in files)
    assert all("loop" not in p.parts for p in files)


def test_safe_iter_files_suffix_and_name(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    root.mkdir()
    (root / "a.pak").write_bytes(b"x")
    (root / "b.txt").write_text("y", encoding="utf-8")
    (root / "info.ini").write_text("z", encoding="utf-8")
    assert [p.name for p in safe_iter_files(root, suffix=".pak")] == ["a.pak"]
    assert [p.name for p in safe_iter_files(root, name="info.ini")] == ["info.ini"]


def test_safe_iter_dirs_by_name(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    (root / "nested" / "stamps").mkdir(parents=True)
    (root / "other").mkdir()
    found = list(safe_iter_dirs(root, name="stamps"))
    assert len(found) == 1
    assert found[0].name == "stamps"


def test_safe_has_any_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert safe_has_any_file(empty) is False
    (empty / "x.txt").write_text("1", encoding="utf-8")
    assert safe_has_any_file(empty) is True


def test_iter_deployable_files_uses_safe_walk(tmp_path: Path) -> None:
    mod = tmp_path / "Game" / "TestMod"
    info = mod / ".info"
    info.mkdir(parents=True)
    (mod / "data.pak").write_bytes(b"x" * 10)
    (info / "metadata.json").write_text("{}", encoding="utf-8")

    files = _iter_deployable_files(mod)
    assert any(p.name == "data.pak" for p in files)
    assert all(".info" not in p.parts for p in files)


def test_windows_junction_loop_terminates(tmp_path: Path) -> None:
    """Windows junction → parent must not recurse forever."""
    if os.name != "nt":
        return
    root = tmp_path / "mod"
    (root / "real").mkdir(parents=True)
    (root / "real" / "ok.txt").write_text("x", encoding="utf-8")
    junction = root / "junc"
    try:
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(root)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    files = list(safe_iter_files(root))
    assert any(p.name == "ok.txt" for p in files)
    assert all(p.name != "junc" or False for p in files)
    assert all("junc" not in p.parts for p in files)
