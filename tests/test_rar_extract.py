"""RAR extract backend priority and error classification."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from services.importers.archive import (
    RAR_TOOL_UNAVAILABLE_MSG,
    TOOL_UNAVAILABLE_MSG,
    _extract_rar_with_rarfile,
    _format_rar_failure,
    extract_archive,
)


def _fake_rar(path: Path) -> None:
    path.write_bytes(b"Rar!\x1a\x07\x00not-a-real-rar")


def test_bundled_unrar_preferred_over_7z(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundled UnRAR wins even when system 7-Zip is installed."""
    rar = tmp_path / "mod.rar"
    _fake_rar(rar)
    bundled = Path(r"C:\fake\bin\tools\UnRAR.exe")
    calls: list[str] = []

    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool",
        lambda: bundled,
    )
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable",
        lambda: r"C:\Program Files\7-Zip\7z.exe",
    )

    def _rarfile(src: Path, dest: Path, *, unrar_tool: Path | str | None = None) -> None:
        calls.append(f"rarfile:{unrar_tool}")

    def _seven(*_a, **_k) -> None:
        calls.append("7z")
        raise AssertionError("7z must not run when bundled UnRAR exists")

    monkeypatch.setattr(
        "services.importers.archive._extract_rar_with_rarfile", _rarfile
    )
    monkeypatch.setattr("services.importers.archive._extract_with_7z", _seven)

    extract_archive(rar, dest_dir=tmp_path / "out")
    assert calls == [f"rarfile:{bundled}"]


def test_system_unrar_used_when_no_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rar = tmp_path / "mod.rar"
    _fake_rar(rar)
    system = r"C:\Windows\unrar.exe"
    seen: list[str | Path | None] = []

    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool", lambda: None
    )
    monkeypatch.setattr(
        "services.importers.archive.find_system_unrar_executable",
        lambda: system,
    )
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable",
        lambda: r"C:\Program Files\7-Zip\7z.exe",
    )

    def _rarfile(src: Path, dest: Path, *, unrar_tool: Path | str | None = None) -> None:
        seen.append(unrar_tool)

    monkeypatch.setattr(
        "services.importers.archive._extract_rar_with_rarfile", _rarfile
    )
    monkeypatch.setattr(
        "services.importers.archive._extract_with_7z",
        lambda *_a, **_k: pytest.fail("7z must not run when system unrar exists"),
    )

    extract_archive(rar, dest_dir=tmp_path / "out")
    assert seen == [system]


def test_7z_fallback_when_no_unrar_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without bundled/system unrar, fall back to 7-Zip."""
    rar = tmp_path / "mod.rar"
    _fake_rar(rar)
    calls: list[str] = []

    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool", lambda: None
    )
    monkeypatch.setattr(
        "services.importers.archive.find_system_unrar_executable", lambda: None
    )
    monkeypatch.setattr(
        "services.importers.archive.find_7z_executable",
        lambda: r"C:\Program Files\7-Zip\7z.exe",
    )

    def _seven(seven: str, src: Path, dest: Path) -> None:
        calls.append(seven)

    monkeypatch.setattr("services.importers.archive._extract_with_7z", _seven)

    extract_archive(rar, dest_dir=tmp_path / "out")
    assert calls == [r"C:\Program Files\7-Zip\7z.exe"]


def test_no_tools_reports_component_missing(
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

    with pytest.raises(RuntimeError) as exc:
        extract_archive(rar, dest_dir=tmp_path / "out")
    assert str(exc.value) == RAR_TOOL_UNAVAILABLE_MSG


def test_corrupt_rar_reports_real_error_not_tool_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rarfile = pytest.importorskip("rarfile")
    rar = tmp_path / "bad.rar"
    _fake_rar(rar)
    bundled = tmp_path / "UnRAR.exe"
    bundled.write_bytes(b"MZ")

    class _FakeRarFile:
        def __init__(self, *_a, **_k) -> None:
            pass

        def __enter__(self) -> _FakeRarFile:
            return self

        def __exit__(self, *_a) -> None:
            return None

        def extractall(self, **_k) -> None:
            raise rarfile.BadRarFile("Corrupt file data")

    monkeypatch.setattr(
        "services.importers.archive.resolve_bundled_unrar_tool",
        lambda: bundled,
    )
    monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)

    with pytest.raises(RuntimeError) as exc:
        extract_archive(rar, dest_dir=tmp_path / "out")
    msg = str(exc.value)
    assert msg != RAR_TOOL_UNAVAILABLE_MSG
    assert "Corrupt file data" in msg


def test_7z_rar_stderr_unrar_not_mapped_to_tool_missing() -> None:
    msg = _format_rar_failure("Can not find unrar")
    assert msg != RAR_TOOL_UNAVAILABLE_MSG
    assert "Can not find unrar" in msg

    msg2 = _format_rar_failure("unrar not found")
    assert msg2 != RAR_TOOL_UNAVAILABLE_MSG
    assert "unrar not found" in msg2


def test_rar_cannot_exec_maps_to_tool_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rarfile = pytest.importorskip("rarfile")
    rar = tmp_path / "x.rar"
    _fake_rar(rar)
    missing = tmp_path / "missing-unrar.exe"

    class _FakeRarFile:
        def __init__(self, *_a, **_k) -> None:
            raise rarfile.RarCannotExec("Cannot run unrar")

        def __enter__(self) -> _FakeRarFile:
            return self

        def __exit__(self, *_a) -> None:
            return None

    monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)

    with pytest.raises(RuntimeError) as exc:
        _extract_rar_with_rarfile(rar, tmp_path / "out", unrar_tool=missing)
    assert str(exc.value) == RAR_TOOL_UNAVAILABLE_MSG


def test_bad_rarfile_raises_bad_rar_message(
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

    with pytest.raises(RuntimeError) as exc:
        _extract_rar_with_rarfile(rar, tmp_path / "out", unrar_tool=tool)
    msg = str(exc.value)
    assert msg != RAR_TOOL_UNAVAILABLE_MSG
    assert "Corrupt file data" in msg
