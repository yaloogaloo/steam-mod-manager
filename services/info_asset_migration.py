"""Phase 2: migrate ``.info/assets`` into Durable Asset Store + Manifest.

Pipeline (per Mod / offline root)::

    .info/assets/<file>
            ↓
    SHA-256(content)
            ↓
    AssetStore.put_file(...)
            ↓
    .info/manifest.json   (or .info/offline/manifest.json)

Safety invariants (Phase 2)::

* Never deletes ``.info/assets``
* Never touches Backup / MISS / asset_cache / DB / Identity
* Does not change Offline Snapshot OPEN semantics (assets stay on disk)
* Migration is idempotent
* Manifest is written atomically; failure leaves previous manifest (if any)
  and always leaves ``.info/assets`` intact

Asset Store remains Mod-agnostic. This module owns Mod path discovery and
manifest placement only.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.paths import asset_store_dir, database_path
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    AssetReference,
    ManifestError,
    build_manifest,
    validate_manifest_path,
    verify_manifest_against_store,
    write_manifest_atomic,
)
from services.asset_store import (
    AssetCorruption,
    AssetStore,
    AssetStoreError,
    sha256_file,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InfoAssetTree:
    """One offline root that owns an ``assets/`` directory + optional index."""

    offline_root: Path
    """Directory containing ``index.html`` (when present) and ``assets/``."""

    assets_dir: Path
    manifest_path: Path


@dataclass
class AssetMigrateItem:
    relative_path: str
    source_path: Path
    size: int
    sha256: str
    created_object: bool
    dry_run: bool = False


@dataclass
class InfoAssetMigrationResult:
    mod_id: str = ""
    managed_path: str = ""
    offline_root: str = ""
    ok: bool = False
    dry_run: bool = False
    skipped: bool = False
    reason: str = ""
    source_files: int = 0
    migrated_files: int = 0
    unique_objects: int = 0
    reused_objects: int = 0
    created_objects: int = 0
    source_bytes: int = 0
    cas_bytes: int = 0
    deduplicated_files: int = 0
    manifest_path: str = ""
    issues: list[str] = field(default_factory=list)
    items: list[AssetMigrateItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "managed_path": self.managed_path,
            "offline_root": self.offline_root,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "reason": self.reason,
            "source_files": self.source_files,
            "migrated_files": self.migrated_files,
            "unique_objects": self.unique_objects,
            "reused_objects": self.reused_objects,
            "created_objects": self.created_objects,
            "source_bytes": self.source_bytes,
            "cas_bytes": self.cas_bytes,
            "deduplicated_files": self.deduplicated_files,
            "manifest_path": self.manifest_path,
            "issues": list(self.issues),
        }


@dataclass
class BatchMigrationResult:
    ok: bool = True
    dry_run: bool = False
    mods_attempted: int = 0
    mods_succeeded: int = 0
    mods_failed: int = 0
    mods_skipped: int = 0
    source_files: int = 0
    unique_objects_seen: int = 0
    created_objects: int = 0
    reused_objects: int = 0
    source_bytes: int = 0
    results: list[InfoAssetMigrationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_attempted": self.mods_attempted,
            "mods_succeeded": self.mods_succeeded,
            "mods_failed": self.mods_failed,
            "mods_skipped": self.mods_skipped,
            "source_files": self.source_files,
            "unique_objects_seen": self.unique_objects_seen,
            "created_objects": self.created_objects,
            "reused_objects": self.reused_objects,
            "source_bytes": self.source_bytes,
            "results": [r.to_dict() for r in self.results],
        }


def discover_info_asset_trees(managed_path: Path | str) -> list[InfoAssetTree]:
    """
    Discover ``.info/assets`` and ``.info/offline/assets`` trees.

    Manifest is placed beside the assets directory's parent (offline root),
    matching Offline HTML relative paths ``assets/...``.
    """
    root = Path(managed_path)
    trees: list[InfoAssetTree] = []
    seen: set[str] = set()
    for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
        info = root / info_name
        if not info.is_dir():
            continue
        candidates = (
            info / "offline" / "assets",
            info / "assets",
        )
        for assets_dir in candidates:
            if not assets_dir.is_dir():
                continue
            key = str(assets_dir.resolve())
            if key in seen:
                continue
            seen.add(key)
            offline_root = assets_dir.parent
            trees.append(
                InfoAssetTree(
                    offline_root=offline_root,
                    assets_dir=assets_dir,
                    manifest_path=offline_root / MANIFEST_FILENAME,
                )
            )
    return trees


def iter_asset_files(assets_dir: Path) -> Iterator[Path]:
    if not assets_dir.is_dir():
        return
    for path in sorted(assets_dir.rglob("*")):
        try:
            if path.is_file():
                yield path
        except OSError:
            continue


def relative_manifest_path(assets_dir: Path, file_path: Path) -> str:
    """Return ``assets/<rel>`` suitable for Offline HTML references."""
    rel = file_path.relative_to(assets_dir).as_posix()
    return validate_manifest_path(f"assets/{rel}")


def resolve_mod_managed_path(
    mod_id: str | int,
    *,
    db_path: Path | None = None,
) -> Path | None:
    """Resolve LIVE managed folder from readonly DB ``last_known_path``."""
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return None
    path = Path(db_path) if db_path is not None else database_path()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT last_known_path FROM mods WHERE mod_id = ?",
            (int(mid),),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    folder = Path(str(row[0] or "").strip())
    if folder.is_dir():
        return folder
    return None


def list_mod_ids_with_paths(*, db_path: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(mod_id, last_known_path)`` for Mods with an existing folder."""
    path = Path(db_path) if db_path is not None else database_path()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    out: list[tuple[str, Path]] = []
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute(
            "SELECT mod_id, last_known_path FROM mods "
            "WHERE last_known_path IS NOT NULL AND TRIM(last_known_path) != '' "
            "ORDER BY mod_id"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for mod_id, lkp in rows:
        folder = Path(str(lkp or "").strip())
        if folder.is_dir():
            out.append((str(mod_id), folder))
    return out


def migrate_info_asset_tree(
    tree: InfoAssetTree,
    *,
    store: AssetStore,
    dry_run: bool = False,
    mod_id: str = "",
    managed_path: Path | None = None,
) -> InfoAssetMigrationResult:
    """
    Import one ``assets/`` tree into the store and write/validate its manifest.

    Never deletes source files under ``.info/assets``.
    """
    result = InfoAssetMigrationResult(
        mod_id=str(mod_id or ""),
        managed_path=str(managed_path or ""),
        offline_root=str(tree.offline_root),
        dry_run=dry_run,
        manifest_path=str(tree.manifest_path),
    )
    if not tree.assets_dir.is_dir():
        result.skipped = True
        result.reason = "assets dir missing"
        result.ok = True
        return result

    files = list(iter_asset_files(tree.assets_dir))
    result.source_files = len(files)
    if not files:
        result.skipped = True
        result.reason = "no asset files"
        result.ok = True
        # Still write empty manifest for consistency? Prefer skip — no empty wipe.
        return result

    refs: list[AssetReference] = []
    digest_sizes: dict[str, int] = {}
    seen_this_run: set[str] = set()
    issues: list[str] = []

    for src in files:
        try:
            size = int(src.stat().st_size)
        except OSError as exc:
            issues.append(f"unreadable {src}: {exc}")
            continue
        if size <= 0:
            issues.append(f"empty file skipped: {src}")
            continue
        try:
            rel = relative_manifest_path(tree.assets_dir, src)
        except ManifestError as exc:
            issues.append(f"unsafe path {src}: {exc}")
            continue
        try:
            digest = sha256_file(src)
        except OSError as exc:
            issues.append(f"hash failed {src}: {exc}")
            continue

        result.source_bytes += size
        created = False
        if dry_run:
            already = digest in seen_this_run or store.has(digest)
            if already and store.has(digest):
                try:
                    obj = store.verify(digest)
                    if obj.size != size:
                        issues.append(
                            f"{rel}: dry-run size mismatch store={obj.size} src={size}"
                        )
                        continue
                except AssetStoreError as exc:
                    issues.append(f"{rel}: dry-run verify failed: {exc}")
                    continue
            elif already:
                created = False
            else:
                created = True  # would create
        else:
            try:
                obj = store.put_file(src, expected_sha256=digest)
                created = bool(obj.created)
                # Re-verify after put
                verified = store.verify(digest)
                if verified.size != size or verified.sha256 != digest:
                    issues.append(f"{rel}: post-put verify failed")
                    continue
            except AssetStoreError as exc:
                issues.append(f"{rel}: put failed: {exc}")
                continue

        seen_this_run.add(digest)
        refs.append(AssetReference(path=rel, sha256=digest, size=size))
        digest_sizes[digest] = size
        result.items.append(
            AssetMigrateItem(
                relative_path=rel,
                source_path=src,
                size=size,
                sha256=digest,
                created_object=created,
                dry_run=dry_run,
            )
        )
        if created:
            result.created_objects += 1
        else:
            result.reused_objects += 1

    result.migrated_files = len(refs)
    result.unique_objects = len(digest_sizes)
    result.cas_bytes = sum(digest_sizes.values())
    result.deduplicated_files = max(0, result.migrated_files - result.unique_objects)

    if issues:
        result.issues = issues
        result.ok = False
        result.reason = f"{len(issues)} asset issue(s); manifest not written"
        return result

    if len(refs) != result.source_files:
        # Some files skipped as empty — treat as partial failure unless all
        # non-empty files migrated. Count only successful refs.
        pass

    if not refs:
        result.ok = False
        result.reason = "no migratable assets"
        result.issues = issues or ["no migratable assets"]
        return result

    try:
        manifest = build_manifest(refs)
    except ManifestError as exc:
        result.ok = False
        result.reason = f"manifest build failed: {exc}"
        result.issues.append(str(exc))
        return result

    # Validate against store (dry-run may skip missing objects that would be created)
    if dry_run:
        missing = [r for r in manifest.assets if not store.has(r.sha256)]
        # Missing is OK in dry-run (would be created); verify present ones.
        for ref in manifest.assets:
            if not store.has(ref.sha256):
                continue
            try:
                obj = store.verify(ref.sha256)
                if obj.size != ref.size:
                    issues.append(
                        f"{ref.path}: size mismatch manifest={ref.size} store={obj.size}"
                    )
            except AssetStoreError as exc:
                issues.append(f"{ref.path}: {exc}")
        if issues:
            result.issues = issues
            result.ok = False
            result.reason = "dry-run verification failed"
            return result
        result.ok = True
        result.reason = (
            f"dry-run ok; would write {len(refs)} refs "
            f"({result.created_objects} new objects)"
        )
        return result

    store_issues = verify_manifest_against_store(manifest, store)
    if store_issues:
        result.issues = store_issues
        result.ok = False
        result.reason = "store verification failed; manifest not written"
        return result

    try:
        write_manifest_atomic(tree.manifest_path, manifest)
    except OSError as exc:
        result.ok = False
        result.reason = f"manifest write failed: {exc}"
        result.issues.append(str(exc))
        return result

    # Re-read and validate manifest on disk
    try:
        loaded = AssetManifest.from_path(tree.manifest_path)
        reload_issues = verify_manifest_against_store(loaded, store)
    except (OSError, ManifestError) as exc:
        result.ok = False
        result.reason = f"manifest reload failed: {exc}"
        result.issues.append(str(exc))
        return result
    if reload_issues:
        result.ok = False
        result.reason = "manifest reload verification failed"
        result.issues = reload_issues
        return result

    result.ok = True
    result.reason = "migrated"
    return result


def migrate_info_assets(
    managed_path: Path | str,
    *,
    store: AssetStore | None = None,
    dry_run: bool = False,
    mod_id: str | int = "",
) -> InfoAssetMigrationResult:
    """
    Migrate all discovered ``.info`` asset trees for one Mod folder.

    If multiple trees exist (legacy + offline/), each gets its own manifest.
    Aggregate stats are returned; overall ``ok`` requires all trees to succeed
    (or skip cleanly).
    """
    folder = Path(managed_path)
    mid = str(mod_id or "").strip()
    store = store or AssetStore(root=asset_store_dir())
    trees = discover_info_asset_trees(folder)
    if not trees:
        return InfoAssetMigrationResult(
            mod_id=mid,
            managed_path=str(folder),
            ok=True,
            skipped=True,
            dry_run=dry_run,
            reason="no .info/assets trees",
        )

    combined = InfoAssetMigrationResult(
        mod_id=mid,
        managed_path=str(folder),
        dry_run=dry_run,
        ok=True,
    )
    digests: set[str] = set()
    for tree in trees:
        one = migrate_info_asset_tree(
            tree,
            store=store,
            dry_run=dry_run,
            mod_id=mid,
            managed_path=folder,
        )
        combined.source_files += one.source_files
        combined.migrated_files += one.migrated_files
        combined.created_objects += one.created_objects
        combined.reused_objects += one.reused_objects
        combined.source_bytes += one.source_bytes
        combined.items.extend(one.items)
        combined.issues.extend(one.issues)
        for item in one.items:
            digests.add(item.sha256)
        if one.manifest_path:
            combined.manifest_path = one.manifest_path
            combined.offline_root = one.offline_root
        if not one.ok:
            combined.ok = False
            combined.reason = one.reason or "tree failed"
        elif one.skipped and not combined.reason:
            combined.skipped = True
            combined.reason = one.reason
        elif one.ok and not one.skipped:
            combined.skipped = False
            if combined.ok:
                combined.reason = one.reason
    combined.unique_objects = len(digests)
    combined.cas_bytes = 0
    # Unique bytes among migrated items
    size_by_digest = {i.sha256: i.size for i in combined.items}
    combined.cas_bytes = sum(size_by_digest.values())
    combined.deduplicated_files = max(
        0, combined.migrated_files - combined.unique_objects
    )
    if combined.ok and combined.migrated_files == 0 and combined.skipped:
        pass
    elif combined.ok and not combined.reason:
        combined.reason = "migrated"
    return combined


def migrate_info_assets_for_mod_id(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> InfoAssetMigrationResult:
    """Migrate by SQLite ``mod_id`` using readonly ``last_known_path``."""
    mid = str(mod_id).strip()
    folder = resolve_mod_managed_path(mid, db_path=db_path)
    if folder is None:
        return InfoAssetMigrationResult(
            mod_id=mid,
            ok=False,
            dry_run=dry_run,
            reason="managed path not found",
        )
    return migrate_info_assets(
        folder, store=store, dry_run=dry_run, mod_id=mid
    )


def migrate_all_info_assets(
    *,
    store: AssetStore | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
) -> BatchMigrationResult:
    """
    Batch migrate Mods. Single-Mod failure does not abort the batch.

    Does not run on UI thread; caller schedules as needed.
    """
    store = store or AssetStore(root=asset_store_dir())
    batch = BatchMigrationResult(dry_run=dry_run, ok=True)
    seen_digests: set[str] = set()

    if mod_ids is not None:
        targets: list[tuple[str, Path]] = []
        for mid in mod_ids:
            folder = resolve_mod_managed_path(mid, db_path=db_path)
            if folder is not None:
                targets.append((str(mid).strip(), folder))
            else:
                batch.mods_attempted += 1
                batch.mods_failed += 1
                batch.ok = False
                batch.results.append(
                    InfoAssetMigrationResult(
                        mod_id=str(mid).strip(),
                        ok=False,
                        dry_run=dry_run,
                        reason="managed path not found",
                    )
                )
    else:
        targets = list_mod_ids_with_paths(db_path=db_path)

    if limit is not None and limit > 0:
        targets = targets[:limit]

    for mid, folder in targets:
        batch.mods_attempted += 1
        try:
            result = migrate_info_assets(
                folder, store=store, dry_run=dry_run, mod_id=mid
            )
        except Exception as exc:  # noqa: BLE001 — isolate per Mod
            logger.exception("info asset migration crashed mod_id=%s", mid)
            result = InfoAssetMigrationResult(
                mod_id=mid,
                managed_path=str(folder),
                ok=False,
                dry_run=dry_run,
                reason=f"crash: {exc}",
            )
        batch.results.append(result)
        batch.source_files += result.source_files
        batch.source_bytes += result.source_bytes
        batch.created_objects += result.created_objects
        batch.reused_objects += result.reused_objects
        for item in result.items:
            seen_digests.add(item.sha256)
        if result.skipped and result.ok:
            batch.mods_skipped += 1
        elif result.ok:
            batch.mods_succeeded += 1
        else:
            batch.mods_failed += 1
            batch.ok = False

    batch.unique_objects_seen = len(seen_digests)
    return batch


def offline_page_still_resolvable(managed_path: Path | str) -> bool:
    """Regression helper: OPEN resolver still finds an offline index."""
    return resolve_offline_page(managed_path) is not None


def assert_info_assets_intact(managed_path: Path | str, expected_files: set[str]) -> list[str]:
    """Return issues if expected asset relative paths are missing (no deletion)."""
    issues: list[str] = []
    folder = Path(managed_path)
    for tree in discover_info_asset_trees(folder):
        for rel in expected_files:
            # rel like assets/foo.png
            if not rel.startswith("assets/"):
                continue
            path = tree.offline_root / rel
            if not path.is_file():
                issues.append(f"missing source {path}")
    return issues
