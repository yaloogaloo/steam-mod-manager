"""Runtime dependency checks and RAR error classification."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path

import pytest

from services.importers.archive import (
    RAR_ERROR_EXECUTION_FAILED,
    RAR_ERROR_PYTHON_SUPPORT_MISSING,
    RAR_ERROR_TOOL_UNAVAILABLE,
    RAR_PYTHON_SUPPORT_MISSING_MSG,
    RAR_TOOL_UNAVAILABLE_MSG,
    RarExtractError,
    TOOL_UNAVAILABLE_MSG,
    _extract_rar_with_rarfile,
    extract_archive,
)
from services.runtime.dependency_check import (
    RuntimeDependency,
    check_runtime_dependencies,
    format_missing_dependency_message,
    log_runtime_dependencies,
    missing_required_dependencies,
)


def _fake_rar(path: Path) -> None:
    path.write_bytes(b"Rar!\x1a\x07\x00not-a-real-rar")


def test_missing_rarfile_dependency_reports_python_dependency_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rar = tmp_path / "mod.rar"
    _fake_rar(rar)
    bundled = tmp_path / "UnRAR.exe"
    bundled.write_bytes(b"MZ")
    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool",
        lambda: bundled,
    )

    real_import = builtins.__import__

    def _fake_import(
        name: str,
        globals=None,
        locals=None,
        fromlist=(),
        level: int = 0,
    ):
        if name == "rarfile":
            raise ImportError("No module named 'rarfile'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(RarExtractError) as exc:
        extract_archive(rar, dest_dir=tmp_path / "out")

    assert exc.value.code == RAR_ERROR_PYTHON_SUPPORT_MISSING
    assert exc.value.args[0] == RAR_PYTHON_SUPPORT_MISSING_MSG
    assert exc.value.args[0] != TOOL_UNAVAILABLE_MSG


def test_requirements_dependencies_available() -> None:
    statuses = check_runtime_dependencies()
    missing = missing_required_dependencies(statuses)
    assert not missing, format_missing_dependency_message(missing)


def test_runtime_dependency_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    rows = log_runtime_dependencies(logging.getLogger("test.runtime"))
    assert rows
    assert any("[RUNTIME_DEPENDENCY]" in rec.message for rec in caplog.records)
    assert any(row.ok for row in rows)

    fake_rows = check_runtime_dependencies(
        [
            RuntimeDependency("missing-pkg", "definitely_missing_pkg_xyz"),
        ]
    )
    missing = missing_required_dependencies(fake_rows)
    assert len(missing) == 1
    message = format_missing_dependency_message(missing)
    assert "missing-pkg" in message
    assert "pip install -r requirements.txt" in message


def test_rar_tool_unavailable_when_no_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rar = tmp_path / "mod.rar"
    _fake_rar(rar)
    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool", lambda: None
    )
    monkeypatch.setattr(
        "services.importers.archive.find_system_unrar_executable", lambda: None
    )
    monkeypatch.setattr("services.importers.archive.find_7z_executable", lambda: None)
    pytest.importorskip("rarfile")

    with pytest.raises(RarExtractError) as exc:
        extract_archive(rar, dest_dir=tmp_path / "out")
    assert exc.value.code == RAR_ERROR_TOOL_UNAVAILABLE
    assert exc.value.args[0] == RAR_TOOL_UNAVAILABLE_MSG


def test_rar_execution_failed_on_bad_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rarfile = pytest.importorskip("rarfile")
    rar = tmp_path / "bad.rar"
    _fake_rar(rar)
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"MZ")

    class _FakeRarFile:
        def __init__(self, *_a, **_k) -> None:
            pass

        def __enter__(self) -> _FakeRarFile:
            return self

        def __exit__(self, *_a) -> None:
            return None

        def extractall(self, **_k) -> None:
            raise rarfile.BadRarFile("Corrupt file data")

    monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)

    with pytest.raises(RarExtractError) as exc:
        _extract_rar_with_rarfile(rar, tmp_path / "out", unrar_tool=tool)
    assert exc.value.code == RAR_ERROR_EXECUTION_FAILED
    assert "Corrupt file data" in str(exc.value)
