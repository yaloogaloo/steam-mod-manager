"""Deploy Core Apply — filesystem mutation for the Core Deploy pipeline.

DEPLOYMENT EXECUTION BOUNDARY (permanent)

This module owns actual filesystem mutation for Core Deploy:

- Folder: DeployFilePlan → copy to target
- Archive: DeployFilePlan → ArchiveExtractor → planned members → target

``ArchiveExtractor.extract`` for Deploy must go through ``extract_archive_via_core``.
Strategies MUST NOT mutate deploy targets or call ArchiveExtractor.

Apply executes the authoritative DeployFilePlan.
It does not discover, rescan, or invent a new file list (no after−before).
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from services.archive_extractor import ArchiveExtractor
from services.deploy_file_plan import (
    OP_COPY,
    OP_EXTRACT_MEMBER,
    DeployFilePlan,
    DeployFilePlanEntry,
)

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    success: bool
    applied: int = 0
    failed: int = 0
    error: str = ""
    failed_details: list[str] = field(default_factory=list)
    # Diagnostics (large-mod copy stalls)
    source_file_count: int = 0
    total_bytes: int = 0
    copied_files: int = 0
    group_timings_ms: list[dict[str, object]] = field(default_factory=list)


def _copy_one(entry: DeployFilePlanEntry) -> None:
    src = Path(entry.source)
    dst = Path(entry.target_absolute)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def extract_archive_via_core(archive: Path, dest: Path):
    """
    Sole Deploy-path entry to ``ArchiveExtractor.extract``.

    Used by FilePlan Apply and by ModDeployer source staging
    (``prepare_deploy_content``). Strategies must not call ArchiveExtractor.
    """
    from services.archive_extractor import ArchiveExtractStatus

    result = ArchiveExtractor.extract(archive, dest)
    return result, ArchiveExtractStatus


def _apply_extract_group(
    archive: Path,
    entries: list[DeployFilePlanEntry],
    *,
    staging_parent: Path,
) -> tuple[int, int]:
    """Extract archive once via ArchiveExtractor, then copy planned members.

    Returns ``(copied_count, bytes_copied)``. Does not re-list or re-extract
    the archive between members.
    """
    from services.importers.archive import cleanup_import_cache

    stage = staging_parent / f"apply_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    copied = 0
    nbytes_total = 0
    try:
        extracted, _status = extract_archive_via_core(archive, stage)
        if not extracted.success:
            raise RuntimeError(extracted.error or f"压缩包解压失败：{archive}")
        root = Path(extracted.output_root)
        for entry in entries:
            member = str(entry.source_relative or "").replace("\\", "/").lstrip("/")
            if not member:
                raise RuntimeError(f"缺少 archive member：{entry.target_absolute}")
            src = root / member
            if not src.is_file():
                # Some extractors may flatten differently; try posix path as-is.
                alt = root.joinpath(*Path(member).parts)
                if alt.is_file():
                    src = alt
                else:
                    raise FileNotFoundError(
                        f"解压后缺少成员：{member} (archive={archive})"
                    )
            dst = Path(entry.target_absolute)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            try:
                nbytes_total += int(dst.stat().st_size)
            except OSError:
                pass
    finally:
        cleanup_import_cache(stage)
    return copied, nbytes_total


def apply_file_plan(
    plan: DeployFilePlan,
    *,
    staging_parent: Path | None = None,
) -> ApplyResult:
    """
    Execute FilePlan filesystem mutation (copy / extract_member).

    Overwriting an existing target is success. Does not rescan targets to
    invent a new file list — accounting is DeployFilePlan itself.
    """
    if not plan.files:
        plan.diagnostics.stage = "apply"
        plan.diagnostics.failed_files = 0
        return ApplyResult(
            success=False,
            error="FilePlan 为空：没有可部署的文件",
            failed_details=["planned_files=0"],
        )

    staging_root = staging_parent
    if staging_root is None:
        from services.importers.archive import import_cache_root

        staging_root = import_cache_root()

    extract_groups: dict[str, list[DeployFilePlanEntry]] = defaultdict(list)
    copy_entries: list[DeployFilePlanEntry] = []

    for entry in plan.files:
        if not entry.required:
            continue
        if entry.op == OP_EXTRACT_MEMBER:
            extract_groups[entry.source].append(entry)
        elif entry.op == OP_COPY:
            copy_entries.append(entry)
        else:
            detail = f"unsupported op={entry.op} target={entry.target_absolute}"
            plan.diagnostics.stage = "apply"
            plan.diagnostics.failed_files = 1
            plan.diagnostics.failed_details = [detail]
            return ApplyResult(
                success=False,
                error=f"不支持的 Apply 操作：{entry.op}",
                failed_details=[detail],
            )

    source_file_count = sum(len(g) for g in extract_groups.values()) + len(
        copy_entries
    )
    applied = 0
    total_bytes = 0
    failed_details: list[str] = []
    group_timings: list[dict[str, object]] = []

    logger.info(
        "[DEPLOY_APPLY] start planned=%s extract_groups=%s copy_entries=%s",
        source_file_count,
        len(extract_groups),
        len(copy_entries),
    )

    try:
        for archive_s, group in extract_groups.items():
            archive = Path(archive_s)
            archive_bytes = 0
            try:
                if archive.is_file():
                    archive_bytes = int(archive.stat().st_size)
            except OSError:
                archive_bytes = 0
            t0 = time.perf_counter()
            logger.info(
                "[DEPLOY_APPLY] extract archive=%s members=%s archive_bytes=%s",
                archive,
                len(group),
                archive_bytes,
            )
            copied, nbytes = _apply_extract_group(
                archive, group, staging_parent=staging_root
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            applied += copied
            total_bytes += nbytes
            timing = {
                "group": "extract",
                "archive": str(archive),
                "members": len(group),
                "copied": copied,
                "bytes": nbytes,
                "archive_bytes": archive_bytes,
                "elapsed_ms": elapsed_ms,
            }
            group_timings.append(timing)
            logger.info(
                "[DEPLOY_APPLY] extract_done archive=%s members=%s copied=%s "
                "bytes=%s elapsed_ms=%s",
                archive.name,
                len(group),
                copied,
                nbytes,
                elapsed_ms,
            )

        from services.deploy_file_ops_log import (
            log_deploy_file_failed,
            log_deploy_file_start,
            log_deploy_file_success,
        )

        if copy_entries:
            t0 = time.perf_counter()
            copy_bytes = 0
            for entry in copy_entries:
                src = Path(entry.source)
                dst = Path(entry.target_absolute)
                log_deploy_file_start(source=src, target=dst, mode="copy")
                try:
                    _copy_one(entry)
                except OSError as exc:
                    log_deploy_file_failed(
                        source=src,
                        target=dst,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    detail = f"{dst}: {exc}"
                    failed_details.append(detail)
                    plan.diagnostics.stage = "apply"
                    plan.diagnostics.applied_files = applied
                    plan.diagnostics.failed_files = len(failed_details)
                    plan.diagnostics.failed_details = list(failed_details)
                    err = str(exc).lower()
                    if "permission" in err or "denied" in err:
                        msg = f"Permission denied：{exc}"
                    else:
                        msg = f"复制失败：{exc}"
                    return ApplyResult(
                        success=False,
                        applied=applied,
                        failed=len(failed_details),
                        error=msg,
                        failed_details=failed_details,
                        source_file_count=source_file_count,
                        total_bytes=total_bytes,
                        copied_files=applied,
                        group_timings_ms=group_timings,
                    )
                try:
                    nbytes = int(dst.stat().st_size)
                except OSError:
                    nbytes = 0
                copy_bytes += nbytes
                total_bytes += nbytes
                log_deploy_file_success(source=src, target=dst, bytes_written=nbytes)
                applied += 1
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            group_timings.append(
                {
                    "group": "copy",
                    "members": len(copy_entries),
                    "copied": len(copy_entries),
                    "bytes": copy_bytes,
                    "elapsed_ms": elapsed_ms,
                }
            )
            logger.info(
                "[DEPLOY_APPLY] copy_done members=%s bytes=%s elapsed_ms=%s",
                len(copy_entries),
                copy_bytes,
                elapsed_ms,
            )
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        detail = str(exc)
        failed_details.append(detail)
        plan.diagnostics.stage = "apply"
        plan.diagnostics.applied_files = applied
        plan.diagnostics.failed_files = len(failed_details)
        plan.diagnostics.failed_details = list(failed_details)
        return ApplyResult(
            success=False,
            applied=applied,
            failed=len(failed_details),
            error=detail,
            failed_details=failed_details,
            source_file_count=source_file_count,
            total_bytes=total_bytes,
            copied_files=applied,
            group_timings_ms=group_timings,
        )

    plan.diagnostics.stage = "apply"
    plan.diagnostics.applied_files = applied
    plan.diagnostics.failed_files = 0
    plan.diagnostics.failed_details = []
    logger.info(
        "[DEPLOY_APPLY] done planned=%s copied=%s bytes=%s groups=%s",
        source_file_count,
        applied,
        total_bytes,
        len(group_timings),
    )
    return ApplyResult(
        success=True,
        applied=applied,
        source_file_count=source_file_count,
        total_bytes=total_bytes,
        copied_files=applied,
        group_timings_ms=group_timings,
    )
