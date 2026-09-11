"""Deploy Core Apply — filesystem mutation for the Core Deploy pipeline.

DEPLOYMENT EXECUTION BOUNDARY (permanent)

This module owns actual filesystem mutation for Core Deploy:

- Folder: DeployFilePlan → copy to target
- Archive: DeployFilePlan → planned members → target

``OP_EXTRACT_MEMBER`` streams ``archive + source_relative`` directly to
``target_absolute``. It must not extract the whole archive into
``import_cache/apply_*`` and copy from that tree.

Full-archive ``ArchiveExtractor.extract`` for Deploy still goes through
``extract_archive_via_core`` (``prepare_deploy_content`` / WH3 activation).
Member extract goes through ``extract_members_via_core``.
Strategies MUST NOT mutate deploy targets or call ArchiveExtractor.

Apply executes the authoritative DeployFilePlan.
It does not discover, rescan, or invent a new file list (no after−before).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from collections import defaultdict
from contextvars import ContextVar
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

# Files at/above this size are hashed in the same read as copy so the later
# manifest hash stage does not re-read the same payload (800MB .pack etc.).
_HASH_WHILE_COPY_MIN_BYTES = 16 * 1024 * 1024
_APPLY_SOURCE_HASHES: ContextVar[dict[str, str] | None] = ContextVar(
    "apply_source_hashes", default=None
)


def current_apply_source_hashes() -> dict[str, str]:
    """Sha256 of sources hashed during the latest ``apply_file_plan`` copy."""
    return dict(_APPLY_SOURCE_HASHES.get() or {})


def _source_hash_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


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


def _copy_one(entry: DeployFilePlanEntry) -> str | None:
    """Copy *entry* to its target.

    Returns a sha256 hex digest when the source was hashed during copy;
    otherwise ``None`` (small files keep ``shutil.copy2``).
    """
    src = Path(entry.source)
    dst = Path(entry.target_absolute)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = int(src.stat().st_size)
    except OSError:
        size = 0
    if size >= _HASH_WHILE_COPY_MIN_BYTES:
        digest = hashlib.sha256()
        with src.open("rb") as inf, dst.open("wb") as outf:
            while True:
                block = inf.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                outf.write(block)
        shutil.copystat(src, dst, follow_symlinks=True)
        return digest.hexdigest()
    shutil.copy2(src, dst)
    return None


def extract_archive_via_core(archive: Path, dest: Path):
    """
    Sole Deploy-path entry to full-archive ``ArchiveExtractor.extract``.

    Used by ModDeployer source staging (``prepare_deploy_content``) and
    WH3 activation. Strategies must not call ArchiveExtractor.
    FilePlan ``OP_EXTRACT_MEMBER`` uses :func:`extract_members_via_core`.
    """
    from services.archive_extractor import ArchiveExtractStatus

    result = ArchiveExtractor.extract(archive, dest)
    return result, ArchiveExtractStatus


def extract_members_via_core(
    archive: Path,
    members: list[tuple[str, Path]],
):
    """Sole Deploy-path entry to planned-member ``ArchiveExtractor.extract_members``."""
    return ArchiveExtractor.extract_members(archive, members)


def _apply_extract_group(
    archive: Path,
    entries: list[DeployFilePlanEntry],
) -> tuple[int, int]:
    """Stream planned archive members directly to FilePlan targets.

    Returns ``(copied_count, bytes_copied)``. Does not extract unplanned
    members and does not create an ``apply_*`` staging tree.
    """
    planned: list[tuple[str, Path]] = []
    for entry in entries:
        member = str(entry.source_relative or "").replace("\\", "/").lstrip("/")
        if not member:
            raise RuntimeError(f"缺少 archive member：{entry.target_absolute}")
        dest = Path(entry.target_absolute)
        if not str(entry.target_absolute or "").strip():
            raise RuntimeError(f"缺少 archive member：{member}")
        planned.append((member, dest))

    extracted = extract_members_via_core(archive, planned)
    if not extracted.success:
        err = extracted.error or f"压缩包解压失败：{archive}"
        if extracted.error_code in {"ARCHIVE_NOT_FOUND", "ARCHIVE_MEMBER_MISSING"}:
            raise FileNotFoundError(err)
        raise RuntimeError(err)
    return int(extracted.extracted_files), int(extracted.extracted_bytes)


def apply_file_plan(
    plan: DeployFilePlan,
    *,
    staging_parent: Path | None = None,
) -> ApplyResult:
    """
    Execute FilePlan filesystem mutation (copy / extract_member).

    Overwriting an existing target is success. Does not rescan targets to
    invent a new file list — accounting is DeployFilePlan itself.

    ``staging_parent`` is accepted for call-site compatibility and ignored.
    ``OP_EXTRACT_MEMBER`` writes directly to ``target_absolute``.
    """
    del staging_parent
    if not plan.files:
        plan.diagnostics.stage = "apply"
        plan.diagnostics.failed_files = 0
        return ApplyResult(
            success=False,
            error="FilePlan 为空：没有可部署的文件",
            failed_details=["planned_files=0"],
        )

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
    source_hashes: dict[str, str] = {}
    _APPLY_SOURCE_HASHES.set(source_hashes)

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
            copied, nbytes = _apply_extract_group(archive, group)
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
                    digest = _copy_one(entry)
                    if digest:
                        source_hashes[_source_hash_key(src)] = digest
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
                "[DEPLOY_APPLY] copy_done members=%s bytes=%s elapsed_ms=%s hashed_during_copy=%s",
                len(copy_entries),
                copy_bytes,
                elapsed_ms,
                len(source_hashes),
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
