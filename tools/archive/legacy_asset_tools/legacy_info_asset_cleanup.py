"""Phase 7: Legacy LIVE ``.info/assets`` retirement (SAFE delete only).

Deletes only physical ``.info[/offline]/assets/*`` copies that are fully
proven recoverable from::

    .info[/offline]/manifest.json  →  Asset Store (content SHA-256)

Never deletes::

    .info/metadata.json
    .info/manifest.json
    index.html
    Asset Store objects
    Backup / asset_cache / DB

Mod-level classification::

    SAFE_DELETE — all SAFE criteria including OPEN readiness
    KEEP — MISSING_MANIFEST | MISSING_CAS_OBJECT | HASH_MISMATCH |
           UNKNOWN_FILE | INVALID_MANIFEST
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from core.paths import asset_store_dir, database_path
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    ManifestError,
    verify_manifest_against_store,
)
from services.asset_store import (
    AssetCorruption,
    AssetNotFound,
    AssetStore,
    sha256_file,
)
from services.info_asset_migration import (
    discover_info_asset_trees,
    iter_asset_files,
    list_mod_ids_with_paths,
    relative_manifest_path,
    resolve_mod_managed_path,
)
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA = 1
CHECKPOINT_TOOL = "legacy_info_asset_cleanup"
DEFAULT_CHECKPOINT = Path("_tmp") / "info_asset_cleanup_checkpoint.json"
AUDIT_JSON = Path("_tmp") / "info_asset_cleanup_audit.json"
AUDIT_MD = Path("_tmp") / "info_asset_cleanup_audit.md"
DETAIL_CAP = 100
DEFAULT_DELETE_BATCH_SIZE = 2000
DEFAULT_DELETE_BATCH_PAUSE_MS = 40
DEFAULT_CHECKPOINT_EVERY_FILES = 2000


class KeepReason(str, Enum):
    SAFE_DELETE = "SAFE_DELETE"
    MISSING_MANIFEST = "MISSING_MANIFEST"
    MISSING_CAS_OBJECT = "MISSING_CAS_OBJECT"
    HASH_MISMATCH = "HASH_MISMATCH"
    UNKNOWN_FILE = "UNKNOWN_FILE"
    INVALID_MANIFEST = "INVALID_MANIFEST"
    OPEN_FAILED = "OPEN_FAILED"
    NO_ASSETS = "NO_ASSETS"
    PATH_MISSING = "PATH_MISSING"


class OpenCheckMode(str, Enum):
    """How strictly OPEN is proven during audit."""

    MANIFEST = "manifest"  # usable LIVE manifest + CAS (Phase 6 OPEN precondition)
    MATERIALIZE = "materialize"  # full ensure_live_offline_openable


@dataclass
class InfoAssetFileVerdict:
    relative_path: str
    absolute_path: str
    size: int = 0
    sha256: str = ""
    reason: KeepReason = KeepReason.UNKNOWN_FILE
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "size": self.size,
            "sha256": self.sha256,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass
class ModInfoAssetAudit:
    mod_id: str
    managed_path: str = ""
    classification: KeepReason = KeepReason.PATH_MISSING
    offline_root: str = ""
    manifest_path: str = ""
    manifest_sha256: str = ""
    manifest_assets: int = 0
    asset_files: int = 0
    asset_bytes: int = 0
    safe_files: int = 0
    safe_bytes: int = 0
    open_ok: bool = False
    open_path: str = ""
    keep_reasons: list[str] = field(default_factory=list)
    file_verdicts: list[InfoAssetFileVerdict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def is_safe_delete(self) -> bool:
        return self.classification == KeepReason.SAFE_DELETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "managed_path": self.managed_path,
            "classification": self.classification.value,
            "offline_root": self.offline_root,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "manifest_assets": self.manifest_assets,
            "asset_files": self.asset_files,
            "asset_bytes": self.asset_bytes,
            "safe_files": self.safe_files,
            "safe_bytes": self.safe_bytes,
            "open_ok": self.open_ok,
            "open_path": self.open_path,
            "keep_reasons": list(self.keep_reasons),
            "issues": list(self.issues)[:DETAIL_CAP],
            "file_verdicts": [
                v.to_dict()
                for v in self.file_verdicts[:DETAIL_CAP]
                if v.reason != KeepReason.SAFE_DELETE
            ],
        }


@dataclass
class InfoAssetCleanupAuditResult:
    mods_scanned: int = 0
    mods_with_info_assets: int = 0
    safe_mods: int = 0
    keep_mods: int = 0
    no_assets_mods: int = 0
    files: int = 0
    bytes_total: int = 0
    bytes_reclaimable: int = 0
    files_reclaimable: int = 0
    reason_breakdown: dict[str, int] = field(default_factory=dict)
    open_check: str = OpenCheckMode.MANIFEST.value
    mods: list[ModInfoAssetAudit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mods_scanned": self.mods_scanned,
            "mods_with_info_assets": self.mods_with_info_assets,
            "safe_mods": self.safe_mods,
            "keep_mods": self.keep_mods,
            "no_assets_mods": self.no_assets_mods,
            "files": self.files,
            "bytes_total": self.bytes_total,
            "files_reclaimable": self.files_reclaimable,
            "bytes_reclaimable": self.bytes_reclaimable,
            "reason_breakdown": dict(self.reason_breakdown),
            "open_check": self.open_check,
            "mods": [m.to_dict() for m in self.mods],
        }


@dataclass
class ModCleanupExecuteResult:
    mod_id: str
    ok: bool = False
    skipped: bool = False
    dry_run: bool = True
    reason: str = ""
    classification: str = ""
    verify_mode: str = "light"
    asset_count_before: int = 0
    bytes_before: int = 0
    manifest_sha256_before: str = ""
    manifest_sha256_after: str = ""
    deleted_files: int = 0
    deleted_bytes: int = 0
    open_ok: bool = False
    miss_ok: bool = False
    repair_ok: bool = False
    light_ok: bool = False
    cas_objects_deleted: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "ok": self.ok,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
            "reason": self.reason,
            "classification": self.classification,
            "verify_mode": self.verify_mode,
            "asset_count_before": self.asset_count_before,
            "bytes_before": self.bytes_before,
            "manifest_sha256_before": self.manifest_sha256_before,
            "manifest_sha256_after": self.manifest_sha256_after,
            "deleted_files": self.deleted_files,
            "deleted_bytes": self.deleted_bytes,
            "open_ok": self.open_ok,
            "miss_ok": self.miss_ok,
            "repair_ok": self.repair_ok,
            "light_ok": self.light_ok,
            "cas_objects_deleted": self.cas_objects_deleted,
            "issues": list(self.issues),
        }


@dataclass
class BatchCleanupResult:
    ok: bool = False
    dry_run: bool = True
    mods_attempted: int = 0
    mods_cleaned: int = 0
    mods_skipped: int = 0
    mods_failed: int = 0
    files_deleted: int = 0
    bytes_reclaimed: int = 0
    stopped_on: str = ""
    verify_mode: str = "light"
    sample_verify_mod_ids: list[str] = field(default_factory=list)
    sample_verify_results: list[ModCleanupExecuteResult] = field(default_factory=list)
    results: list[ModCleanupExecuteResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_attempted": self.mods_attempted,
            "mods_cleaned": self.mods_cleaned,
            "mods_skipped": self.mods_skipped,
            "mods_failed": self.mods_failed,
            "files_deleted": self.files_deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "stopped_on": self.stopped_on,
            "verify_mode": self.verify_mode,
            "sample_verify_mod_ids": list(self.sample_verify_mod_ids),
            "sample_verify_results": [r.to_dict() for r in self.sample_verify_results],
            "results": [r.to_dict() for r in self.results],
        }


def _file_sha256(path: Path) -> str:
    return sha256_file(path)


def _manifest_file_digest(path: Path) -> str:
    return sha256_file(path)


def _count_assets(folder: Path) -> tuple[int, int]:
    files = 0
    nbytes = 0
    for tree in discover_info_asset_trees(folder):
        for p in iter_asset_files(tree.assets_dir):
            try:
                nbytes += int(p.stat().st_size)
                files += 1
            except OSError:
                pass
    return files, nbytes


def _verify_open(
    managed_path: Path,
    *,
    store: AssetStore,
    mode: OpenCheckMode,
) -> tuple[bool, str]:
    from services.info_asset_runtime import (
        ensure_live_offline_openable,
        load_usable_live_manifest,
    )

    index = resolve_offline_page(managed_path)
    if index is None:
        return False, "no offline index"
    if mode == OpenCheckMode.MANIFEST:
        man = load_usable_live_manifest(index, store=store)
        if man is None:
            return False, "usable LIVE manifest+CAS missing"
        return True, str(index)
    opened = ensure_live_offline_openable(managed_path, store=store)
    if opened is None:
        return False, "ensure_live_offline_openable failed"
    return True, str(opened)


def audit_mod_info_assets(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    managed_path: Path | None = None,
    open_check: OpenCheckMode = OpenCheckMode.MANIFEST,
    db_path: Path | None = None,
) -> ModInfoAssetAudit:
    """Classify one Mod's ``.info/.../assets`` for SAFE_DELETE vs KEEP."""
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    out = ModInfoAssetAudit(mod_id=mid)

    folder = managed_path or resolve_mod_managed_path(mid, db_path=db_path)
    if folder is None or not Path(folder).is_dir():
        out.classification = KeepReason.PATH_MISSING
        out.keep_reasons = [KeepReason.PATH_MISSING.value]
        return out

    folder = Path(folder)
    out.managed_path = str(folder)
    trees = discover_info_asset_trees(folder)
    if not trees:
        out.classification = KeepReason.NO_ASSETS
        out.keep_reasons = [KeepReason.NO_ASSETS.value]
        return out

    # Prefer the tree that matches the resolved offline index when possible.
    index = resolve_offline_page(folder)
    primary = trees[0]
    if index is not None:
        for tree in trees:
            if tree.offline_root.resolve() == index.parent.resolve():
                primary = tree
                break

    out.offline_root = str(primary.offline_root)
    out.manifest_path = str(primary.manifest_path)

    keep: set[str] = set()
    all_files: list[Path] = []
    for tree in trees:
        all_files.extend(list(iter_asset_files(tree.assets_dir)))

    out.asset_files = len(all_files)
    for p in all_files:
        try:
            out.asset_bytes += int(p.stat().st_size)
        except OSError:
            pass

    if not primary.manifest_path.is_file():
        out.classification = KeepReason.MISSING_MANIFEST
        out.keep_reasons = [KeepReason.MISSING_MANIFEST.value]
        return out

    try:
        manifest = AssetManifest.from_path(primary.manifest_path)
        out.manifest_sha256 = _manifest_file_digest(primary.manifest_path)
    except (OSError, ManifestError) as exc:
        out.classification = KeepReason.INVALID_MANIFEST
        out.keep_reasons = [KeepReason.INVALID_MANIFEST.value]
        out.issues.append(str(exc))
        return out

    out.manifest_assets = len(manifest.assets)
    by_path = {a.path: a for a in manifest.assets}

    cas_issues = verify_manifest_against_store(manifest, store)
    if cas_issues:
        # Distinguish missing vs other
        missing = any("missing" in i.lower() for i in cas_issues)
        if missing:
            keep.add(KeepReason.MISSING_CAS_OBJECT.value)
            out.classification = KeepReason.MISSING_CAS_OBJECT
        else:
            keep.add(KeepReason.HASH_MISMATCH.value)
            out.classification = KeepReason.HASH_MISMATCH
        out.keep_reasons = sorted(keep)
        out.issues.extend(cas_issues[:20])
        return out

    # Per-file correspondence
    for tree in trees:
        for path in iter_asset_files(tree.assets_dir):
            try:
                rel = relative_manifest_path(tree.assets_dir, path)
                size = int(path.stat().st_size)
                digest = _file_sha256(path)
            except (OSError, ManifestError) as exc:
                v = InfoAssetFileVerdict(
                    relative_path=str(path),
                    absolute_path=str(path),
                    reason=KeepReason.UNKNOWN_FILE,
                    detail=str(exc),
                )
                out.file_verdicts.append(v)
                keep.add(KeepReason.UNKNOWN_FILE.value)
                continue

            ref = by_path.get(rel)
            if ref is None:
                v = InfoAssetFileVerdict(
                    relative_path=rel,
                    absolute_path=str(path),
                    size=size,
                    sha256=digest,
                    reason=KeepReason.UNKNOWN_FILE,
                    detail="not in manifest",
                )
                out.file_verdicts.append(v)
                keep.add(KeepReason.UNKNOWN_FILE.value)
                continue

            if digest != ref.sha256 or size != int(ref.size):
                v = InfoAssetFileVerdict(
                    relative_path=rel,
                    absolute_path=str(path),
                    size=size,
                    sha256=digest,
                    reason=KeepReason.HASH_MISMATCH,
                    detail=(
                        f"disk={digest}/{size} manifest={ref.sha256}/{ref.size}"
                    ),
                )
                out.file_verdicts.append(v)
                keep.add(KeepReason.HASH_MISMATCH.value)
                continue

            try:
                store.verify(digest)
            except (AssetNotFound, AssetCorruption) as exc:
                v = InfoAssetFileVerdict(
                    relative_path=rel,
                    absolute_path=str(path),
                    size=size,
                    sha256=digest,
                    reason=KeepReason.MISSING_CAS_OBJECT,
                    detail=str(exc),
                )
                out.file_verdicts.append(v)
                keep.add(KeepReason.MISSING_CAS_OBJECT.value)
                continue

            out.file_verdicts.append(
                InfoAssetFileVerdict(
                    relative_path=rel,
                    absolute_path=str(path),
                    size=size,
                    sha256=digest,
                    reason=KeepReason.SAFE_DELETE,
                    detail="manifest+cas verified",
                )
            )
            out.safe_files += 1
            out.safe_bytes += size

    if keep:
        # Prefer strongest keep reason ordering
        for preferred in (
            KeepReason.MISSING_CAS_OBJECT,
            KeepReason.HASH_MISMATCH,
            KeepReason.UNKNOWN_FILE,
            KeepReason.INVALID_MANIFEST,
            KeepReason.MISSING_MANIFEST,
        ):
            if preferred.value in keep:
                out.classification = preferred
                break
        out.keep_reasons = sorted(keep)
        return out

    # All disk files SAFE; require OPEN readiness
    open_ok, open_path = _verify_open(folder, store=store, mode=open_check)
    out.open_ok = open_ok
    out.open_path = open_path
    if not open_ok:
        out.classification = KeepReason.OPEN_FAILED
        out.keep_reasons = [KeepReason.OPEN_FAILED.value]
        out.issues.append(open_path)
        return out

    out.classification = KeepReason.SAFE_DELETE
    out.keep_reasons = [KeepReason.SAFE_DELETE.value]
    return out


def audit_all_info_assets(
    *,
    store: AssetStore | None = None,
    open_check: OpenCheckMode = OpenCheckMode.MANIFEST,
    limit: int = 0,
    mod_ids: list[str] | None = None,
    db_path: Path | None = None,
    progress_every: int = 50,
) -> InfoAssetCleanupAuditResult:
    """Scan Mods and classify ``.info/assets`` retirement safety."""
    store = store or AssetStore(root=asset_store_dir())
    result = InfoAssetCleanupAuditResult(open_check=open_check.value)

    if mod_ids:
        pairs: list[tuple[str, Path]] = []
        for mid in mod_ids:
            folder = resolve_mod_managed_path(mid, db_path=db_path)
            if folder is not None:
                pairs.append((str(mid), folder))
    else:
        pairs = list_mod_ids_with_paths(db_path=db_path)

    if limit > 0:
        pairs = pairs[: int(limit)]

    for i, (mid, folder) in enumerate(pairs, 1):
        if progress_every and i % progress_every == 0:
            logger.info(
                "info asset audit progress %s/%s safe=%s keep=%s",
                i,
                len(pairs),
                result.safe_mods,
                result.keep_mods,
            )
        mod = audit_mod_info_assets(
            mid,
            store=store,
            managed_path=folder,
            open_check=open_check,
            db_path=db_path,
        )
        result.mods_scanned += 1
        result.mods.append(mod)
        result.reason_breakdown[mod.classification.value] = (
            result.reason_breakdown.get(mod.classification.value, 0) + 1
        )

        if mod.classification == KeepReason.NO_ASSETS:
            result.no_assets_mods += 1
            continue
        if mod.classification == KeepReason.PATH_MISSING:
            result.keep_mods += 1
            continue

        result.mods_with_info_assets += 1
        result.files += mod.asset_files
        result.bytes_total += mod.asset_bytes

        if mod.is_safe_delete:
            result.safe_mods += 1
            result.files_reclaimable += mod.safe_files
            result.bytes_reclaimable += mod.safe_bytes
        else:
            result.keep_mods += 1

    return result


def write_audit_reports(
    audit: InfoAssetCleanupAuditResult,
    *,
    json_path: Path | None = None,
    md_path: Path | None = None,
) -> tuple[Path, Path]:
    json_out = Path(json_path) if json_path else AUDIT_JSON
    md_out = Path(md_path) if md_path else AUDIT_MD
    json_out.parent.mkdir(parents=True, exist_ok=True)
    payload = audit.to_dict()
    # Compact mods list for summary MD; full JSON keeps classifications
    json_out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    lines = [
        "# Phase 7 — Legacy `.info/assets` cleanup audit",
        "",
        f"- mods_scanned: {audit.mods_scanned}",
        f"- mods_with_info_assets: {audit.mods_with_info_assets}",
        f"- safe_mods (SAFE_DELETE): {audit.safe_mods}",
        f"- keep_mods: {audit.keep_mods}",
        f"- no_assets_mods: {audit.no_assets_mods}",
        f"- files: {audit.files}",
        f"- bytes_total: {audit.bytes_total}",
        f"- files_reclaimable: {audit.files_reclaimable}",
        f"- bytes_reclaimable: {audit.bytes_reclaimable}",
        f"- open_check: {audit.open_check}",
        "",
        "## Reason breakdown",
        "",
        "```",
        json.dumps(audit.reason_breakdown, indent=2, ensure_ascii=False),
        "```",
        "",
        "## KEEP sample (first 30)",
        "",
    ]
    keep_samples = [
        m
        for m in audit.mods
        if m.classification
        not in (KeepReason.SAFE_DELETE, KeepReason.NO_ASSETS)
    ][:30]
    for m in keep_samples:
        lines.append(
            f"- mod_id={m.mod_id} reason={m.classification.value} "
            f"files={m.asset_files} bytes={m.asset_bytes}"
        )
    lines.append("")
    md_out.write_text("\n".join(lines), encoding="utf-8")
    return json_out, md_out


def _prune_empty_dirs(assets_dir: Path) -> None:
    if not assets_dir.is_dir():
        return
    try:
        for path in sorted(
            assets_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True
        ):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            assets_dir.rmdir()
        except OSError:
            pass
    except OSError:
        pass


def _delete_info_assets_only(
    folder: Path,
    *,
    batch_size: int = 2000,
    batch_pause_ms: int = 40,
    on_batch: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, int, list[str]]:
    """Unlink only files under ``.info/.../assets``. Never touches sidecars.

    Windows-friendly: pause every *batch_size* unlinks so disk is not saturated.
    """
    deleted = 0
    nbytes = 0
    issues: list[str] = []
    since_batch = 0
    pause_s = max(0.0, float(batch_pause_ms) / 1000.0)
    for tree in discover_info_asset_trees(folder):
        for path in list(iter_asset_files(tree.assets_dir)):
            if should_cancel is not None and should_cancel():
                issues.append("cancelled during delete")
                return deleted, nbytes, issues
            try:
                size = int(path.stat().st_size)
            except OSError:
                size = 0
            try:
                path.unlink()
            except OSError as exc:
                issues.append(f"unlink failed {path}: {exc}")
                continue
            if path.is_file():
                issues.append(f"still present after unlink: {path}")
                continue
            deleted += 1
            nbytes += size
            since_batch += 1
            if since_batch >= max(1, int(batch_size)):
                if on_batch is not None:
                    try:
                        on_batch(deleted, nbytes)
                    except Exception:  # noqa: BLE001
                        pass
                since_batch = 0
                if pause_s > 0:
                    time.sleep(pause_s)
        _prune_empty_dirs(tree.assets_dir)
    return deleted, nbytes, issues


def _lightweight_safe_precheck(
    folder: Path,
    *,
    store: AssetStore,
    audit: ModInfoAssetAudit,
) -> tuple[bool, list[str]]:
    """
    Trust a prior SAFE_DELETE audit without re-hashing every disk file.

    Still requires: manifest present, digest matches audit snapshot (when
    provided), and every manifest object verifies in Asset Store.
    """
    issues: list[str] = []
    manifest_path = Path(audit.manifest_path)
    if not manifest_path.is_file():
        return False, ["manifest missing"]
    try:
        digest = _manifest_file_digest(manifest_path)
    except OSError as exc:
        return False, [f"manifest unreadable: {exc}"]
    if audit.manifest_sha256 and digest != audit.manifest_sha256:
        return False, [
            f"manifest changed since audit: {audit.manifest_sha256} -> {digest}"
        ]
    try:
        man = AssetManifest.from_path(manifest_path)
    except (OSError, ManifestError) as exc:
        return False, [f"invalid manifest: {exc}"]
    cas_issues = verify_manifest_against_store(man, store)
    if cas_issues:
        return False, cas_issues[:10]
    # Ensure assets still exist to delete (noop if already gone).
    files, _ = _count_assets(folder)
    if files <= 0:
        return True, []
    return True, issues


def _post_delete_verify_light(
    folder: Path,
    *,
    store: AssetStore,
    manifest_path: Path,
    manifest_sha_before: str,
    expected_manifest_assets: int | None = None,
) -> tuple[bool, list[str]]:
    """
    Production batch post-delete checks (no offline_view / OPEN / Repair).

    * ``.info/assets`` absent or empty
    * ``manifest.json`` present and unchanged
    * manifest asset count matches expectation (when provided)
    * every manifest object ``AssetStore.verify`` PASS
    """
    issues: list[str] = []
    remaining, _ = _count_assets(folder)
    if remaining != 0:
        issues.append(f"assets still present after delete: {remaining}")

    if not manifest_path.is_file():
        issues.append("manifest missing after delete")
        return False, issues
    try:
        after = _manifest_file_digest(manifest_path)
    except OSError as exc:
        issues.append(f"manifest hash failed: {exc}")
        return False, issues
    if after != manifest_sha_before:
        issues.append(
            f"manifest changed: before={manifest_sha_before} after={after}"
        )
        return False, issues

    try:
        man = AssetManifest.from_path(manifest_path)
    except (OSError, ManifestError) as exc:
        issues.append(f"manifest unreadable: {exc}")
        return False, issues

    if expected_manifest_assets is not None:
        if len(man.assets) != int(expected_manifest_assets):
            issues.append(
                f"manifest asset count mismatch: "
                f"got={len(man.assets)} expected={expected_manifest_assets}"
            )

    cas_issues = verify_manifest_against_store(man, store)
    if cas_issues:
        issues.extend(cas_issues[:10])

    return not issues, issues


def _post_delete_verify_deep(
    folder: Path,
    *,
    mod_id: str,
    store: AssetStore,
    manifest_path: Path,
    manifest_sha_before: str,
    expected_manifest_assets: int | None = None,
) -> tuple[bool, bool, bool, list[str]]:
    """Light checks + CAS materialize via OPEN / MISS / Repair (gate only)."""
    from services.info_asset_runtime import repair_live_from_cas
    from services.info_asset_runtime import ensure_live_offline_openable

    light_ok, issues = _post_delete_verify_light(
        folder,
        store=store,
        manifest_path=manifest_path,
        manifest_sha_before=manifest_sha_before,
        expected_manifest_assets=expected_manifest_assets,
    )
    if not light_ok:
        return False, False, False, issues

    opened = ensure_live_offline_openable(folder, store=store)
    open_ok = opened is not None
    if not open_ok:
        issues.append("OPEN failed after delete")

    remaining, _ = _count_assets(folder)
    if remaining != 0:
        issues.append(f"assets present during deep verify: {remaining}")
        return False, open_ok, False, issues

    repair = repair_live_from_cas(
        folder, mod_id=mod_id, store=store
    )
    repair_ok = bool(repair.ok)
    if not repair_ok:
        issues.append(f"Repair failed: {repair.reason}")
    again, _ = _count_assets(folder)
    if again != 0:
        issues.append(f"Repair recreated .info/assets: {again}")
        repair_ok = False

    miss_ok = open_ok and repair_ok and remaining == 0
    ok = light_ok and open_ok and repair_ok and not issues
    return ok, open_ok, repair_ok, issues


def _post_delete_verify(
    folder: Path,
    *,
    mod_id: str,
    store: AssetStore,
    manifest_path: Path,
    manifest_sha_before: str,
) -> tuple[bool, bool, bool, list[str]]:
    """Backward-compatible alias for deep post-delete verification."""
    return _post_delete_verify_deep(
        folder,
        mod_id=mod_id,
        store=store,
        manifest_path=manifest_path,
        manifest_sha_before=manifest_sha_before,
    )


def cleanup_mod_info_assets(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    open_check: OpenCheckMode = OpenCheckMode.MATERIALIZE,
    db_path: Path | None = None,
    stop_on_failure: bool = True,
    trust_audit: bool = False,
    audit_snapshot: ModInfoAssetAudit | None = None,
    batch_size: int = DEFAULT_DELETE_BATCH_SIZE,
    batch_pause_ms: int = DEFAULT_DELETE_BATCH_PAUSE_MS,
    on_batch: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    deep_verify: bool = False,
) -> ModCleanupExecuteResult:
    """
    Delete SAFE ``.info/assets`` for one Mod.

    Post-delete verification:
    * default (*deep_verify=False*): lightweight — assets gone, manifest
      unchanged, CAS ``verify`` for every manifest hash (no offline_view).
    * *deep_verify=True*: also materialize OPEN / MISS / Repair (gate only).

    *trust_audit*: when True and *audit_snapshot* is SAFE_DELETE, skip
    per-file disk hashing (still verifies manifest ↔ Asset Store).
    """
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    out = ModCleanupExecuteResult(
        mod_id=mid,
        dry_run=dry_run,
        verify_mode="deep" if deep_verify else "light",
    )

    # Pre-count store objects for safety (never delete CAS)
    try:
        store_before = sum(1 for _ in store.iter_objects())
    except Exception:  # noqa: BLE001
        store_before = -1

    audit: ModInfoAssetAudit
    if (
        trust_audit
        and audit_snapshot is not None
        and audit_snapshot.is_safe_delete
        and str(audit_snapshot.mod_id) == mid
    ):
        audit = audit_snapshot
        folder = Path(audit.managed_path)
        ok_light, light_issues = _lightweight_safe_precheck(
            folder, store=store, audit=audit
        )
        if not ok_light:
            # Fall back to full audit when snapshot is stale.
            audit = audit_mod_info_assets(
                mid,
                store=store,
                managed_path=folder if folder.is_dir() else None,
                open_check=OpenCheckMode.MANIFEST if not dry_run else open_check,
                db_path=db_path,
            )
            out.issues.extend(light_issues)
        else:
            out.issues.extend(light_issues)
    else:
        # Pre-delete: manifest+CAS correspondence (OPEN materialize is post-delete).
        audit = audit_mod_info_assets(
            mid,
            store=store,
            open_check=OpenCheckMode.MANIFEST if not dry_run else open_check,
            db_path=db_path,
        )

    out.classification = audit.classification.value
    out.asset_count_before = audit.asset_files
    out.bytes_before = audit.asset_bytes
    out.manifest_sha256_before = audit.manifest_sha256

    if audit.classification == KeepReason.NO_ASSETS:
        out.ok = True
        out.skipped = True
        out.reason = "no .info/assets"
        return out

    if not audit.is_safe_delete:
        out.ok = True
        out.skipped = True
        out.reason = f"KEEP:{audit.classification.value}"
        out.issues = list(audit.issues)
        return out

    folder = Path(audit.managed_path)
    manifest_path = Path(audit.manifest_path)

    if dry_run:
        out.ok = True
        out.deleted_files = audit.safe_files
        out.deleted_bytes = audit.safe_bytes
        out.reason = "dry-run SAFE_DELETE"
        return out

    # Snapshot protected paths before delete
    protected = {
        "manifest": manifest_path.is_file(),
        "metadata": (folder / ".info" / "metadata.json").is_file()
        or (folder / "info" / "metadata.json").is_file(),
    }

    deleted, nbytes, unlink_issues = _delete_info_assets_only(
        folder,
        batch_size=batch_size,
        batch_pause_ms=batch_pause_ms,
        on_batch=on_batch,
        should_cancel=should_cancel,
    )
    out.deleted_files = deleted
    out.deleted_bytes = nbytes
    out.issues.extend(unlink_issues)

    if any("cancelled" in i for i in unlink_issues):
        out.ok = False
        out.reason = "cancelled"
        return out

    if unlink_issues and stop_on_failure:
        out.ok = False
        out.reason = "unlink issues"
        return out

    # Protected files must remain
    if protected["manifest"] and not manifest_path.is_file():
        out.ok = False
        out.reason = "CRITICAL: manifest deleted"
        return out

    try:
        out.manifest_sha256_after = _manifest_file_digest(manifest_path)
    except OSError as exc:
        out.ok = False
        out.reason = f"manifest hash after delete failed: {exc}"
        return out

    expected_count = int(audit.manifest_assets or 0) or None
    if deep_verify:
        ok, open_ok, repair_ok, verify_issues = _post_delete_verify_deep(
            folder,
            mod_id=mid,
            store=store,
            manifest_path=manifest_path,
            manifest_sha_before=out.manifest_sha256_before,
            expected_manifest_assets=expected_count,
        )
        out.open_ok = open_ok
        out.repair_ok = repair_ok
        out.miss_ok = ok
        out.light_ok = ok
    else:
        ok, verify_issues = _post_delete_verify_light(
            folder,
            store=store,
            manifest_path=manifest_path,
            manifest_sha_before=out.manifest_sha256_before,
            expected_manifest_assets=expected_count,
        )
        out.light_ok = ok
        out.open_ok = False
        out.repair_ok = False
        out.miss_ok = False
    out.issues.extend(verify_issues)

    try:
        store_after = sum(1 for _ in store.iter_objects())
        if store_before >= 0:
            out.cas_objects_deleted = max(0, store_before - store_after)
    except Exception:  # noqa: BLE001
        pass

    if out.cas_objects_deleted:
        out.ok = False
        out.reason = "CRITICAL: Asset Store objects decreased"
        return out

    if not ok:
        out.ok = False
        out.reason = "post-delete verification failed"
        return out

    out.ok = True
    out.reason = "deleted"
    return out


def select_sample_verify_ids(
    cleaned: list[ModCleanupExecuteResult],
    *,
    sample_n: int = 0,
    rng: random.Random | None = None,
) -> list[str]:
    """
    Pick Mod IDs for post-batch deep verify.

    Always includes (when present among *cleaned*):
    * Mod with the largest ``asset_count_before``
    * Mod with the largest ``bytes_before``

    Plus up to *sample_n* additional random Mods from the remainder.
    """
    eligible = [
        r
        for r in cleaned
        if r.ok and not r.skipped and not r.dry_run and str(r.mod_id).strip()
    ]
    if not eligible:
        return []

    by_id = {str(r.mod_id): r for r in eligible}
    selected: list[str] = []

    max_files = max(eligible, key=lambda r: (int(r.asset_count_before), str(r.mod_id)))
    max_bytes = max(eligible, key=lambda r: (int(r.bytes_before), str(r.mod_id)))
    for mid in (str(max_files.mod_id), str(max_bytes.mod_id)):
        if mid not in selected:
            selected.append(mid)

    remaining = [mid for mid in by_id if mid not in selected]
    n = max(0, int(sample_n))
    if n > 0 and remaining:
        picker = rng if rng is not None else random.Random()
        k = min(n, len(remaining))
        selected.extend(picker.sample(remaining, k))
    return selected


def deep_verify_cleaned_mod(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    managed_path: Path | None = None,
    db_path: Path | None = None,
    manifest_path: Path | str | None = None,
    manifest_sha256_before: str = "",
    expected_manifest_assets: int | None = None,
) -> ModCleanupExecuteResult:
    """
    Deep OPEN / MISS / Repair on a Mod whose ``.info/assets`` were already
    deleted (sample / gate). Does not delete anything or touch DB / CAS.
    """
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    out = ModCleanupExecuteResult(
        mod_id=mid, dry_run=False, verify_mode="deep", reason="sample-deep"
    )
    folder = managed_path
    if folder is None:
        folder = resolve_mod_managed_path(mid, db_path=db_path)
    if folder is None or not Path(folder).is_dir():
        out.ok = False
        out.reason = "path missing"
        out.issues.append("managed path missing for deep verify")
        return out
    folder = Path(folder)

    mp: Path | None = Path(manifest_path) if manifest_path else None
    if mp is None or not mp.is_file():
        index = resolve_offline_page(folder)
        if index is not None:
            cand = Path(index).parent / MANIFEST_FILENAME
            if cand.is_file():
                mp = cand
        if mp is None or not mp.is_file():
            for cand in (
                folder / ".info" / MANIFEST_FILENAME,
                folder / "info" / MANIFEST_FILENAME,
            ):
                if cand.is_file():
                    mp = cand
                    break
    if mp is None or not mp.is_file():
        out.ok = False
        out.reason = "manifest missing"
        out.issues.append("manifest missing for deep verify")
        return out

    before = manifest_sha256_before
    if not before:
        try:
            before = _manifest_file_digest(mp)
        except OSError as exc:
            out.ok = False
            out.reason = f"manifest hash failed: {exc}"
            return out
    out.manifest_sha256_before = before
    out.manifest_sha256_after = before

    ok, open_ok, repair_ok, issues = _post_delete_verify_deep(
        folder,
        mod_id=mid,
        store=store,
        manifest_path=mp,
        manifest_sha_before=before,
        expected_manifest_assets=expected_manifest_assets,
    )
    out.open_ok = open_ok
    out.repair_ok = repair_ok
    out.miss_ok = ok
    out.light_ok = ok
    out.issues.extend(issues)
    out.ok = ok
    if not ok:
        out.reason = "sample deep verify failed"
    return out


def load_cleanup_checkpoint(path: Path | None = None) -> dict[str, Any]:
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
        return empty
    if not isinstance(data, dict):
        return empty
    if int(data.get("schema_version") or 0) != CHECKPOINT_SCHEMA:
        return empty
    if data.get("tool") != CHECKPOINT_TOOL:
        return empty
    for key in empty:
        data.setdefault(key, empty[key])
    return data


def save_cleanup_checkpoint(
    data: dict[str, Any], path: Path | None = None
) -> Path:
    target = Path(path) if path is not None else DEFAULT_CHECKPOINT
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["schema_version"] = CHECKPOINT_SCHEMA
    payload["tool"] = CHECKPOINT_TOOL
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def cleanup_all_safe_info_assets(
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    resume: bool = False,
    checkpoint_path: Path | None = None,
    limit: int = 0,
    mod_ids: list[str] | None = None,
    open_check: OpenCheckMode = OpenCheckMode.MANIFEST,
    db_path: Path | None = None,
    stop_on_failure: bool = True,
    trust_audit: bool = False,
    audit_by_mod: dict[str, ModInfoAssetAudit] | None = None,
    batch_size: int = DEFAULT_DELETE_BATCH_SIZE,
    batch_pause_ms: int = DEFAULT_DELETE_BATCH_PAUSE_MS,
    checkpoint_every_files: int = DEFAULT_CHECKPOINT_EVERY_FILES,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    deep_verify: bool = False,
    sample_verify: int = -1,
    sample_rng: random.Random | None = None,
) -> BatchCleanupResult:
    """Batch cleanup of SAFE_DELETE Mods.

    Default post-delete verification is *lightweight* (no offline_view).
    Pass *deep_verify=True* to materialize OPEN on every Mod (slow; gate only).
    Pass *sample_verify=N* (*N* >= 0) to deep-verify N random cleaned Mods
    plus the largest-by-files and largest-by-bytes Mods after the batch.
    *sample_verify=-1* (default) skips post-batch sampling.

    Checkpoint strategy: after each Mod **and** every *checkpoint_every_files*
    deleted files (batch delete → checkpoint), never per-file.
    """
    store = store or AssetStore(root=asset_store_dir())
    batch = BatchCleanupResult(
        dry_run=dry_run,
        verify_mode="deep" if deep_verify else "light",
    )
    cp = load_cleanup_checkpoint(checkpoint_path) if resume else {
        "schema_version": CHECKPOINT_SCHEMA,
        "tool": CHECKPOINT_TOOL,
        "last_processed_mod_id": "",
        "cleaned_mod_ids": [],
        "skipped_mod_ids": [],
        "failed_mod_ids": [],
        "files_deleted": 0,
        "bytes_reclaimed": 0,
        "checkpoint_writes": 0,
        "updated_at": "",
    }
    done = set(str(x) for x in (cp.get("cleaned_mod_ids") or []))
    skipped_set = set(str(x) for x in (cp.get("skipped_mod_ids") or []))
    files_since_ckpt = 0
    ckpt_writes = int(cp.get("checkpoint_writes") or 0)
    last_batch_deleted = 0

    def _save_cp() -> None:
        nonlocal ckpt_writes
        ckpt_writes += 1
        cp["checkpoint_writes"] = ckpt_writes
        save_cleanup_checkpoint(cp, checkpoint_path)

    if mod_ids:
        ids = [str(m).strip() for m in mod_ids]
    else:
        # Prefer audit-driven SAFE list when available
        audit = audit_all_info_assets(
            store=store,
            open_check=OpenCheckMode.MANIFEST,
            limit=limit,
            db_path=db_path,
        )
        ids = [m.mod_id for m in audit.mods if m.is_safe_delete]
        if audit_by_mod is None:
            audit_by_mod = {m.mod_id: m for m in audit.mods if m.is_safe_delete}

    if limit > 0:
        ids = ids[: int(limit)]

    for mid in ids:
        if should_cancel is not None and should_cancel():
            batch.ok = False
            batch.stopped_on = mid
            _save_cp()
            return batch
        if mid in done or mid in skipped_set:
            continue
        batch.mods_attempted += 1
        snap = (audit_by_mod or {}).get(mid)
        last_batch_deleted = 0

        def _on_batch(deleted: int, nbytes: int, _mid: str = mid) -> None:
            nonlocal files_since_ckpt, last_batch_deleted
            delta = max(0, int(deleted) - int(last_batch_deleted))
            last_batch_deleted = int(deleted)
            files_since_ckpt += delta
            if on_progress is not None:
                try:
                    on_progress(
                        {
                            "phase": "delete",
                            "mod_id": _mid,
                            "deleted_files": deleted,
                            "deleted_bytes": nbytes,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Mid-mod batch checkpoint by file count (not per file).
            if files_since_ckpt >= max(1, int(checkpoint_every_files)):
                cp["last_processed_mod_id"] = _mid
                _save_cp()
                files_since_ckpt = 0

        result = cleanup_mod_info_assets(
            mid,
            store=store,
            dry_run=dry_run,
            open_check=open_check,
            db_path=db_path,
            stop_on_failure=stop_on_failure,
            trust_audit=bool(trust_audit and snap is not None),
            audit_snapshot=snap,
            batch_size=batch_size,
            batch_pause_ms=batch_pause_ms,
            on_batch=_on_batch,
            should_cancel=should_cancel,
            deep_verify=deep_verify,
        )
        batch.results.append(result)
        cp["last_processed_mod_id"] = mid

        if result.skipped:
            batch.mods_skipped += 1
            skipped_set.add(mid)
            cp["skipped_mod_ids"] = sorted(skipped_set)
        elif result.ok:
            batch.mods_cleaned += 1
            done.add(mid)
            cp["cleaned_mod_ids"] = sorted(done)
            batch.files_deleted += result.deleted_files
            batch.bytes_reclaimed += result.deleted_bytes
            cp["files_deleted"] = int(cp.get("files_deleted") or 0) + (
                0 if dry_run else result.deleted_files
            )
            cp["bytes_reclaimed"] = int(cp.get("bytes_reclaimed") or 0) + (
                0 if dry_run else result.deleted_bytes
            )
            files_since_ckpt += int(result.deleted_files or 0)
        else:
            batch.mods_failed += 1
            failed = list(cp.get("failed_mod_ids") or [])
            failed.append(mid)
            cp["failed_mod_ids"] = failed
            batch.stopped_on = mid
            _save_cp()
            batch.ok = False
            if stop_on_failure:
                return batch

        if files_since_ckpt >= max(1, int(checkpoint_every_files)):
            _save_cp()
            files_since_ckpt = 0
        else:
            # Still checkpoint at Mod boundary for resume granularity.
            _save_cp()
            files_since_ckpt = 0

    # Optional post-batch sample deep verify (does not rewrite cleaned checkpoints).
    # sample_verify < 0 → disabled (default). >= 0 → fixed extremes + N random.
    want_sample = (
        (not dry_run)
        and (not deep_verify)
        and int(sample_verify) >= 0
        and batch.mods_cleaned > 0
        and batch.mods_failed == 0
    )
    if want_sample:
        cleaned_ok = [
            r
            for r in batch.results
            if r.ok and not r.skipped and not r.dry_run
        ]
        sample_ids = select_sample_verify_ids(
            cleaned_ok,
            sample_n=int(sample_verify),
            rng=sample_rng,
        )
        batch.sample_verify_mod_ids = list(sample_ids)
        for smid in sample_ids:
            snap = (audit_by_mod or {}).get(smid)
            prior = next((r for r in cleaned_ok if r.mod_id == smid), None)
            folder = (
                Path(snap.managed_path)
                if snap is not None and snap.managed_path
                else resolve_mod_managed_path(smid, db_path=db_path)
            )
            mp = (
                Path(snap.manifest_path)
                if snap is not None and snap.manifest_path
                else None
            )
            expected = None
            if snap is not None and snap.manifest_assets:
                expected = int(snap.manifest_assets)
            sha_before = (
                (prior.manifest_sha256_before if prior else "")
                or (snap.manifest_sha256 if snap else "")
            )
            sv = deep_verify_cleaned_mod(
                smid,
                store=store,
                managed_path=folder,
                db_path=db_path,
                manifest_path=mp,
                manifest_sha256_before=sha_before,
                expected_manifest_assets=expected,
            )
            batch.sample_verify_results.append(sv)
            if not sv.ok:
                batch.ok = False
                batch.stopped_on = f"sample:{smid}"
                batch.mods_failed += 1
                if stop_on_failure:
                    break

    batch.ok = batch.mods_failed == 0
    return batch


def load_audit_snapshots(path: Path | str) -> dict[str, ModInfoAssetAudit]:
    """Rebuild lightweight SAFE_DELETE snapshots from an audit JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, ModInfoAssetAudit] = {}
    for m in raw.get("mods") or []:
        if str(m.get("classification") or "") != KeepReason.SAFE_DELETE.value:
            continue
        mid = str(m.get("mod_id") or "").strip()
        if not mid:
            continue
        snap = ModInfoAssetAudit(
            mod_id=mid,
            managed_path=str(m.get("managed_path") or ""),
            classification=KeepReason.SAFE_DELETE,
            offline_root=str(m.get("offline_root") or ""),
            manifest_path=str(m.get("manifest_path") or ""),
            manifest_sha256=str(m.get("manifest_sha256") or ""),
            manifest_assets=int(m.get("manifest_assets") or 0),
            asset_files=int(m.get("asset_files") or 0),
            asset_bytes=int(m.get("asset_bytes") or 0),
            safe_files=int(m.get("safe_files") or m.get("asset_files") or 0),
            safe_bytes=int(m.get("safe_bytes") or m.get("asset_bytes") or 0),
            open_ok=bool(m.get("open_ok", True)),
        )
        out[mid] = snap
    return out
