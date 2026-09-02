"""ArchiveExtractor primitive tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

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
