"""ArchiveExtractor primitive tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from services.archive_extractor import ArchiveExtractStatus, ArchiveExtractor
from services.deploy_archive_errors import archive_error_code


def test_can_handle_zip(tmp_path: Path) -> None:
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", "hi")
    assert ArchiveExtractor.can_handle(z) is True
    assert ArchiveExtractor.can_handle(tmp_path / "x.bin") is False


def test_extract_zip_success(tmp_path: Path) -> None:
    z = tmp_path / "m.zip"
    dest = tmp_path / "out"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("nested/a.txt", "data")
    result = ArchiveExtractor.extract(z, dest)
    assert result.success is True
    assert result.status == ArchiveExtractStatus.SUCCESS
    assert (dest / "nested" / "a.txt").is_file()
    assert result.extracted_files >= 1


def test_extract_missing_archive(tmp_path: Path) -> None:
    result = ArchiveExtractor.extract(tmp_path / "nope.zip", tmp_path / "out")
    assert result.success is False
    assert result.error_code == "ARCHIVE_NOT_FOUND"


def test_extract_unsupported(tmp_path: Path) -> None:
    bad = tmp_path / "x.bin"
    bad.write_bytes(b"x")
    result = ArchiveExtractor.extract(bad, tmp_path / "out")
    assert result.success is False
    assert result.error_code == "ARCHIVE_UNSUPPORTED"


def test_zip_slip_blocked(tmp_path: Path) -> None:
    z = tmp_path / "evil.zip"
    dest = tmp_path / "stage"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../outside.txt", "nope")
    result = ArchiveExtractor.extract(z, dest)
    assert result.success is False
    assert result.error_code == "ARCHIVE_SECURITY_VIOLATION"
    assert not (tmp_path / "outside.txt").exists()


def test_archive_error_code_timeout() -> None:
    assert archive_error_code("RAR 部署失败: 解压超时（>600s）") == "ARCHIVE_TIMEOUT"
    assert archive_error_code("不安全的压缩包路径") == "ARCHIVE_SECURITY_VIOLATION"
    assert archive_error_code("压缩包缺少成员：a.txt (archive=x.zip)") == "ARCHIVE_MEMBER_MISSING"


def test_extract_members_writes_only_planned_targets(tmp_path: Path) -> None:
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("keep/a.txt", "A")
        zf.writestr("skip/b.txt", "B")
    dest = tmp_path / "out" / "nested" / "a.txt"
    result = ArchiveExtractor.extract_members(z, [("keep/a.txt", dest)])
    assert result.success is True
    assert result.extracted_files == 1
    assert dest.read_text(encoding="utf-8") == "A"
    assert not (tmp_path / "out" / "skip").exists()
    assert not (tmp_path / "skip").exists()


def test_extract_members_missing_member_fails(tmp_path: Path) -> None:
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("keep/a.txt", "A")
    dest = tmp_path / "out" / "missing.txt"
    result = ArchiveExtractor.extract_members(z, [("nope/missing.txt", dest)])
    assert result.success is False
    assert result.error_code == "ARCHIVE_MEMBER_MISSING"
    assert not dest.exists()


def test_extract_7z_members_bcj2_falls_back_to_7z_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "m.7z"
    archive.write_bytes(b"7z fake")
    dest = tmp_path / "out" / "a.txt"

    class _Boom:
        def __init__(self, *_a, **_k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def getnames(self):
            return ["a.txt"]

        def read(self, _names):
            raise RuntimeError("BCJ2 filter is not supported by py7zr")

    def fake_cli(_src, members):
        for _member, out in members:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("cli-ok", encoding="utf-8")
        return len(members), 6

    monkeypatch.setattr("py7zr.SevenZipFile", _Boom)
    monkeypatch.setattr(
        "services.archive_extractor._extract_7z_members_cli", fake_cli
    )
    result = ArchiveExtractor.extract_members(archive, [("a.txt", dest)])
    assert result.success is True
    assert dest.read_text(encoding="utf-8") == "cli-ok"


def test_extract_members_rejects_traversal_member(tmp_path: Path) -> None:
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok.txt", "ok")
    dest = tmp_path / "safe.txt"
    result = ArchiveExtractor.extract_members(z, [("../outside.txt", dest)])
    assert result.success is False
    assert result.error_code == "ARCHIVE_SECURITY_VIOLATION"
    assert not dest.exists()
    assert not (tmp_path / "outside.txt").exists()
