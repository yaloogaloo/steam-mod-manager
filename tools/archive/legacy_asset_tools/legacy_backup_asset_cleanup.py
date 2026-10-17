"""Phase 4 / 4.1: Legacy Backup ``offline/assets`` cleanup (per-file SAFE gate).

Deletes only Backup physical copies that are fully proven recoverable from::

    Backup offline/manifest.json  →  Asset Store (content SHA-256)

Never deletes Asset Store objects, ``.info/assets``, or ``asset_cache``.
Never uses ``shutil.rmtree`` on Backup assets. Unknown / unreferenced files
are retained and reported.

Audit modes (Phase 4.1)::

    FAST      — metadata / size / object existence (no file SHA, no materialize)
    STANDARD  — file SHA-256 + AssetStore.verify (execute default; no materialize)
    DEEP      — STANDARD + temp materialize equality check
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.paths import asset_store_dir, database_path
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    ManifestError,
    validate_manifest_path,
)
from services.asset_store import (
    AssetCorruption,
    AssetNotFound,
    AssetStore,
    sha256_file,
)
from services.backup_asset_migration import (
    backup_offline_manifest_path,
    restore_backup_assets_from_store,
)
from services.info_asset_migration import iter_asset_files, relative_manifest_path
from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA = 1
CHECKPOINT_TOOL = "legacy_backup_asset_cleanup"
DEFAULT_CHECKPOINT = Path("_tmp") / "legacy_backup_cleanup_checkpoint.json"
DETAIL_CAP = 200  # max per-file rows kept in audit JSON summaries


class AuditMode(str, Enum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class CleanupReason(str, Enum):
    SAFE = "SAFE"
    MISSING_MANIFEST = "MISSING_MANIFEST"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    MISSING_CAS = "MISSING_CAS"
    CORRUPT_CAS = "CORRUPT_CAS"
    UNREFERENCED = "UNREFERENCED"
    UNKNOWN = "UNKNOWN"
    PATH_UNSAFE = "PATH_UNSAFE"
    ALREADY_GONE = "ALREADY_GONE"


_UNSAFE_REASONS = frozenset(
    {
        CleanupReason.MISSING_MANIFEST,
        CleanupReason.MANIFEST_MISMATCH,
        CleanupReason.MISSING_CAS,
        CleanupReason.CORRUPT_CAS,
        CleanupReason.PATH_UNSAFE,
        CleanupReason.UNKNOWN,
    }
)
_UNKNOWN_LIKE = frozenset(
    {CleanupReason.UNREFERENCED, CleanupReason.UNKNOWN, CleanupReason.PATH_UNSAFE}
)


@dataclass
class LegacyAssetVerdict:
    mod_id: str
    backup_path: str
    relative_path: str
    size: int = 0
    sha256: str = ""
    cas_path: str = ""
    reason: CleanupReason = CleanupReason.UNKNOWN
    detail: str = ""
    recommended_action: str = ""

    @property
    def verification(self) -> str:
        if self.reason == CleanupReason.SAFE:
            return "safe"
        if self.reason in _UNKNOWN_LIKE:
            return "unknown"
        if self.reason == CleanupReason.ALREADY_GONE:
            return "already_gone"
        return "unsafe"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "backup_path": self.backup_path,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size": self.size,
            "cas_path": self.cas_path,
            "verification": self.verification,
            "reason": self.reason.value,
            "detail": self.detail,
            "recommended_action": self.recommended_action,
        }


@dataclass
class ModCleanupAudit:
    mod_id: str
    backup_offline: str = ""
    has_offline_assets: bool = False
    has_manifest: bool = False
    manifest_valid: bool = False
    manifest_error: str = ""
    asset_files: int = 0
    asset_bytes: int = 0
    verdicts: list[LegacyAssetVerdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "backup_offline": self.backup_offline,
            "has_offline_assets": self.has_offline_assets,
            "has_manifest": self.has_manifest,
            "manifest_valid": self.manifest_valid,
            "manifest_error": self.manifest_error,
            "asset_files": self.asset_files,
            "asset_bytes": self.asset_bytes,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


@dataclass
class LegacyCleanupAuditResult:
    mods_scanned: int = 0
    backups_with_offline_assets: int = 0
    backup_asset_files: int = 0
    backup_asset_bytes: int = 0
    backup_manifests: int = 0
    valid_manifests: int = 0
    invalid_manifests: int = 0
    missing_manifests: int = 0
    assets_covered_by_manifest: int = 0
    assets_not_covered_by_manifest: int = 0
    cas_objects_referenced: int = 0
    missing_cas_objects: int = 0
    corrupt_cas_objects: int = 0
    safe_files: int = 0
    safe_bytes: int = 0
    unsafe_files: int = 0
    unsafe_bytes: int = 0
    unknown_files: int = 0
    unknown_bytes: int = 0
    reason_counts: dict[str, int] = field(default_factory=dict)
    safe_to_delete: list[dict[str, Any]] = field(default_factory=list)
    unsafe_files_detail: list[dict[str, Any]] = field(default_factory=list)
    unknown_files_detail: list[dict[str, Any]] = field(default_factory=list)
    mods: list[ModCleanupAudit] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "mods_scanned": self.mods_scanned,
            "backups_with_offline_assets": self.backups_with_offline_assets,
            "backup_asset_files": self.backup_asset_files,
            "backup_asset_bytes": self.backup_asset_bytes,
            "backup_manifests": self.backup_manifests,
            "valid_manifests": self.valid_manifests,
            "invalid_manifests": self.invalid_manifests,
            "missing_manifests": self.missing_manifests,
            "assets_covered_by_manifest": self.assets_covered_by_manifest,
            "assets_not_covered_by_manifest": self.assets_not_covered_by_manifest,
            "cas_objects_referenced": self.cas_objects_referenced,
            "missing_cas_objects": self.missing_cas_objects,
            "corrupt_cas_objects": self.corrupt_cas_objects,
            "safe_files": self.safe_files,
            "safe_bytes": self.safe_bytes,
            "unsafe_files": self.unsafe_files,
            "unsafe_bytes": self.unsafe_bytes,
            "unknown_files": self.unknown_files,
            "unknown_bytes": self.unknown_bytes,
            "reason_counts": dict(self.reason_counts),
            "safe_to_delete": list(self.safe_to_delete),
            "unsafe_files_detail": list(self.unsafe_files_detail),
            "unknown_files_detail": list(self.unknown_files_detail),
            "mods": [m.to_dict() for m in self.mods],
        }


@dataclass
class ModCleanupExecuteResult:
    mod_id: str
    ok: bool = False
    dry_run: bool = False
    skipped: bool = False
    reason: str = ""
    scanned: int = 0
    safe: int = 0
    deleted: int = 0
    deleted_bytes: int = 0
    skipped_files: int = 0
    skipped_bytes: int = 0
    unknown: int = 0
    unknown_bytes: int = 0
    restore_ok: bool | None = None
    restore_written: int = 0
    issues: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "reason": self.reason,
            "scanned": self.scanned,
            "safe": self.safe,
            "deleted": self.deleted,
            "deleted_bytes": self.deleted_bytes,
            "skipped_files": self.skipped_files,
            "skipped_bytes": self.skipped_bytes,
            "unknown": self.unknown,
            "unknown_bytes": self.unknown_bytes,
            "restore_ok": self.restore_ok,
            "restore_written": self.restore_written,
            "issues": list(self.issues),
            "deleted_paths": list(self.deleted_paths),
        }


@dataclass
class BatchCleanupResult:
    ok: bool = True
    dry_run: bool = False
    mods_scanned: int = 0
    mods_cleaned: int = 0
    mods_skipped: int = 0
    mods_failed: int = 0
    files_deleted: int = 0
    bytes_reclaimed: int = 0
    unsafe_files: int = 0
    unknown_files: int = 0
    missing_cas: int = 0
    corrupt_cas: int = 0
    manifest_errors: int = 0
    asset_store_objects_deleted: int = 0  # must remain 0
    results: list[ModCleanupExecuteResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_scanned": self.mods_scanned,
            "mods_cleaned": self.mods_cleaned,
            "mods_skipped": self.mods_skipped,
            "mods_failed": self.mods_failed,
            "files_deleted": self.files_deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "unsafe_files": self.unsafe_files,
            "unknown_files": self.unknown_files,
            "missing_cas": self.missing_cas,
            "corrupt_cas": self.corrupt_cas,
            "manifest_errors": self.manifest_errors,
            "asset_store_objects_deleted": self.asset_store_objects_deleted,
            "results": [r.to_dict() for r in self.results],
        }


def _list_mod_ids(
    *,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
) -> list[str]:
    if mod_ids is not None:
        ids = [str(m).strip() for m in mod_ids if str(m).strip()]
    else:
        path = Path(db_path) if db_path is not None else database_path()
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        ids = []
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                rows = conn.execute(
                    "SELECT mod_id FROM mods ORDER BY mod_id"
                ).fetchall()
                ids = [str(r[0]) for r in rows]
            finally:
                conn.close()
        except sqlite3.Error:
            # Fall back to scanning backup directories
            root = backup_root("0").parent
            if root.is_dir():
                ids = sorted(
                    p.name for p in root.iterdir() if p.is_dir() and p.name.isdigit()
                )
    if limit is not None and limit > 0:
        ids = ids[:limit]
    return ids


def _load_manifest(dest_offline: Path) -> tuple[AssetManifest | None, str]:
    man_path = backup_offline_manifest_path(dest_offline)
    if not man_path.is_file():
        return None, "missing"
    try:
        return AssetManifest.from_path(man_path), ""
    except (OSError, ManifestError) as exc:
        return None, str(exc)


def _materialize_matches(
    store: AssetStore,
    digest: str,
    expected_size: int,
    original: Path,
) -> tuple[bool, str]:
    """Materialize CAS object to a temp file and compare to *original*."""
    try:
        store.verify(digest)
        src = store.get_path(digest)
    except AssetNotFound:
        return False, "missing cas during materialize"
    except AssetCorruption as exc:
        return False, f"corrupt cas during materialize: {exc}"

    fd, tmp_name = tempfile.mkstemp(prefix=".legacy_cleanup_", suffix=".part")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as out_fh, src.open("rb") as in_fh:
            while True:
                block = in_fh.read(1024 * 1024)
                if not block:
                    break
                out_fh.write(block)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        try:
            size = int(tmp_path.stat().st_size)
        except OSError as exc:
            return False, f"materialize stat failed: {exc}"
        if size != int(expected_size):
            return False, f"materialize size mismatch: {size} != {expected_size}"
        try:
            got = sha256_file(tmp_path)
            want = sha256_file(original)
        except OSError as exc:
            return False, f"materialize hash failed: {exc}"
        if got != want or got != digest:
            return False, f"materialize content mismatch: {got}"
        return True, ""
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def classify_backup_asset_file(
    *,
    mod_id: str,
    dest_offline: Path,
    file_path: Path,
    manifest: AssetManifest | None,
    manifest_error: str,
    store: AssetStore,
    by_path: dict[str, Any] | None,
    mode: AuditMode = AuditMode.STANDARD,
    verified_digests: dict[str, Any] | None = None,
) -> LegacyAssetVerdict:
    """Apply the SAFE gate to one Backup asset file.

    * FAST: size + ``store.has`` / CAS size (no file SHA, no materialize)
    * STANDARD: file SHA-256 + ``store.verify`` (execute default)
    * DEEP: STANDARD + temp materialize equality
    """
    abs_path = str(file_path)
    verdict = LegacyAssetVerdict(
        mod_id=str(mod_id),
        backup_path=abs_path,
        relative_path="",
    )
    try:
        size = int(file_path.stat().st_size)
    except OSError as exc:
        verdict.reason = CleanupReason.UNKNOWN
        verdict.detail = f"unreadable: {exc}"
        verdict.recommended_action = "investigate; do not delete"
        return verdict
    verdict.size = size

    assets_dir = Path(dest_offline) / "assets"
    try:
        rel = relative_manifest_path(assets_dir, file_path)
        validate_manifest_path(rel)
    except (ManifestError, ValueError, OSError) as exc:
        verdict.reason = CleanupReason.PATH_UNSAFE
        verdict.detail = str(exc)
        verdict.recommended_action = "retain; investigate path"
        return verdict
    verdict.relative_path = rel

    if manifest is None:
        if manifest_error and manifest_error != "missing":
            verdict.reason = CleanupReason.MANIFEST_MISMATCH
            verdict.detail = f"invalid manifest: {manifest_error}"
            verdict.recommended_action = "fix manifest; do not delete"
        else:
            verdict.reason = CleanupReason.MISSING_MANIFEST
            verdict.detail = "backup offline/manifest.json missing"
            verdict.recommended_action = "run Phase 3 / 4.1 manifest migration; do not delete"
        return verdict

    assert by_path is not None
    ref = by_path.get(rel)
    if ref is None:
        verdict.reason = CleanupReason.UNREFERENCED
        verdict.detail = "path not listed in backup manifest"
        verdict.recommended_action = "retain + report as data debt"
        if mode != AuditMode.FAST:
            try:
                verdict.sha256 = sha256_file(file_path)
            except OSError:
                pass
        return verdict

    if int(ref.size) != size:
        verdict.reason = CleanupReason.MANIFEST_MISMATCH
        verdict.detail = f"size mismatch file={size} manifest={ref.size}"
        verdict.recommended_action = "investigate; do not delete"
        verdict.sha256 = str(ref.sha256)
        return verdict

    if mode == AuditMode.FAST:
        # Metadata path: trust size match + object presence/size (no content hash).
        verdict.sha256 = str(ref.sha256)
        if not store.has(ref.sha256):
            verdict.reason = CleanupReason.MISSING_CAS
            verdict.detail = f"missing asset object: {ref.sha256}"
            verdict.recommended_action = "restore CAS before cleanup"
            return verdict
        try:
            cas_path = store.get_path(ref.sha256)
            cas_size = int(cas_path.stat().st_size)
        except (AssetNotFound, OSError) as exc:
            verdict.reason = CleanupReason.MISSING_CAS
            verdict.detail = f"cas unreadable: {exc}"
            verdict.recommended_action = "investigate; do not delete"
            return verdict
        if cas_size != size:
            verdict.reason = CleanupReason.MANIFEST_MISMATCH
            verdict.detail = f"cas size mismatch cas={cas_size} file={size}"
            verdict.recommended_action = "investigate; do not delete"
            return verdict
        verdict.cas_path = str(cas_path)
        verdict.reason = CleanupReason.SAFE
        verdict.detail = "fast: size+cas presence (content hash deferred)"
        verdict.recommended_action = "candidate; confirm with standard/deep before delete"
        return verdict

    try:
        digest = sha256_file(file_path)
    except OSError as exc:
        verdict.reason = CleanupReason.UNKNOWN
        verdict.detail = f"hash failed: {exc}"
        verdict.recommended_action = "investigate; do not delete"
        return verdict
    verdict.sha256 = digest

    if digest != ref.sha256:
        verdict.reason = CleanupReason.MANIFEST_MISMATCH
        verdict.detail = f"sha256 mismatch file={digest} manifest={ref.sha256}"
        verdict.recommended_action = "investigate; do not delete"
        return verdict

    cache = verified_digests if verified_digests is not None else {}
    try:
        if digest in cache:
            obj = cache[digest]
        else:
            obj = store.verify(digest)
            cache[digest] = obj
    except AssetNotFound:
        verdict.reason = CleanupReason.MISSING_CAS
        verdict.detail = f"missing asset object: {digest}"
        verdict.recommended_action = "restore CAS from backup before cleanup"
        return verdict
    except AssetCorruption as exc:
        verdict.reason = CleanupReason.CORRUPT_CAS
        verdict.detail = f"corrupt asset object: {exc}"
        verdict.recommended_action = "repair CAS; do not delete backup copy"
        return verdict

    if int(obj.size) != int(ref.size) or int(obj.size) != size:
        verdict.reason = CleanupReason.MANIFEST_MISMATCH
        verdict.detail = (
            f"cas size mismatch cas={obj.size} manifest={ref.size} file={size}"
        )
        verdict.recommended_action = "investigate; do not delete"
        return verdict

    verdict.cas_path = str(obj.path)

    if mode == AuditMode.DEEP:
        ok_mat, mat_detail = _materialize_matches(store, digest, size, file_path)
        if not ok_mat:
            if "missing" in mat_detail.lower():
                verdict.reason = CleanupReason.MISSING_CAS
            elif "corrupt" in mat_detail.lower():
                verdict.reason = CleanupReason.CORRUPT_CAS
            else:
                verdict.reason = CleanupReason.UNKNOWN
            verdict.detail = mat_detail
            verdict.recommended_action = "do not delete"
            return verdict
        verdict.detail = "deep: manifest+cas+materialize verified"
    else:
        verdict.detail = "standard: manifest+file hash+cas verify"

    verdict.reason = CleanupReason.SAFE
    verdict.recommended_action = "safe to delete backup physical copy only"
    return verdict


def audit_mod_legacy_backup_assets(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    mode: AuditMode = AuditMode.STANDARD,
    require_materialize: bool | None = None,
) -> ModCleanupAudit:
    """Audit one Mod. ``require_materialize`` is legacy; prefer ``mode``."""
    if require_materialize is not None:
        mode = AuditMode.DEEP if require_materialize else AuditMode.FAST
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    dest = backup_root(mid) / BACKUP_OFFLINE_DIR
    audit = ModCleanupAudit(mod_id=mid, backup_offline=str(dest))
    assets_dir = dest / "assets"
    files = list(iter_asset_files(assets_dir)) if assets_dir.is_dir() else []
    audit.has_offline_assets = bool(files)
    audit.asset_files = len(files)

    manifest, man_err = _load_manifest(dest)
    man_path = backup_offline_manifest_path(dest)
    audit.has_manifest = man_path.is_file()
    if manifest is not None:
        audit.manifest_valid = True
    elif audit.has_manifest:
        audit.manifest_error = man_err
    else:
        audit.manifest_error = "missing"

    by_path = {a.path: a for a in manifest.assets} if manifest else None
    verified: dict[str, Any] = {}
    for src in files:
        try:
            audit.asset_bytes += int(src.stat().st_size)
        except OSError:
            pass
        verdict = classify_backup_asset_file(
            mod_id=mid,
            dest_offline=dest,
            file_path=src,
            manifest=manifest,
            manifest_error=man_err,
            store=store,
            by_path=by_path,
            mode=mode,
            verified_digests=verified,
        )
        audit.verdicts.append(verdict)
    return audit


def audit_legacy_backup_assets(
    *,
    store: AssetStore | None = None,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    mode: AuditMode = AuditMode.FAST,
    require_materialize: bool | None = None,
    include_file_details: bool = False,
) -> LegacyCleanupAuditResult:
    if require_materialize is not None:
        mode = AuditMode.DEEP if require_materialize else AuditMode.FAST
    store = store or AssetStore(root=asset_store_dir())
    result = LegacyCleanupAuditResult(dry_run=dry_run)
    ids = _list_mod_ids(mod_ids=mod_ids, db_path=db_path, limit=limit)
    cas_seen: set[str] = set()
    missing_cas: set[str] = set()
    corrupt_cas: set[str] = set()

    for mid in ids:
        result.mods_scanned += 1
        mod_audit = audit_mod_legacy_backup_assets(mid, store=store, mode=mode)
        # Keep mod summaries but drop per-file verdicts unless details requested
        if not include_file_details:
            slim = ModCleanupAudit(
                mod_id=mod_audit.mod_id,
                backup_offline=mod_audit.backup_offline,
                has_offline_assets=mod_audit.has_offline_assets,
                has_manifest=mod_audit.has_manifest,
                manifest_valid=mod_audit.manifest_valid,
                manifest_error=mod_audit.manifest_error,
                asset_files=mod_audit.asset_files,
                asset_bytes=mod_audit.asset_bytes,
                verdicts=[],
            )
            result.mods.append(slim)
        else:
            result.mods.append(mod_audit)

        if mod_audit.has_offline_assets:
            result.backups_with_offline_assets += 1
        result.backup_asset_files += mod_audit.asset_files
        result.backup_asset_bytes += mod_audit.asset_bytes

        if mod_audit.has_manifest:
            result.backup_manifests += 1
            if mod_audit.manifest_valid:
                result.valid_manifests += 1
            else:
                result.invalid_manifests += 1
        elif mod_audit.has_offline_assets:
            result.missing_manifests += 1

        for v in mod_audit.verdicts:
            key = v.reason.value
            result.reason_counts[key] = result.reason_counts.get(key, 0) + 1
            if v.reason == CleanupReason.SAFE:
                result.safe_files += 1
                result.safe_bytes += v.size
                result.assets_covered_by_manifest += 1
                if include_file_details and len(result.safe_to_delete) < DETAIL_CAP:
                    result.safe_to_delete.append(v.to_dict())
                if v.sha256:
                    cas_seen.add(v.sha256)
            elif v.reason == CleanupReason.UNREFERENCED:
                result.unknown_files += 1
                result.unknown_bytes += v.size
                result.assets_not_covered_by_manifest += 1
                if include_file_details and len(result.unknown_files_detail) < DETAIL_CAP:
                    result.unknown_files_detail.append(v.to_dict())
            elif v.reason in _UNKNOWN_LIKE:
                result.unknown_files += 1
                result.unknown_bytes += v.size
                if include_file_details and len(result.unknown_files_detail) < DETAIL_CAP:
                    result.unknown_files_detail.append(v.to_dict())
            else:
                result.unsafe_files += 1
                result.unsafe_bytes += v.size
                if include_file_details and len(result.unsafe_files_detail) < DETAIL_CAP:
                    result.unsafe_files_detail.append(v.to_dict())
                if v.reason == CleanupReason.MISSING_CAS and v.sha256:
                    missing_cas.add(v.sha256)
                if v.reason == CleanupReason.CORRUPT_CAS and v.sha256:
                    corrupt_cas.add(v.sha256)
                if (
                    v.reason != CleanupReason.UNREFERENCED
                    and v.relative_path
                    and v.reason
                    not in (
                        CleanupReason.MISSING_MANIFEST,
                        CleanupReason.PATH_UNSAFE,
                        CleanupReason.UNKNOWN,
                    )
                ):
                    result.assets_covered_by_manifest += 1

    result.cas_objects_referenced = len(cas_seen)
    result.missing_cas_objects = len(missing_cas)
    result.corrupt_cas_objects = len(corrupt_cas)
    return result


def _reverify_safe_before_delete(
    verdict: LegacyAssetVerdict,
    *,
    store: AssetStore,
    dest_offline: Path,
) -> tuple[bool, str]:
    """Final triple-check: Backup file ↔ manifest ↔ CAS before unlink."""
    path = Path(verdict.backup_path)
    if not path.is_file():
        return False, "already missing"
    try:
        size = int(path.stat().st_size)
        digest = sha256_file(path)
    except OSError as exc:
        return False, f"re-hash failed: {exc}"
    if size != verdict.size or digest != verdict.sha256:
        return False, "file changed since audit"

    manifest, err = _load_manifest(dest_offline)
    if manifest is None:
        return False, f"manifest gone: {err}"
    by_path = {a.path: a for a in manifest.assets}
    ref = by_path.get(verdict.relative_path)
    if ref is None:
        return False, "path no longer in manifest"
    if ref.sha256 != digest or int(ref.size) != size:
        return False, "manifest no longer matches file"

    try:
        obj = store.verify(digest)
    except (AssetNotFound, AssetCorruption) as exc:
        return False, f"cas re-verify failed: {exc}"
    if int(obj.size) != size:
        return False, "cas size mismatch on re-verify"
    return True, ""


def _prune_empty_asset_dirs(assets_dir: Path) -> None:
    if not assets_dir.is_dir():
        return
    try:
        for path in sorted(assets_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                try:
                    path.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
        try:
            next(assets_dir.iterdir())
        except StopIteration:
            try:
                assets_dir.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    except OSError:
        pass


def load_cleanup_checkpoint(path: Path | None = None) -> dict[str, Any]:
    """Load checkpoint from ``_tmp``; corrupt/missing → empty safe default."""
    target = Path(path) if path is not None else DEFAULT_CHECKPOINT
    empty = {
        "schema_version": CHECKPOINT_SCHEMA,
        "tool": CHECKPOINT_TOOL,
        "last_processed_mod_id": "",
        "cleaned_mod_ids": [],
        "skipped_mod_ids": [],
        "failed_mod_ids": [],
        "files_deleted": 0,
        "bytes_reclaimed": 0,
        "updated_at": "",
    }
    if not target.is_file():
        return empty
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("checkpoint unreadable; starting fresh: %s", target)
        return empty
    if not isinstance(data, dict) or int(data.get("schema_version") or 0) != CHECKPOINT_SCHEMA:
        logger.warning("checkpoint schema mismatch; starting fresh: %s", target)
        return empty
    if data.get("tool") != CHECKPOINT_TOOL:
        return empty
    for key in empty:
        data.setdefault(key, empty[key])
    return data


def save_cleanup_checkpoint(data: dict[str, Any], path: Path | None = None) -> Path:
    target = Path(path) if path is not None else DEFAULT_CHECKPOINT
    if ".." in target.as_posix() or target.is_absolute() and "_tmp" not in target.as_posix().replace("\\", "/"):
        # Prefer _tmp-relative; allow absolute only under repo _tmp
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["schema_version"] = CHECKPOINT_SCHEMA
    payload["tool"] = CHECKPOINT_TOOL
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def cleanup_mod_legacy_backup_assets(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    verify_restore_after: bool = False,
    mode: AuditMode = AuditMode.STANDARD,
) -> ModCleanupExecuteResult:
    """
    Per-file cleanup for one Mod. Never deletes CAS objects.

    Default *mode* is STANDARD (file hash + CAS verify, no materialize).
    Use DEEP only when investigating corruption.
    """
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    dest = backup_root(mid) / BACKUP_OFFLINE_DIR
    out = ModCleanupExecuteResult(mod_id=mid, dry_run=dry_run)

    if not dest.is_dir():
        out.ok = True
        out.skipped = True
        out.reason = "no backup offline dir"
        return out

    # Execute never uses FAST (would skip content hash).
    exec_mode = mode if mode != AuditMode.FAST else AuditMode.STANDARD
    mod_audit = audit_mod_legacy_backup_assets(mid, store=store, mode=exec_mode)
    out.scanned = len(mod_audit.verdicts)
    if out.scanned == 0:
        out.ok = True
        out.skipped = True
        out.reason = "no legacy backup assets"
        return out

    safe = [v for v in mod_audit.verdicts if v.reason == CleanupReason.SAFE]
    unknown = [
        v
        for v in mod_audit.verdicts
        if v.reason in (CleanupReason.UNREFERENCED, CleanupReason.UNKNOWN, CleanupReason.PATH_UNSAFE)
    ]
    unsafe = [v for v in mod_audit.verdicts if v not in safe and v not in unknown]
    out.safe = len(safe)
    out.unknown = len(unknown)
    out.unknown_bytes = sum(v.size for v in unknown)
    out.skipped_files = len(unsafe) + len(unknown)
    out.skipped_bytes = sum(v.size for v in unsafe) + out.unknown_bytes

    if dry_run:
        out.ok = True
        out.reason = (
            f"dry-run: safe={len(safe)} unsafe={len(unsafe)} unknown={len(unknown)}"
        )
        out.deleted = 0
        out.deleted_bytes = sum(v.size for v in safe)
        return out

    deleted = 0
    deleted_bytes = 0
    for v in safe:
        ok, detail = _reverify_safe_before_delete(v, store=store, dest_offline=dest)
        if not ok:
            out.issues.append(f"{v.relative_path}: skip delete ({detail})")
            out.skipped_files += 1
            out.skipped_bytes += v.size
            continue
        path = Path(v.backup_path)
        try:
            path.unlink()
        except OSError as exc:
            out.issues.append(f"{v.relative_path}: unlink failed: {exc}")
            continue
        if path.is_file():
            out.issues.append(f"{v.relative_path}: still present after unlink")
            continue
        try:
            store.verify(v.sha256)
        except (AssetNotFound, AssetCorruption) as exc:
            out.issues.append(
                f"{v.relative_path}: CAS invalid after delete (CRITICAL): {exc}"
            )
            out.ok = False
            out.reason = "cas broken after delete"
            return out
        man, err = _load_manifest(dest)
        if man is None:
            out.issues.append(f"manifest invalid after delete: {err}")
            out.ok = False
            out.reason = "manifest broken after delete"
            return out

        deleted += 1
        deleted_bytes += v.size
        out.deleted_paths.append(v.relative_path)

    _prune_empty_asset_dirs(dest / "assets")

    out.deleted = deleted
    out.deleted_bytes = deleted_bytes

    if verify_restore_after and deleted > 0:
        with tempfile.TemporaryDirectory(prefix="legacy_restore_") as tmp:
            tmp_root = Path(tmp) / "offline"
            tmp_root.mkdir(parents=True)
            for name in ("index.html", MANIFEST_FILENAME):
                src = dest / name
                if src.is_file():
                    (tmp_root / name).write_bytes(src.read_bytes())
            restore = restore_backup_assets_from_store(tmp_root, store=store)
            out.restore_ok = bool(restore.ok)
            out.restore_written = int(restore.written)
            if not restore.ok:
                out.issues.extend(restore.issues or [restore.reason])
                out.ok = False
                out.reason = f"post-delete restore failed: {restore.reason}"
                return out
            try:
                man = AssetManifest.from_path(tmp_root / MANIFEST_FILENAME)
            except (OSError, ManifestError) as exc:
                out.ok = False
                out.reason = f"restore manifest unreadable: {exc}"
                return out
            for ref in man.assets:
                got = tmp_root / Path(ref.path)
                if not got.is_file():
                    out.ok = False
                    out.reason = f"restore missing {ref.path}"
                    return out
                if int(got.stat().st_size) != int(ref.size):
                    out.ok = False
                    out.reason = f"restore size mismatch {ref.path}"
                    return out
                if sha256_file(got) != ref.sha256:
                    out.ok = False
                    out.reason = f"restore hash mismatch {ref.path}"
                    return out

    out.ok = True
    out.reason = (
        f"deleted={deleted} skipped={out.skipped_files} unknown={out.unknown}"
    )
    return out


def cleanup_all_legacy_backup_assets(
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
    verify_restore_after: bool = False,
    mode: AuditMode = AuditMode.STANDARD,
    checkpoint_path: Path | None = None,
    resume: bool = False,
) -> BatchCleanupResult:
    store = store or AssetStore(root=asset_store_dir())
    batch = BatchCleanupResult(dry_run=dry_run, ok=True)
    ids = _list_mod_ids(mod_ids=mod_ids, db_path=db_path, limit=limit)

    ckpt = load_cleanup_checkpoint(checkpoint_path) if resume else {
        "schema_version": CHECKPOINT_SCHEMA,
        "tool": CHECKPOINT_TOOL,
        "last_processed_mod_id": "",
        "cleaned_mod_ids": [],
        "skipped_mod_ids": [],
        "failed_mod_ids": [],
        "files_deleted": 0,
        "bytes_reclaimed": 0,
        "updated_at": "",
    }
    done = set(str(x) for x in (ckpt.get("cleaned_mod_ids") or []))
    if resume:
        batch.files_deleted = int(ckpt.get("files_deleted") or 0)
        batch.bytes_reclaimed = int(ckpt.get("bytes_reclaimed") or 0)

    for mid in ids:
        if resume and mid in done:
            batch.mods_skipped += 1
            batch.mods_scanned += 1
            continue
        batch.mods_scanned += 1
        try:
            result = cleanup_mod_legacy_backup_assets(
                mid,
                store=store,
                dry_run=dry_run,
                verify_restore_after=verify_restore_after,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("legacy backup cleanup crashed mod_id=%s", mid)
            result = ModCleanupExecuteResult(
                mod_id=mid, ok=False, dry_run=dry_run, reason=f"crash: {exc}"
            )
        batch.results.append(result)
        ckpt["last_processed_mod_id"] = mid

        if result.skipped and result.ok:
            batch.mods_skipped += 1
            ckpt.setdefault("skipped_mod_ids", []).append(mid)
        elif result.ok and (result.deleted > 0 or (dry_run and result.safe > 0)):
            if not dry_run and result.deleted > 0:
                batch.mods_cleaned += 1
                done.add(mid)
                ckpt.setdefault("cleaned_mod_ids", []).append(mid)
            elif dry_run and result.safe > 0:
                batch.mods_cleaned += 1
            else:
                batch.mods_skipped += 1
                ckpt.setdefault("skipped_mod_ids", []).append(mid)
            if not dry_run:
                batch.files_deleted += result.deleted
                batch.bytes_reclaimed += result.deleted_bytes
            batch.unsafe_files += max(0, result.skipped_files - result.unknown)
            batch.unknown_files += result.unknown
        elif result.ok:
            batch.mods_skipped += 1
            ckpt.setdefault("skipped_mod_ids", []).append(mid)
            batch.unknown_files += result.unknown
            batch.unsafe_files += max(0, result.skipped_files - result.unknown)
        else:
            batch.mods_failed += 1
            batch.ok = False
            ckpt.setdefault("failed_mod_ids", []).append(mid)

        ckpt["files_deleted"] = batch.files_deleted
        ckpt["bytes_reclaimed"] = batch.bytes_reclaimed
        if not dry_run:
            try:
                save_cleanup_checkpoint(ckpt, checkpoint_path)
            except OSError as exc:
                logger.warning("checkpoint save failed: %s", exc)

        for issue in result.issues:
            low = issue.lower()
            if "missing asset object" in low or ("cas" in low and "missing" in low):
                batch.missing_cas += 1
            if "corrupt" in low:
                batch.corrupt_cas += 1
            if "manifest" in low:
                batch.manifest_errors += 1

    batch.asset_store_objects_deleted = 0
    return batch


def iter_safe_delete_candidates(
    audit: LegacyCleanupAuditResult,
) -> Iterator[dict[str, Any]]:
    yield from audit.safe_to_delete
