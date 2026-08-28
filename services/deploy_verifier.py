"""Deploy-time source / result verification (runtime only; not manifest format)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from services.deploy_errors import DeploySourceError, DeployValidationError

logger = logging.getLogger(__name__)

_SKIP_DIR_NAMES = frozenset({".info", "info", "历史版本"})


def _sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _is_archive_name(name: str) -> bool:
    from services.importers.archive import is_archive_path

    return bool(is_archive_path(name))


def iter_loose_deploy_files(root: Path, *, skip_archives: bool = True) -> list[Path]:
    """Non-metadata files under *root* (optionally excluding archives)."""
    base = Path(root)
    if not base.is_dir():
        return []
    files: list[Path] = []
    try:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                parts = path.relative_to(base).parts
            except ValueError:
                continue
            if any(part in _SKIP_DIR_NAMES or part.startswith(".") for part in parts[:-1]):
                continue
            if parts and parts[0] in _SKIP_DIR_NAMES:
                continue
            if skip_archives and _is_archive_name(path.name):
                continue
            files.append(path)
    except OSError:
        return []
    return files


def has_legal_deploy_content(root: Path) -> bool:
    """
    True when *root* contains deployable Mod payload.

    Legal: at least one non-archive file outside ``.info`` / ``info``.
    Illegal: missing dir, empty, metadata-only, or archives-only (would copy
    ``.zip`` as a normal file).
    """
    return bool(iter_loose_deploy_files(root, skip_archives=True))


def plain_content_allow_list(root: Path) -> frozenset[str]:
    """Relative posix paths of loose (non-archive) files under *root*."""
    base = Path(root)
    allowed: set[str] = set()
    for path in iter_loose_deploy_files(base, skip_archives=True):
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if rel:
            allowed.add(rel)
            name = Path(rel).name
            if name:
                allowed.add(name)
    return frozenset(allowed)


def verify_deploy_source(
    content_root: Path | str,
    *,
    managed_path: Path | str | None = None,
    allowed_rel_paths: frozenset[str] | None = None,
) -> None:
    """
    Pre-deploy gate: source must exist and contain legal Mod content.

    Delegates to :func:`services.mod_source_integrity.validate_content_root`.
    """
    from services.mod_source_integrity import validate_content_root

    validate_content_root(
        content_root,
        managed_path=managed_path,
        allowed_rel_paths=allowed_rel_paths,
    )


def verify_deploy_result(
    result: Any,
    *,
    check_size: bool = True,
    check_hash: bool = False,
) -> int:
    """
    Post-deploy gate: every manifest target must exist and match source size.

    Optional sha256 when ``check_hash=True``. Raises ``DeployValidationError``.
    Returns validated entry count.
    """
    manifest = getattr(result, "manifest", None)
    if manifest is None:
        raise DeployValidationError(
            "部署结果校验失败：缺少 manifest",
            missing_targets=[],
        )

    entries = list(getattr(manifest, "files", None) or [])
    if not entries:
        raise DeployValidationError(
            "部署结果校验失败：manifest 未记录任何已部署文件",
            missing_targets=[],
        )

    missing: list[str] = []
    size_mismatches: list[str] = []
    hash_mismatches: list[str] = []

    for entry in entries:
        raw_target = str(getattr(entry, "target", "") or "").strip()
        if not raw_target:
            missing.append("(empty target)")
            continue
        target = Path(raw_target)
        if target.is_dir():
            continue
        if not target.is_file():
            missing.append(str(target))
            continue

        raw_source = str(getattr(entry, "source", "") or "").strip()
        source = Path(raw_source) if raw_source else None

        if check_size and source is not None and source.is_file():
            try:
                src_size = int(source.stat().st_size)
                dst_size = int(target.stat().st_size)
            except OSError:
                size_mismatches.append(str(target))
            else:
                if src_size != dst_size:
                    size_mismatches.append(
                        f"{target} (source={src_size} target={dst_size})"
                    )

        if check_hash and source is not None and source.is_file():
            try:
                if _sha256_file(source) != _sha256_file(target):
                    hash_mismatches.append(str(target))
            except OSError:
                hash_mismatches.append(str(target))

    if missing:
        raise DeployValidationError(
            f"部署结果校验失败：缺少 {len(missing)} 个目标",
            missing_targets=missing,
        )
    if size_mismatches:
        raise DeployValidationError(
            f"部署结果校验失败：{len(size_mismatches)} 个文件大小不一致",
            missing_targets=size_mismatches,
        )
    if hash_mismatches:
        raise DeployValidationError(
            f"部署结果校验失败：{len(hash_mismatches)} 个文件 hash 不一致",
            missing_targets=hash_mismatches,
        )
    return len(entries)
