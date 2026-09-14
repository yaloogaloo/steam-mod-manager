"""Phase 4.1: classify / resolve Backup ``MISSING_MANIFEST`` debt.

Never deletes Backup assets. Migration reuses Phase 3
``migrate_backup_offline_for_mod_id`` (``.info`` manifest preferred).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from core.paths import asset_store_dir
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, ManifestError
from services.asset_store import AssetStore
from services.backup_asset_migration import (
    backup_offline_manifest_path,
    load_live_manifest,
    migrate_backup_offline_for_mod_id,
)
from services.info_asset_migration import (
    iter_asset_files,
    resolve_mod_managed_path,
)
from tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup import _list_mod_ids
from services.metadata_backup import BACKUP_OFFLINE_DIR, backup_root
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)


class ManifestDebtClass(str, Enum):
    """Why a Backup lacks a usable offline manifest (or related debt)."""

    OK_HAS_MANIFEST = "OK_HAS_MANIFEST"
    NO_BACKUP_OFFLINE = "NO_BACKUP_OFFLINE"
    NO_BACKUP_ASSETS = "NO_BACKUP_ASSETS"
    CORRUPT_BACKUP_MANIFEST = "CORRUPT_BACKUP_MANIFEST"
    CAN_MIGRATE_FROM_INFO = "CAN_MIGRATE_FROM_INFO"
    CAN_MIGRATE_FROM_BACKUP_ASSETS = "CAN_MIGRATE_FROM_BACKUP_ASSETS"
    NO_INFO_MANIFEST = "NO_INFO_MANIFEST"
    NO_LIVE_FOLDER = "NO_LIVE_FOLDER"
    OTHER = "OTHER"


@dataclass
class ManifestDebtItem:
    mod_id: str
    classification: ManifestDebtClass
    detail: str = ""
    backup_offline: str = ""
    asset_files: int = 0
    asset_bytes: int = 0
    has_info_manifest: bool = False
    has_backup_manifest: bool = False
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "classification": self.classification.value,
            "detail": self.detail,
            "backup_offline": self.backup_offline,
            "asset_files": self.asset_files,
            "asset_bytes": self.asset_bytes,
            "has_info_manifest": self.has_info_manifest,
            "has_backup_manifest": self.has_backup_manifest,
            "recommended_action": self.recommended_action,
        }


@dataclass
class ManifestDebtAuditResult:
    mods_scanned: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    by_class_bytes: dict[str, int] = field(default_factory=dict)
    items: list[ManifestDebtItem] = field(default_factory=list)
    migratable_mods: int = 0
    blocked_mods: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mods_scanned": self.mods_scanned,
            "by_class": dict(self.by_class),
            "by_class_bytes": dict(self.by_class_bytes),
            "migratable_mods": self.migratable_mods,
            "blocked_mods": self.blocked_mods,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class ManifestMigrationBatchResult:
    ok: bool = True
    dry_run: bool = False
    mods_attempted: int = 0
    mods_succeeded: int = 0
    mods_failed: int = 0
    mods_skipped: int = 0
    created_objects: int = 0
    reused_objects: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_attempted": self.mods_attempted,
            "mods_succeeded": self.mods_succeeded,
            "mods_failed": self.mods_failed,
            "mods_skipped": self.mods_skipped,
            "created_objects": self.created_objects,
            "reused_objects": self.reused_objects,
            "results": list(self.results),
        }


def _info_manifest_present(mod_id: str, db_path: Path | None = None) -> tuple[bool, Path | None]:
    folder = resolve_mod_managed_path(mod_id, db_path=db_path)
    if folder is None:
        return False, None
    index = resolve_offline_page(folder)
    if index is None:
        # Still check common locations
        for candidate in (
            folder / ".info" / MANIFEST_FILENAME,
            folder / ".info" / "offline" / MANIFEST_FILENAME,
        ):
            if candidate.is_file():
                return True, folder
        return False, folder
    man = index.parent / MANIFEST_FILENAME
    return man.is_file(), folder


def classify_manifest_debt_for_mod(
    mod_id: str | int,
    *,
    db_path: Path | None = None,
) -> ManifestDebtItem:
    mid = str(mod_id).strip()
    dest = backup_root(mid) / BACKUP_OFFLINE_DIR
    item = ManifestDebtItem(mod_id=mid, classification=ManifestDebtClass.OTHER, backup_offline=str(dest))

    if not dest.is_dir():
        item.classification = ManifestDebtClass.NO_BACKUP_OFFLINE
        item.detail = "backup offline directory missing"
        item.recommended_action = "no legacy assets to clean; skip"
        return item

    assets_dir = dest / "assets"
    files = list(iter_asset_files(assets_dir)) if assets_dir.is_dir() else []
    item.asset_files = len(files)
    for f in files:
        try:
            item.asset_bytes += int(f.stat().st_size)
        except OSError:
            pass

    man_path = backup_offline_manifest_path(dest)
    item.has_backup_manifest = man_path.is_file()
    if man_path.is_file():
        try:
            AssetManifest.from_path(man_path)
            item.classification = ManifestDebtClass.OK_HAS_MANIFEST
            item.detail = "backup offline/manifest.json valid"
            item.recommended_action = "eligible for SAFE cleanup when CAS matches"
            return item
        except (OSError, ManifestError) as exc:
            item.classification = ManifestDebtClass.CORRUPT_BACKUP_MANIFEST
            item.detail = str(exc)
            item.recommended_action = "repair/regenerate manifest; do not delete assets"
            return item

    if item.asset_files == 0:
        item.classification = ManifestDebtClass.NO_BACKUP_ASSETS
        item.detail = "no offline/assets and no manifest"
        item.recommended_action = "nothing to migrate for cleanup"
        return item

    has_info, folder = _info_manifest_present(mid, db_path=db_path)
    item.has_info_manifest = has_info
    if folder is None:
        # Can still migrate from Backup assets alone via Phase 3
        item.classification = ManifestDebtClass.CAN_MIGRATE_FROM_BACKUP_ASSETS
        item.detail = "no live folder; can hash Backup assets into Store + manifest"
        item.recommended_action = "migrate_backup_offline_for_mod_id"
        return item

    if has_info:
        item.classification = ManifestDebtClass.CAN_MIGRATE_FROM_INFO
        item.detail = ".info/manifest.json present; prefer reuse into Backup manifest"
        item.recommended_action = "migrate_backup_offline_for_mod_id (reuse info)"
        return item

    # Live folder exists but no info manifest — still can hash Backup assets
    live = load_live_manifest(resolve_offline_page(folder) if folder else None)
    if live is not None:
        item.has_info_manifest = True
        item.classification = ManifestDebtClass.CAN_MIGRATE_FROM_INFO
        item.detail = "live offline manifest loadable"
        item.recommended_action = "migrate_backup_offline_for_mod_id"
        return item

    item.classification = ManifestDebtClass.CAN_MIGRATE_FROM_BACKUP_ASSETS
    item.detail = "no .info manifest; Backup assets can seed Store + Backup manifest"
    item.recommended_action = "migrate_backup_offline_for_mod_id from Backup assets"
    # Also note missing info for later Phase
    if folder is not None and not has_info:
        item.detail += "; .info/manifest also missing (Phase 2 debt separate)"
    return item


def audit_manifest_debt(
    *,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
    only_missing: bool = True,
) -> ManifestDebtAuditResult:
    """Classify Backup manifest debt. Readonly."""
    result = ManifestDebtAuditResult()
    ids = _list_mod_ids(mod_ids=mod_ids, db_path=db_path, limit=limit)
    migratable = {
        ManifestDebtClass.CAN_MIGRATE_FROM_INFO,
        ManifestDebtClass.CAN_MIGRATE_FROM_BACKUP_ASSETS,
    }
    for mid in ids:
        item = classify_manifest_debt_for_mod(mid, db_path=db_path)
        if only_missing and item.classification == ManifestDebtClass.OK_HAS_MANIFEST:
            # Still count scan but omit from detail unless missing assets debt
            if item.asset_files == 0:
                result.mods_scanned += 1
                continue
        result.mods_scanned += 1
        key = item.classification.value
        result.by_class[key] = result.by_class.get(key, 0) + 1
        result.by_class_bytes[key] = result.by_class_bytes.get(key, 0) + item.asset_bytes
        if only_missing and item.classification == ManifestDebtClass.OK_HAS_MANIFEST:
            continue
        result.items.append(item)
        if item.classification in migratable and item.asset_files > 0:
            result.migratable_mods += 1
        elif item.classification in (
            ManifestDebtClass.CORRUPT_BACKUP_MANIFEST,
            ManifestDebtClass.OTHER,
        ):
            result.blocked_mods += 1
    return result


def migrate_manifest_debt(
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    mod_ids: Iterable[str | int] | None = None,
    db_path: Path | None = None,
    limit: int | None = None,
    only_migratable: bool = True,
) -> ManifestMigrationBatchResult:
    """
    Run Phase 3 Backup manifest migration for debt Mods.

    Never deletes Backup ``offline/assets``.
    """
    store = store or AssetStore(root=asset_store_dir())
    batch = ManifestMigrationBatchResult(dry_run=dry_run, ok=True)
    debt = audit_manifest_debt(
        mod_ids=mod_ids, db_path=db_path, limit=limit, only_missing=True
    )
    migratable = {
        ManifestDebtClass.CAN_MIGRATE_FROM_INFO,
        ManifestDebtClass.CAN_MIGRATE_FROM_BACKUP_ASSETS,
        ManifestDebtClass.CORRUPT_BACKUP_MANIFEST,  # regenerate if assets exist
    }
    targets = [
        i
        for i in debt.items
        if i.asset_files > 0
        and (
            not only_migratable
            or i.classification in migratable
        )
    ]
    for item in targets:
        batch.mods_attempted += 1
        try:
            result = migrate_backup_offline_for_mod_id(
                item.mod_id, store=store, dry_run=dry_run, db_path=db_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("manifest debt migration crashed mod_id=%s", item.mod_id)
            batch.mods_failed += 1
            batch.ok = False
            batch.results.append(
                {"mod_id": item.mod_id, "ok": False, "reason": f"crash: {exc}"}
            )
            continue
        batch.results.append(result.to_dict())
        batch.created_objects += int(result.created_objects)
        batch.reused_objects += int(result.reused_objects)
        if result.skipped and result.ok:
            batch.mods_skipped += 1
        elif result.ok:
            batch.mods_succeeded += 1
        else:
            batch.mods_failed += 1
            batch.ok = False
    return batch
