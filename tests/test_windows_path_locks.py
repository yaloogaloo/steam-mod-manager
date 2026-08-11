"""Windows path-lock diagnostics (rename WinError 5)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from services.windows_path_locks import (
    _classify_process,
    find_processes_locking_path,
    summarize_lock_holders,
)


def test_classify_self_pid_as_internal() -> None:
    assert _classify_process("python.exe", os.getpid()) == "internal"
    assert _classify_process("explorer.exe", 1) == "external"
    assert _classify_process("MsMpEng.exe", 2) == "external"
    assert _classify_process("SearchIndexer.exe", 3) == "external"
    assert _classify_process("msedge.exe", 4) == "external"


def test_summarize_empty_holders() -> None:
    text = summarize_lock_holders([])
    assert "no lock holder identified" in text


def test_summarize_holders_lists_names() -> None:
    text = summarize_lock_holders(
        [
            {
                "pid": 100,
                "name": "explorer.exe",
                "class": "external",
                "source": "restart_manager",
                "detail": "Explorer",
            }
        ]
    )
    assert "explorer.exe" in text
    assert "external" in text


def test_find_processes_merges_probe_results(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()

    fake = [
        {
            "pid": 42,
            "name": "explorer.exe",
            "source": "restart_manager",
            "detail": "x",
            "class": "external",
        }
    ]
    with (
        patch(
            "services.windows_path_locks._via_restart_manager",
            return_value=fake,
        ),
        patch("services.windows_path_locks._via_handle_exe", return_value=[]),
        patch("services.windows_path_locks._via_psutil", return_value=[]),
    ):
        hits = find_processes_locking_path(folder)
    assert hits == fake


def test_rename_retries_without_lock_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access-denied retries still run; lock probing is not required."""
    from services.metadata_refresh import safe_directory_rename

    src = tmp_path / "Locked"
    src.mkdir()
    (src / ".info").mkdir()
    (src / ".info" / "metadata.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "Target"

    calls = {"n": 0}
    sleeps: list[float] = []

    def always_locked(src_s: str, dst_s: str):
        calls["n"] += 1
        raise PermissionError(5, "Access is denied", src_s)

    monkeypatch.setattr("services.metadata_refresh.os.rename", always_locked)
    monkeypatch.setattr(
        "services.windows_rename.move_directory_movefile_ex",
        always_locked,
    )
    monkeypatch.setattr(
        "services.metadata_refresh.time.sleep",
        lambda sec: sleeps.append(float(sec)),
    )

    with pytest.raises(PermissionError):
        safe_directory_rename(src, dest, attempts=3, delay_sec=0.05)

    assert calls["n"] == 6  # 3 attempts × (rename + MoveFileExW)
    assert sleeps == [0.05, 0.05]


def test_audit_self_open_files_reports_list(tmp_path: Path) -> None:
    from services.windows_path_locks import audit_self_open_files

    folder = tmp_path / "mod"
    folder.mkdir()
    # Just ensure callable; may be empty without holding a handle.
    result = audit_self_open_files(folder)
    assert isinstance(result, list)
