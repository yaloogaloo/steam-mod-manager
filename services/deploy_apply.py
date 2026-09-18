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
import os
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

# Hash during the copy read so the later manifest hash stage does not re-read
# the same payload. Threshold 0: every copied file is hashed once at apply.
_HASH_WHILE_COPY_MIN_BYTES = 0
_APPLY_SOURCE_HASHES: ContextVar[dict[str, str] | None] = ContextVar(
    "apply_source_hashes", default=None
)
_APPLY_SOURCE_SIZES: ContextVar[dict[str, int] | None] = ContextVar(
    "apply_source_sizes", default=None
)


def current_apply_source_hashes() -> dict[str, str]:
    """Sha256 of sources hashed during the latest ``apply_file_plan`` copy."""
    return dict(_APPLY_SOURCE_HASHES.get() or {})


def current_apply_source_sizes() -> dict[str, int]:
    """Byte sizes recorded during the latest ``apply_file_plan`` copy."""
    return dict(_APPLY_SOURCE_SIZES.get() or {})


def _source_hash_key(path: Path) -> str:
    from services.deploy_op_profile import cached_resolve

    try:
        return cached_resolve(path)
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


def _copy_one(entry: DeployFilePlanEntry) -> tuple[str | None, int]:
    """Copy *entry* to its target.

    Returns ``(sha256 or None, source size)``. Size comes from the source
    stat already required to copy — callers must not re-stat the dest.
    """
    from services.deploy_op_profile import (
        CopyFileTrace,
        ensure_dir,
        note_copy_file,
        record_op,
        volume_id,
    )

    src = Path(entry.source)
    dst = Path(entry.target_absolute)
    rel = str(entry.source_relative or entry.target_relative or dst.name).replace("\\", "/")
    mkdir_ms = ensure_dir(dst.parent)
    size = 0
    stat_ms = 0.0
    try:
        t_stat = time.perf_counter()
        size = int(src.stat().st_size)
        stat_ms = (time.perf_counter() - t_stat) * 1000.0
        record_op("stat", stat_ms, path=str(src))
    except OSError:
        size = 0
    src_vol = volume_id(src)
    dst_vol = volume_id(dst)
    if size >= _HASH_WHILE_COPY_MIN_BYTES:
        digest = hashlib.sha256()
        hash_ms = 0.0
        t_loop = time.perf_counter()
        with src.open("rb") as inf, dst.open("wb") as outf:
            while True:
                block = inf.read(1024 * 1024)
                if not block:
                    break
                t_h = time.perf_counter()
                digest.update(block)
                hash_ms += (time.perf_counter() - t_h) * 1000.0
                outf.write(block)
        loop_ms = (time.perf_counter() - t_loop) * 1000.0
        t_meta = time.perf_counter()
        shutil.copystat(src, dst, follow_symlinks=True)
        copystat_ms = (time.perf_counter() - t_meta) * 1000.0
        elapsed = loop_ms + copystat_ms
        record_op("copyfile", loop_ms, bytes_count=size, path=str(src))
        record_op("hash", hash_ms, bytes_count=size, path=str(src))
        record_op("copystat", copystat_ms, path=str(dst))
        note_copy_file(
            CopyFileTrace(
                relative=rel,
                size=size,
                elapsed_ms=elapsed + mkdir_ms + stat_ms,
                read_write_ms=max(0.0, loop_ms - hash_ms),
                hash_ms=hash_ms,
                copystat_ms=copystat_ms,
                mkdir_ms=mkdir_ms,
                stat_ms=stat_ms,
                source_volume=src_vol,
                target_volume=dst_vol,
                source=str(src),
                target=str(dst),
                copyfile=True,
                hashed=True,
                copystat=True,
                chmod=os.name != "nt",
            )
        )
        return digest.hexdigest(), size
    t_copy = time.perf_counter()
    shutil.copy2(src, dst)
    copy2_ms = (time.perf_counter() - t_copy) * 1000.0
    record_op("copy2", copy2_ms, bytes_count=size, path=str(src))
    note_copy_file(
        CopyFileTrace(
            relative=rel,
            size=size,
            elapsed_ms=copy2_ms + mkdir_ms + stat_ms,
            read_write_ms=copy2_ms,
            mkdir_ms=mkdir_ms,
            stat_ms=stat_ms,
            source_volume=src_vol,
            target_volume=dst_vol,
            source=str(src),
            target=str(dst),
            copy2=True,
            copystat=True,
            chmod=os.name != "nt",
        )
    )
    return None, size


def extract_archive_via_core(archive: Path, dest: Path):
    """
    Sole Deploy-path entry to full-archive ``ArchiveExtractor.extract``.

    Used by ModDeployer source staging (``prepare_deploy_content``) and
    WH3 activation. Strategies must not call ArchiveExtractor.
    FilePlan ``OP_EXTRACT_MEMBER`` uses :func:`extract_members_via_core`.
    """
    from services.archive_extractor import ArchiveExtractStatus
    from services.deploy_op_profile import timed_op

    with timed_op("archive_extraction", path=str(archive)):
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

    t0 = time.perf_counter()
    extracted = extract_members_via_core(archive, planned)
    from services.deploy_op_profile import record_op

    record_op(
        "archive_extraction",
        (time.perf_counter() - t0) * 1000.0,
        path=str(archive),
        bytes_count=int(getattr(extracted, "extracted_bytes", 0) or 0),
    )
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
    source_sizes: dict[str, int] = {}
    _APPLY_SOURCE_HASHES.set(source_hashes)
    _APPLY_SOURCE_SIZES.set(source_sizes)

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
            debug_files = logger.isEnabledFor(logging.DEBUG)
            for entry in copy_entries:
                src = Path(entry.source)
                dst = Path(entry.target_absolute)
                if debug_files:
                    log_deploy_file_start(source=src, target=dst, mode="copy")
                try:
                    digest, nbytes = _copy_one(entry)
                    key = str(src)
                    source_sizes[key] = int(nbytes)
                    if digest:
                        source_hashes[key] = digest
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
                copy_bytes += nbytes
                total_bytes += nbytes
                if debug_files:
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
