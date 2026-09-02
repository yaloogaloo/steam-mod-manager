"""Deploy-facing archive extraction primitive (ZIP / RAR / 7Z)."""

from __future__ import annotations

import logging
import time
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
