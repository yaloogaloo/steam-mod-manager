"""Phase 3: Backup Offline → Durable Asset Store reference migration.

Dual-write model (production)::

    LIVE .info/manifest.json  (Phase 2, preferred)
            ↓ reuse same SHA-256 objects
    Backup offline/manifest.json
            ↓
    Asset Store

Legacy ``Backup/offline/assets/`` byte copies remain until a later cleanup Phase.

Restore / Repair can materialize files from the Store using the Backup
manifest — proven in tests by deleting isolated Backup assets (never production).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.paths import asset_store_dir, database_path
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    AssetReference,
    ManifestError,
    build_manifest,
    compare_manifests,
    materialize_manifest_to_dir,
    validate_manifest_path,
    verify_manifest_against_store,
    write_manifest_atomic,
)
from services.asset_store import (
    AssetStore,
    AssetStoreError,
    sha256_file,
)
from services.info_asset_migration import (
    iter_asset_files,
    relative_manifest_path,
    resolve_mod_managed_path,
)
from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)


class BackupAssetMigrationError(Exception):
    """Backup Asset Store migration / restore failure."""


@dataclass
class BackupAssetMigrationResult:
    mod_id: str = ""
    backup_offline: str = ""
    ok: bool = False
    dry_run: bool = False
    skipped: bool = False
    reason: str = ""
    source_files: int = 0
    manifest_assets: int = 0
    unique_objects: int = 0
    created_objects: int = 0
    reused_objects: int = 0
    source_bytes: int = 0
    cas_bytes: int = 0
    reused_info_manifest: bool = False
    manifest_path: str = ""
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "backup_offline": self.backup_offline,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "reason": self.reason,
            "source_files": self.source_files,
            "manifest_assets": self.manifest_assets,
            "unique_objects": self.unique_objects,
            "created_objects": self.created_objects,
            "reused_objects": self.reused_objects,
            "source_bytes": self.source_bytes,
            "cas_bytes": self.cas_bytes,
            "reused_info_manifest": self.reused_info_manifest,
            "manifest_path": self.manifest_path,
            "issues": list(self.issues),
        }


@dataclass
class RestoreAssetsResult:
    ok: bool = False
    reason: str = ""
    written: int = 0
    skipped: int = 0
    bytes_written: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "written": self.written,
            "skipped": self.skipped,
            "bytes_written": self.bytes_written,
            "issues": list(self.issues),
        }


@dataclass
class BatchBackupMigrationResult:
    ok: bool = True
    dry_run: bool = False
    mods_attempted: int = 0
    mods_succeeded: int = 0
    mods_failed: int = 0
    mods_skipped: int = 0
    results: list[BackupAssetMigrationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_attempted": self.mods_attempted,
            "mods_succeeded": self.mods_succeeded,
            "mods_failed": self.mods_failed,
            "mods_skipped": self.mods_skipped,
            "results": [r.to_dict() for r in self.results],
        }


def live_manifest_path_for_index(source_index: Path) -> Path:
    """``.info/manifest.json`` or ``.info/offline/manifest.json`` beside the index."""
    return Path(source_index).parent / MANIFEST_FILENAME


def load_live_manifest(source_index: Path | None) -> AssetManifest | None:
    if source_index is None or not Path(source_index).is_file():
        return None
    path = live_manifest_path_for_index(source_index)
    if not path.is_file():
        return None
    try:
        return AssetManifest.from_path(path)
    except (OSError, ManifestError):
        return None


def backup_offline_manifest_path(dest_offline: Path) -> Path:
    return Path(dest_offline) / MANIFEST_FILENAME


def _iter_backup_asset_files(dest_offline: Path) -> list[Path]:
    assets = Path(dest_offline) / "assets"
    if not assets.is_dir():
        return []
    return list(iter_asset_files(assets))


def build_backup_manifest_from_assets(
    dest_offline: Path,
    *,
    store: AssetStore,
    live_manifest: AssetManifest | None = None,
    dry_run: bool = False,
) -> tuple[AssetManifest, BackupAssetMigrationResult]:
    """
    Hash Backup ``offline/assets``, put into Store, build manifest.

    Prefer LIVE manifest SHA-256 when path matches; mismatch → hard fail.
    """
    result = BackupAssetMigrationResult(
        backup_offline=str(dest_offline),
        dry_run=dry_run,
        reused_info_manifest=live_manifest is not None,
    )
    live_by_path = {a.path: a for a in (live_manifest.assets if live_manifest else [])}
    files = _iter_backup_asset_files(dest_offline)
    result.source_files = len(files)
    if not files:
        result.skipped = True
        result.ok = True
        result.reason = "no backup offline/assets"
        return AssetManifest(), result

    refs: list[AssetReference] = []
    digest_sizes: dict[str, int] = {}
    seen: set[str] = set()
    issues: list[str] = []

    assets_dir = Path(dest_offline) / "assets"
    for src in files:
        try:
            size = int(src.stat().st_size)
        except OSError as exc:
            issues.append(f"unreadable {src}: {exc}")
            continue
        if size <= 0:
            issues.append(f"empty skipped: {src}")
            continue
        try:
            rel = relative_manifest_path(assets_dir, src)
        except ManifestError as exc:
            issues.append(f"unsafe path {src}: {exc}")
            continue
        try:
            digest = sha256_file(src)
        except OSError as exc:
            issues.append(f"hash failed {src}: {exc}")
            continue

        live_ref = live_by_path.get(rel)
        if live_ref is not None:
            if live_ref.sha256 != digest:
                issues.append(
                    f"manifest mismatch {rel}: "
                    f"info={live_ref.sha256} backup_file={digest}"
                )
                continue
            if int(live_ref.size) != size:
                issues.append(
                    f"manifest size mismatch {rel}: "
                    f"info={live_ref.size} backup_file={size}"
                )
                continue

        result.source_bytes += size
        created = False
        if dry_run:
            already = digest in seen or store.has(digest)
            created = not already
        else:
            try:
                obj = store.put_file(src, expected_sha256=digest)
                created = bool(obj.created)
                store.verify(digest)
            except AssetStoreError as exc:
                issues.append(f"{rel}: put failed: {exc}")
                continue

        seen.add(digest)
        refs.append(AssetReference(path=rel, sha256=digest, size=size))
        digest_sizes[digest] = size
        if created:
            result.created_objects += 1
        else:
            result.reused_objects += 1

    result.issues = issues
    if issues:
        result.ok = False
        result.reason = f"{len(issues)} issue(s)"
        return AssetManifest(), result

    if not refs:
        result.ok = False
        result.reason = "no migratable backup assets"
        return AssetManifest(), result

    # If LIVE manifest exists, shared paths must already match (checked above).
    # Extra LIVE-only paths are OK (Phase 2 may include unreferenced assets).
    if live_manifest is not None:
        built = build_manifest(refs)
        mismatch = compare_manifests(built, live_manifest)
        if mismatch:
            result.ok = False
            result.reason = "info/backup manifest mismatch"
            result.issues.extend(mismatch)
            return AssetManifest(), result

    manifest = build_manifest(refs)
    result.manifest_assets = len(manifest.assets)
    result.unique_objects = len(digest_sizes)
    result.cas_bytes = sum(digest_sizes.values())
    result.ok = True
    result.reason = "built"
    return manifest, result


def sync_backup_offline_manifest(
    dest_offline: Path,
    *,
    source_index: Path | None = None,
    store: AssetStore | None = None,
    dry_run: bool = False,
    mod_id: str = "",
) -> BackupAssetMigrationResult:
    """
    Ensure ``Backup/offline/manifest.json`` exists and Store objects verify.

    Prefer Phase 2 LIVE ``.info`` manifest. Never deletes ``offline/assets``.
    """
    dest = Path(dest_offline)
    store = store or AssetStore(root=asset_store_dir())
    result = BackupAssetMigrationResult(
        mod_id=str(mod_id or ""),
        backup_offline=str(dest),
        dry_run=dry_run,
        manifest_path=str(backup_offline_manifest_path(dest)),
    )
    if not (dest / "index.html").is_file():
        result.skipped = True
        result.ok = True
        result.reason = "no backup offline index"
        return result

    live_man = load_live_manifest(source_index)
    try:
        manifest, built = build_backup_manifest_from_assets(
            dest,
            store=store,
            live_manifest=live_man,
            dry_run=dry_run,
        )
    except ManifestError as exc:
        result.ok = False
        result.reason = str(exc)
        result.issues.append(str(exc))
        return result

    # Merge stats
    for key in (
        "source_files",
        "manifest_assets",
        "unique_objects",
        "created_objects",
        "reused_objects",
        "source_bytes",
        "cas_bytes",
        "reused_info_manifest",
        "skipped",
        "ok",
        "reason",
        "issues",
    ):
        setattr(result, key, getattr(built, key))

    if not built.ok or built.skipped:
        return result

    if dry_run:
        result.reason = (
            f"dry-run ok; would write {result.manifest_assets} refs "
            f"({result.created_objects} new objects)"
        )
        return result

    store_issues = verify_manifest_against_store(manifest, store)
    if store_issues:
        result.ok = False
        result.reason = "store verification failed"
        result.issues.extend(store_issues)
        return result

    try:
        write_manifest_atomic(backup_offline_manifest_path(dest), manifest)
    except OSError as exc:
        result.ok = False
        result.reason = f"manifest write failed: {exc}"
        result.issues.append(str(exc))
        return result

    try:
        loaded = AssetManifest.from_path(backup_offline_manifest_path(dest))
        reload_issues = verify_manifest_against_store(loaded, store)
    except (OSError, ManifestError) as exc:
        result.ok = False
        result.reason = f"manifest reload failed: {exc}"
        return result
    if reload_issues:
        result.ok = False
        result.reason = "manifest reload verify failed"
        result.issues = reload_issues
        return result

    result.ok = True
    result.reason = "migrated"
    return result


def sync_backup_offline_manifest_after_snapshot(
    dest_offline: Path,
    *,
    source_index: Path | None = None,
) -> BackupAssetMigrationResult:
    """
    Legacy Phase 3 hook — Phase 5 snapshot already writes CAS-only manifest.

    Kept as a no-op verifier for callers that still invoke it.
    """
    try:
        dest = Path(dest_offline)
        man = backup_offline_manifest_path(dest)
        if not man.is_file():
            return BackupAssetMigrationResult(
                backup_offline=str(dest),
                ok=False,
                reason="manifest missing after Phase 5 snapshot",
            )
        from core.paths import asset_store_dir
        from services.asset_manifest import AssetManifest, verify_manifest_against_store
        from services.asset_store import AssetStore

        store = AssetStore(root=asset_store_dir())
        loaded = AssetManifest.from_path(man)
        issues = verify_manifest_against_store(loaded, store)
        if issues:
            return BackupAssetMigrationResult(
                backup_offline=str(dest),
                ok=False,
                reason="store verification failed",
                issues=issues,
            )
        return BackupAssetMigrationResult(
            backup_offline=str(dest),
            ok=True,
            reason="phase5 verify-only",
            manifest_assets=len(loaded.assets),
            manifest_path=str(man),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync_backup_offline_manifest verify failed: %s", exc, exc_info=True)
        return BackupAssetMigrationResult(
            backup_offline=str(dest_offline),
            ok=False,
            reason=str(exc),
        )


def _refuse_durable_asset_dest(dest: Path) -> str | None:
    """Block materialize into LIVE ``.info`` (durable leftover tree)."""
    try:
        parts = Path(dest).resolve().parts
    except OSError:
        parts = Path(dest).parts
    if ".info" in parts:
        return "refusing materialize into LIVE .info"
    return None


def restore_backup_assets_from_store(
    dest_offline: Path,
    *,
    store: AssetStore | None = None,
    only_missing: bool = False,
) -> RestoreAssetsResult:
    """
    Materialize Backup manifest objects into *dest_offline* from Asset Store.

    Production OPEN passes ``cache/offline_view``. Refuses durable LIVE
    ``.info``. This is Store → view materialize, not a Backup ``offline/assets``
    OPEN fallback.
    """
    dest = Path(dest_offline)
    out = RestoreAssetsResult()
    refuse = _refuse_durable_asset_dest(dest)
    if refuse:
        out.reason = refuse
        return out
    store = store or AssetStore(root=asset_store_dir())
    man_path = backup_offline_manifest_path(dest)
    if not man_path.is_file():
        out.reason = "backup offline/manifest.json missing"
        return out
    try:
        manifest = AssetManifest.from_path(man_path)
    except (OSError, ManifestError) as exc:
        out.reason = f"manifest unreadable: {exc}"
        out.issues.append(str(exc))
        return out

    issues = verify_manifest_against_store(manifest, store)
    if issues:
        out.reason = "CAS verification failed"
        out.issues = issues
        return out

    try:
        stats = materialize_manifest_to_dir(
            manifest, store, dest, only_missing=only_missing
        )
    except ManifestError as exc:
        out.reason = str(exc)
        out.issues.append(str(exc))
        return out

    out.ok = True
    out.written = int(stats["written"])
    out.skipped = int(stats["skipped"])
    out.bytes_written = int(stats["bytes"])
    out.reason = "restored"
    return out


def repair_info_assets_from_backup_store(
    managed_path: Path | str,
    *,
    mod_id: str | int = "",
    store: AssetStore | None = None,
) -> RestoreAssetsResult:
    """Alias of :func:`services.info_asset_runtime.repair_live_from_cas`.

    Never materializes into LIVE ``.info/assets``. OPEN uses
    ``cache/offline_view``.
    """
    from services.info_asset_runtime import repair_live_from_cas

    return repair_live_from_cas(managed_path, mod_id=mod_id, store=store)


def migrate_backup_offline_for_mod_id(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> BackupAssetMigrationResult:
    """Migrate one Mod's Backup offline assets → Store + Backup manifest."""
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    dest = backup_root(mid) / BACKUP_OFFLINE_DIR
    folder = resolve_mod_managed_path(mid, db_path=db_path)
    source_index = resolve_offline_page(folder) if folder is not None else None
    # If LIVE missing, still migrate from Backup assets alone
    return sync_backup_offline_manifest(
        dest,
        source_index=source_index,
        store=store,
        dry_run=dry_run,
        mod_id=mid,
    )


def migrate_all_backup_offline_assets(
    *,
    store: AssetStore | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
) -> BatchBackupMigrationResult:
    store = store or AssetStore(root=asset_store_dir())
    batch = BatchBackupMigrationResult(dry_run=dry_run, ok=True)

    if mod_ids is not None:
        ids = [str(m).strip() for m in mod_ids]
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
            ids = []

    if limit is not None and limit > 0:
        ids = ids[:limit]

    for mid in ids:
        batch.mods_attempted += 1
        try:
            result = migrate_backup_offline_for_mod_id(
                mid, store=store, dry_run=dry_run, db_path=db_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("backup asset migration crashed mod_id=%s", mid)
            result = BackupAssetMigrationResult(
                mod_id=mid, ok=False, dry_run=dry_run, reason=f"crash: {exc}"
            )
        batch.results.append(result)
        if result.skipped and result.ok:
            batch.mods_skipped += 1
        elif result.ok:
            batch.mods_succeeded += 1
        else:
            batch.mods_failed += 1
            batch.ok = False
    return batch


def strip_backup_offline_assets_for_test(dest_offline: Path) -> int:
    """
    TEST-ONLY: remove ``offline/assets`` tree while keeping index + manifest.

    Never call on production Backup paths from tools without an explicit
    isolated test directory.
    """
    assets = Path(dest_offline) / "assets"
    if not assets.is_dir():
        return 0
    removed = sum(1 for _ in assets.rglob("*") if _.is_file())
    shutil.rmtree(assets)
    return removed
