"""Deploy-facing archive extraction primitive (ZIP / RAR / 7Z)."""

from __future__ import annotations

import logging
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ArchiveExtractStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class ExtractionResult:
    success: bool
    status: ArchiveExtractStatus
    output_root: str = ""
    extracted_files: int = 0
    extracted_bytes: int = 0
    error_code: str = ""
    error: str = ""
    elapsed_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, output_root: Path, *, elapsed_ms: float = 0.0) -> ExtractionResult:
        files = 0
        nbytes = 0
        try:
            from services.deploy_fs import safe_iter_files

            for path in safe_iter_files(output_root):
                files += 1
                try:
                    nbytes += int(path.stat().st_size)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        return cls(
            success=True,
            status=ArchiveExtractStatus.SUCCESS,
            output_root=str(output_root),
            extracted_files=files,
            extracted_bytes=nbytes,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def fail(
        cls,
        *,
        error: str,
        error_code: str = "ARCHIVE_EXTRACT_FAILED",
        status: ArchiveExtractStatus = ArchiveExtractStatus.FAILED,
        elapsed_ms: float = 0.0,
    ) -> ExtractionResult:
        return cls(
            success=False,
            status=status,
            error=error,
            error_code=error_code,
            elapsed_ms=elapsed_ms,
        )


class ArchiveExtractor:
    """Bounded archive extraction for deploy staging (no importer side effects)."""

    @staticmethod
    def can_handle(path: str | Path) -> bool:
        from services.importers.archive import is_archive_path

        return bool(is_archive_path(path))

    @staticmethod
    def extract(
        path: str | Path,
        destination: str | Path,
        *,
        timeout: float = 600.0,
    ) -> ExtractionResult:
        src = Path(path).expanduser()
        dest = Path(destination).expanduser()
        if not src.is_file():
            return ExtractionResult.fail(
                error=f"压缩包不存在：{src}",
                error_code="ARCHIVE_NOT_FOUND",
            )
        if not ArchiveExtractor.can_handle(src):
            return ExtractionResult.fail(
                error=f"不支持的压缩格式：{src.suffix}",
                error_code="ARCHIVE_UNSUPPORTED",
                status=ArchiveExtractStatus.UNSUPPORTED,
            )

        dest.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        try:
            from services.importers.archive import RarExtractError, extract_archive

            out = extract_archive(src, dest_dir=dest)
        except RarExtractError as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            code_map = {
                "RAR_TIMEOUT": "ARCHIVE_TIMEOUT",
                "RAR_EXECUTABLE_INVALID": "ARCHIVE_EXECUTABLE_INVALID",
                "RAR_TOOL_UNAVAILABLE": "EXTRACTOR_NOT_AVAILABLE",
                "RAR_PYTHON_SUPPORT_MISSING": "EXTRACTOR_NOT_AVAILABLE",
                "RAR_EXECUTION_FAILED": "ARCHIVE_CORRUPT",
            }
            error_code = code_map.get(str(exc.code or ""), "ARCHIVE_EXTRACT_FAILED")
            status = ArchiveExtractStatus.FAILED
            if error_code == "ARCHIVE_TIMEOUT":
                status = ArchiveExtractStatus.TIMEOUT
            return ExtractionResult.fail(
                error=str(exc),
                error_code=error_code,
                status=status,
                elapsed_ms=elapsed,
            )
        except TimeoutError as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            logger.warning(
                "[DEPLOY_TIMEOUT] archive=%s elapsed_ms=%.1f",
                src,
                elapsed,
            )
            return ExtractionResult.fail(
                error=str(exc),
                error_code="ARCHIVE_TIMEOUT",
                status=ArchiveExtractStatus.TIMEOUT,
                elapsed_ms=elapsed,
            )
        except FileNotFoundError as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            return ExtractionResult.fail(
                error=str(exc),
                error_code="ARCHIVE_NOT_FOUND",
                elapsed_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - t0) * 1000.0
            from services.deploy_archive_errors import archive_error_code

            code = archive_error_code(str(exc))
            status = ArchiveExtractStatus.FAILED
            if code == "ARCHIVE_TIMEOUT":
                status = ArchiveExtractStatus.TIMEOUT
            elif code == "ARCHIVE_UNSUPPORTED":
                status = ArchiveExtractStatus.UNSUPPORTED
            return ExtractionResult.fail(
                error=str(exc),
                error_code=code,
                status=status,
                elapsed_ms=elapsed,
            )

        elapsed = (time.perf_counter() - t0) * 1000.0
        return ExtractionResult.ok(Path(out), elapsed_ms=elapsed)

    @staticmethod
    def extract_members(
        path: str | Path,
        members: list[tuple[str, Path]],
        *,
        timeout: float = 600.0,
    ) -> ExtractionResult:
        """
        Stream planned archive members to explicit destination files.

        ``members`` is ``(archive_member, destination_file)``. Only those
        members are read. Destinations are caller-supplied file paths
        (FilePlan ``target_absolute``) — this never builds an ``apply_*``
        extract tree and never writes unplanned members.
        """
        del timeout
        src = Path(path).expanduser()
        if not src.is_file():
            return ExtractionResult.fail(
                error=f"压缩包不存在：{src}",
                error_code="ARCHIVE_NOT_FOUND",
            )
        if not ArchiveExtractor.can_handle(src):
            return ExtractionResult.fail(
                error=f"不支持的压缩格式：{src.suffix}",
                error_code="ARCHIVE_UNSUPPORTED",
                status=ArchiveExtractStatus.UNSUPPORTED,
            )
        if not members:
            return ExtractionResult(
                success=True,
                status=ArchiveExtractStatus.SUCCESS,
                extracted_files=0,
                extracted_bytes=0,
            )

        t0 = time.perf_counter()
        try:
            copied, nbytes = _extract_planned_members(src, members)
        except FileNotFoundError as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            code = "ARCHIVE_MEMBER_MISSING"
            if "不存在" in str(exc) and "成员" not in str(exc):
                code = "ARCHIVE_NOT_FOUND"
            return ExtractionResult.fail(
                error=str(exc),
                error_code=code,
                elapsed_ms=elapsed,
            )
        except RuntimeError as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            from services.deploy_archive_errors import archive_error_code

            return ExtractionResult.fail(
                error=str(exc),
                error_code=archive_error_code(str(exc)),
                elapsed_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - t0) * 1000.0
            from services.deploy_archive_errors import archive_error_code

            return ExtractionResult.fail(
                error=str(exc),
                error_code=archive_error_code(str(exc)),
                elapsed_ms=elapsed,
            )

        elapsed = (time.perf_counter() - t0) * 1000.0
        return ExtractionResult(
            success=True,
            status=ArchiveExtractStatus.SUCCESS,
            extracted_files=copied,
            extracted_bytes=nbytes,
            elapsed_ms=elapsed,
        )


def _normalize_member_name(member: str) -> str:
    return str(member or "").replace("\\", "/").lstrip("/")


def _member_has_traversal(member: str) -> bool:
    return any(part == ".." for part in _normalize_member_name(member).split("/") if part)


def _extract_planned_members(
    src: Path,
    members: list[tuple[str, Path]],
) -> tuple[int, int]:
    suffix = src.suffix.lower()
    if suffix == ".zip":
        return _extract_zip_members(src, members)
    if suffix == ".7z":
        return _extract_7z_members(src, members)
    if suffix == ".rar":
        return _extract_rar_members(src, members)
    raise RuntimeError(f"不支持的压缩格式：{suffix}")


def _prepare_member_dest(member: str, dest: Path, *, archive: Path) -> Path:
    name = _normalize_member_name(member)
    if not name or name.endswith("/"):
        raise RuntimeError(f"缺少 archive member：{dest}")
    if _member_has_traversal(name):
        raise RuntimeError(f"不安全的压缩包路径：{member}")
    out = Path(dest)
    if not str(out):
        raise RuntimeError(f"缺少 archive member：{name} (archive={archive})")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _extract_zip_members(
    src: Path,
    members: list[tuple[str, Path]],
) -> tuple[int, int]:
    copied = 0
    nbytes = 0
    with zipfile.ZipFile(src, "r") as zf:
        index: dict[str, zipfile.ZipInfo] = {}
        for info in zf.infolist():
            key = _normalize_member_name(info.filename)
            if not key or key.endswith("/") or info.is_dir():
                continue
            index[key] = info
        for member, dest in members:
            key = _normalize_member_name(member)
            if not key or key.endswith("/"):
                raise RuntimeError(f"缺少 archive member：{dest}")
            if _member_has_traversal(key):
                raise RuntimeError(f"不安全的压缩包路径：{member}")
            info = index.get(key)
            if info is None:
                raise FileNotFoundError(f"压缩包缺少成员：{key} (archive={src})")
            out = _prepare_member_dest(member, dest, archive=src)
            with zf.open(info, "r") as inf, out.open("wb") as outf:
                shutil.copyfileobj(inf, outf)
            copied += 1
            try:
                nbytes += int(out.stat().st_size)
            except OSError:
                pass
    return copied, nbytes


def _extract_7z_members(
    src: Path,
    members: list[tuple[str, Path]],
) -> tuple[int, int]:
    try:
        import py7zr
    except ImportError:
        return _extract_7z_members_cli(src, members)

    copied = 0
    nbytes = 0
    # Re-open per member: py7zr ``read()`` is single-pass on one handle.
    for member, dest in members:
        out = _prepare_member_dest(member, dest, archive=src)
        key = _normalize_member_name(member)
        with py7zr.SevenZipFile(src, mode="r") as archive:
            names = {
                _normalize_member_name(name): name
                for name in archive.getnames()
                if _normalize_member_name(name)
                and not _normalize_member_name(name).endswith("/")
            }
            raw = names.get(key)
            if raw is None:
                raise FileNotFoundError(f"压缩包缺少成员：{key} (archive={src})")
            blobs = archive.read([raw])
            payload = blobs.get(raw)
            if payload is None:
                raise FileNotFoundError(f"压缩包缺少成员：{key} (archive={src})")
            with out.open("wb") as outf:
                shutil.copyfileobj(payload, outf)
        copied += 1
        try:
            nbytes += int(out.stat().st_size)
        except OSError:
            pass
    return copied, nbytes


def _extract_7z_members_cli(
    src: Path,
    members: list[tuple[str, Path]],
) -> tuple[int, int]:
    import subprocess

    from services.importers.archive import find_7z_executable

    seven = find_7z_executable()
    if not seven:
        raise RuntimeError("缺少 py7zr，无法提取 7z 成员")
    copied = 0
    nbytes = 0
    for member, dest in members:
        out = _prepare_member_dest(member, dest, archive=src)
        key = _normalize_member_name(member)
        cmd = [seven, "e", "-so", "-y", str(src), key]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"解压失败：{exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            if "cannot find" in err.lower() or "没有" in err:
                raise FileNotFoundError(f"压缩包缺少成员：{key} (archive={src})")
            raise RuntimeError(f"解压失败：{err or f'exit {proc.returncode}'}")
        out.write_bytes(proc.stdout)
        copied += 1
        nbytes += len(proc.stdout)
    return copied, nbytes


def _extract_rar_members(
    src: Path,
    members: list[tuple[str, Path]],
) -> tuple[int, int]:
    try:
        import rarfile
    except ImportError as exc:
        raise RuntimeError("缺少 rarfile，无法提取 RAR 成员") from exc

    copied = 0
    nbytes = 0
    with rarfile.RarFile(src) as rf:
        index: dict[str, object] = {}
        for info in rf.infolist():
            name = _normalize_member_name(str(getattr(info, "filename", "") or ""))
            is_dir = getattr(info, "isdir", lambda: False)()
            if not name or name.endswith("/") or is_dir:
                continue
            index[name] = info
        for member, dest in members:
            out = _prepare_member_dest(member, dest, archive=src)
            key = _normalize_member_name(member)
            info = index.get(key)
            if info is None:
                raise FileNotFoundError(f"压缩包缺少成员：{key} (archive={src})")
            with rf.open(info) as inf, out.open("wb") as outf:
                shutil.copyfileobj(inf, outf)
            copied += 1
            try:
                nbytes += int(out.stat().st_size)
            except OSError:
                pass
    return copied, nbytes
