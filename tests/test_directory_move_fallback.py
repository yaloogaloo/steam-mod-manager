"""Directory content-move fallback (WinError 5 rename)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.directory_move import (
    move_directory_with_fallback,
    rename_directory_or_fallback,
)
from services.file_ops import INFO_DIR_NAME


def test_fallback_moves_plain_directory(tmp_path: Path) -> None:
    src = tmp_path / "更好的镜头缩放"
    dst = tmp_path / "Zoom Out In Further (Serp)"
    src.mkdir()
    (src / "mod.txt").write_text("payload", encoding="utf-8")
    (src / "subdir").mkdir()
    (src / "subdir" / "inner.bin").write_bytes(b"\x00\x01")

    out = move_directory_with_fallback(src, dst)
    assert out == dst.resolve()
    assert dst.is_dir()
    assert not src.exists()
    assert (dst / "mod.txt").read_text(encoding="utf-8") == "payload"
    assert (dst / "subdir" / "inner.bin").read_bytes() == b"\x00\x01"


def test_fallback_preserves_info_metadata_and_cover(tmp_path: Path) -> None:
    src = tmp_path / "Old Name"
    dst = tmp_path / "New Name [OK]"
    src.mkdir()
    info = src / INFO_DIR_NAME
    info.mkdir()
    (info / "metadata.json").write_text(
        json.dumps({"title": "Old Name", "source_type": "modio"}),
        encoding="utf-8",
    )
    (info / "cover.jpg").write_bytes(b"fakecover")
    (src / "offline").mkdir()
    (src / "offline" / "index.html").write_text("<html/>", encoding="utf-8")
    (src / "pak").mkdir()
    (src / "pak" / "a.pak").write_text("pak", encoding="utf-8")

    move_directory_with_fallback(src, dst)

    assert not src.exists()
    assert (dst / INFO_DIR_NAME / "metadata.json").is_file()
    meta = json.loads(
        (dst / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    assert meta["title"] == "Old Name"
    assert (dst / INFO_DIR_NAME / "cover.jpg").read_bytes() == b"fakecover"
    assert (dst / "offline" / "index.html").is_file()
    assert (dst / "pak" / "a.pak").read_text(encoding="utf-8") == "pak"


def test_fallback_target_exists_raises(tmp_path: Path) -> None:
    src = tmp_path / "Source"
    dst = tmp_path / "Target"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    (dst / "b.txt").write_text("b", encoding="utf-8")

    with pytest.raises(FileExistsError):
        move_directory_with_fallback(src, dst)

    assert src.is_dir()
    assert (src / "a.txt").is_file()
    assert (dst / "b.txt").read_text(encoding="utf-8") == "b"


def test_fallback_failure_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "SourceKeep"
    dst = tmp_path / "TargetPartial"
    src.mkdir()
    (src / "keep_me.txt").write_text("alive", encoding="utf-8")
    (src / "also.txt").write_text("also", encoding="utf-8")

    real_move = __import__("shutil").move
    calls = {"n": 0}

    def boom(a: str, b: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_move(a, b)
        raise OSError("simulated move failure")

    monkeypatch.setattr("services.directory_move.shutil.move", boom)

    with pytest.raises(OSError, match="simulated move failure"):
        move_directory_with_fallback(src, dst)

    # Source directory must remain (partial content OK; never deleted on failure).
    assert src.is_dir()
    leftover = list(src.iterdir())
    assert leftover, "expected remaining files under source after partial failure"
    assert any(p.name in {"keep_me.txt", "also.txt"} for p in leftover)

def test_rename_failure_enters_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "更好的镜头缩放"
    dst = tmp_path / "Zoom Out In Further (Serp)"
    src.mkdir()
    info = src / INFO_DIR_NAME
    info.mkdir()
    (info / "metadata.json").write_text("{}", encoding="utf-8")
    (src / "file.dat").write_text("x", encoding="utf-8")

    def deny(_src: Path, _dst: Path) -> Path:
        raise PermissionError(5, "Access is denied", str(_src))

    out = rename_directory_or_fallback(src, dst, rename_once=deny)
    assert out == dst.resolve()
    assert dst.is_dir()
    assert not src.exists()
    assert (dst / INFO_DIR_NAME / "metadata.json").is_file()
    assert (dst / "file.dat").read_text(encoding="utf-8") == "x"


def test_modio_rename_uses_fallback_on_winerror5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import modio_metadata_refresh as mod

    parent = tmp_path / "Anno 1800"
    old = parent / "更好的镜头缩放"
    old.mkdir(parents=True)
    info = old / INFO_DIR_NAME
    info.mkdir()
    (info / "metadata.json").write_text(
        json.dumps({"title": "更好的镜头缩放"}),
        encoding="utf-8",
    )
    (old / "content.txt").write_text("ok", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "safe_directory_rename",
        MagicMock(side_effect=PermissionError(5, "Access is denied", str(old))),
    )
    monkeypatch.setattr(mod, "prepare_managed_folder_for_rename", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mod,
        "collect_directory_rename_diagnostics",
        lambda src, dst: {
            "source": str(src),
            "target": str(dst),
            "target_exists": False,
            "source_exists": True,
            "source_writable": True,
            "has_info": True,
            "metadata_mtime": "",
            "cover_inflight": 0,
            "cover_active_tokens": 0,
            "metadata_file": "closed",
            "process_cwd": str(tmp_path),
            "cwd_under_source": False,
        },
    )

    new_path, renamed = mod.rename_modio_folder_for_title(
        old, "Zoom Out In Further (Serp)"
    )
    assert renamed is True
    assert new_path.name == "Zoom Out In Further (Serp)"
    assert new_path.is_dir()
    assert not old.exists()
    assert (new_path / "content.txt").read_text(encoding="utf-8") == "ok"
    assert (new_path / INFO_DIR_NAME / "metadata.json").is_file()
