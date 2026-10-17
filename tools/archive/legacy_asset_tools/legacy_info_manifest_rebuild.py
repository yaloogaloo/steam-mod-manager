"""Phase 8: Rebuild LIVE ``.info[/offline]/manifest.json`` from legacy assets.

Pipeline (never deletes ``.info/assets``)::

    .info/assets/<file>
            ↓
    SHA-256(content)
            ↓
    AssetStore.put_file (reuse if present)
            ↓
    .info[/offline]/manifest.json   (services.asset_manifest)

Does not touch Backup, DB, Identity, Deployment, asset_cache, or CAS runtime.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.paths import asset_store_dir
from services.asset_manifest import (
    MANIFEST_FILENAME,
    AssetManifest,
    ManifestError,
    verify_manifest_against_store,
)
from services.asset_store import AssetStore, sha256_file
from services.info_asset_migration import (
    discover_info_asset_trees,
    iter_asset_files,
    list_mod_ids_with_paths,
    migrate_info_assets,
    relative_manifest_path,
    resolve_mod_managed_path,
)
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA = 1
CHECKPOINT_TOOL = "legacy_info_manifest_rebuild"
DEFAULT_CHECKPOINT = Path("_tmp") / "info_manifest_rebuild_checkpoint.json"
AUDIT_JSON = Path("_tmp") / "info_manifest_rebuild_audit.json"
AUDIT_MD = Path("_tmp") / "info_manifest_rebuild_audit.md"
DETAIL_CAP = 50

_TEMP_NAME_RE = re.compile(
    r"(?i)(?:^|[/\\])(?:\..*\.tmp$|.*\.tmp$|.*\.part$|.*~$|~\$|"
    r"thumbs\.db$|desktop\.ini$)"
)


class RebuildClass(str, Enum):
    CAN_MIGRATE = "CAN_MIGRATE"
    EMPTY_ASSETS = "EMPTY_ASSETS"
    READ_ERROR = "READ_ERROR"
    INVALID_PATH = "INVALID_PATH"
    UNKNOWN_FILE = "UNKNOWN_FILE"
    CORRUPTED = "CORRUPTED"
    ALREADY_HAS_MANIFEST = "ALREADY_HAS_MANIFEST"
    PATH_MISSING = "PATH_MISSING"
    NO_ASSET_TREE = "NO_ASSET_TREE"


@dataclass
class ModManifestRebuildAudit:
    mod_id: str
    managed_path: str = ""
    classification: RebuildClass = RebuildClass.PATH_MISSING
    offline_root: str = ""
    manifest_path: str = ""
    asset_files: int = 0
    asset_bytes: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def can_migrate(self) -> bool:
        return self.classification == RebuildClass.CAN_MIGRATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "managed_path": self.managed_path,
            "classification": self.classification.value,
            "offline_root": self.offline_root,
            "manifest_path": self.manifest_path,
            "asset_files": self.asset_files,
            "asset_bytes": self.asset_bytes,
            "issues": list(self.issues)[:DETAIL_CAP],
        }


@dataclass
class ManifestRebuildAuditResult:
    mods_scanned: int = 0
    candidate_mods: int = 0
    blocked_mods: int = 0
    already_ok_mods: int = 0
    files: int = 0
    bytes_total: int = 0
    blocked_count: int = 0
    reason_breakdown: dict[str, int] = field(default_factory=dict)
    mods: list[ModManifestRebuildAudit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mods_scanned": self.mods_scanned,
            "candidate_mods": self.candidate_mods,
            "blocked_mods": self.blocked_mods,
            "already_ok_mods": self.already_ok_mods,
            "files": self.files,
            "bytes_total": self.bytes_total,
            "blocked_count": self.blocked_count,
            "reason_breakdown": dict(self.reason_breakdown),
            "mods": [m.to_dict() for m in self.mods],
        }


@dataclass
class ModRebuildResult:
    mod_id: str
    ok: bool = False
    skipped: bool = False
    dry_run: bool = True
    reason: str = ""
    classification: str = ""
    source_files: int = 0
    source_bytes: int = 0
    migrated_files: int = 0
    created_objects: int = 0
    reused_objects: int = 0
    manifest_path: str = ""
    rolled_back: bool = False
    open_ok: bool = False
    miss_ok: bool = False
    repair_ok: bool = False
    info_assets_untouched: bool = True
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "ok": self.ok,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
            "reason": self.reason,
            "classification": self.classification,
            "source_files": self.source_files,
            "source_bytes": self.source_bytes,
            "migrated_files": self.migrated_files,
            "created_objects": self.created_objects,
            "reused_objects": self.reused_objects,
            "manifest_path": self.manifest_path,
            "rolled_back": self.rolled_back,
            "open_ok": self.open_ok,
            "miss_ok": self.miss_ok,
            "repair_ok": self.repair_ok,
            "info_assets_untouched": self.info_assets_untouched,
            "issues": list(self.issues),
        }


@dataclass
class BatchRebuildResult:
    ok: bool = False
    dry_run: bool = True
    mods_attempted: int = 0
    mods_migrated: int = 0
    mods_skipped: int = 0
    mods_failed: int = 0
    files: int = 0
    bytes_total: int = 0
    new_objects: int = 0
    reused_objects: int = 0
    results: list[ModRebuildResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "mods_attempted": self.mods_attempted,
            "mods_migrated": self.mods_migrated,
            "mods_skipped": self.mods_skipped,
            "mods_failed": self.mods_failed,
            "files": self.files,
            "bytes_total": self.bytes_total,
            "new_objects": self.new_objects,
            "reused_objects": self.reused_objects,
            "results": [r.to_dict() for r in self.results],
        }


def _is_temp_name(path: Path) -> bool:
    text = path.as_posix()
    return bool(_TEMP_NAME_RE.search(text)) or path.name.startswith(".manifest_")


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


def _trees_needing_manifest(folder: Path) -> list:
    """Trees with asset files but no valid on-disk manifest."""
    needing = []
    for tree in discover_info_asset_trees(folder):
        files = list(iter_asset_files(tree.assets_dir))
        if not files:
            continue
        if tree.manifest_path.is_file():
            try:
                AssetManifest.from_path(tree.manifest_path)
                # Valid parse — still may fail CAS; Phase 8 targets missing only.
                continue
            except (OSError, ManifestError):
                # Invalid → rebuild candidate
                needing.append(tree)
                continue
        needing.append(tree)
    return needing


def audit_mod_manifest_rebuild(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    managed_path: Path | None = None,
    db_path: Path | None = None,
) -> ModManifestRebuildAudit:
    """Classify one Mod for Phase 8 manifest reconstruction."""
    mid = str(mod_id).strip()
    out = ModManifestRebuildAudit(mod_id=mid)
    folder = managed_path or resolve_mod_managed_path(mid, db_path=db_path)
    if folder is None or not Path(folder).is_dir():
        out.classification = RebuildClass.PATH_MISSING
        return out

    folder = Path(folder)
    out.managed_path = str(folder)
    trees = discover_info_asset_trees(folder)
    if not trees:
        out.classification = RebuildClass.NO_ASSET_TREE
        return out

    needing = _trees_needing_manifest(folder)
    if not needing:
        # Has trees; either empty or already manifested
        any_files = False
        for tree in trees:
            files = list(iter_asset_files(tree.assets_dir))
            if files:
                any_files = True
                break
        if not any_files:
            out.classification = RebuildClass.EMPTY_ASSETS
            out.offline_root = str(trees[0].offline_root)
            out.manifest_path = str(trees[0].manifest_path)
            return out
        out.classification = RebuildClass.ALREADY_HAS_MANIFEST
        out.offline_root = str(trees[0].offline_root)
        out.manifest_path = str(trees[0].manifest_path)
        return out

    # Audit primary needing tree (and aggregate all needing)
    primary = needing[0]
    index = resolve_offline_page(folder)
    if index is not None:
        for tree in needing:
            if tree.offline_root.resolve() == index.parent.resolve():
                primary = tree
                break
    out.offline_root = str(primary.offline_root)
    out.manifest_path = str(primary.manifest_path)

    blocked: RebuildClass | None = None
    total_files = 0
    total_bytes = 0

    for tree in needing:
        files = list(iter_asset_files(tree.assets_dir))
        if not files:
            continue
        for src in files:
            if _is_temp_name(src):
                out.issues.append(f"temp/unknown file: {src.name}")
                blocked = RebuildClass.UNKNOWN_FILE
                continue
            try:
                size = int(src.stat().st_size)
            except OSError as exc:
                out.issues.append(f"read error: {src.name}: {exc}")
                blocked = RebuildClass.READ_ERROR
                continue
            if size <= 0:
                out.issues.append(f"empty/corrupted: {src.name}")
                blocked = RebuildClass.CORRUPTED
                continue
            try:
                relative_manifest_path(tree.assets_dir, src)
            except ManifestError as exc:
                out.issues.append(f"invalid path: {src.name}: {exc}")
                blocked = RebuildClass.INVALID_PATH
                continue
            # Audit must stay fast — full SHA is execute-time only.
            # Prove readability with a small open.
            try:
                with src.open("rb") as fh:
                    fh.read(1)
            except OSError as exc:
                out.issues.append(f"hash/read error: {src.name}: {exc}")
                blocked = RebuildClass.READ_ERROR
                continue
            total_files += 1
            total_bytes += size

    out.asset_files = total_files
    out.asset_bytes = total_bytes

    if blocked is not None:
        out.classification = blocked
        return out
    if total_files == 0:
        out.classification = RebuildClass.EMPTY_ASSETS
        return out

    out.classification = RebuildClass.CAN_MIGRATE
    return out


def audit_all_manifest_rebuild(
    *,
    store: AssetStore | None = None,
    limit: int = 0,
    mod_ids: list[str] | None = None,
    db_path: Path | None = None,
    progress_every: int = 100,
) -> ManifestRebuildAuditResult:
    result = ManifestRebuildAuditResult()
    if mod_ids:
        pairs = []
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
                "manifest rebuild audit %s/%s candidates=%s blocked=%s",
                i,
                len(pairs),
                result.candidate_mods,
                result.blocked_mods,
            )
        mod = audit_mod_manifest_rebuild(
            mid, store=store, managed_path=folder, db_path=db_path
        )
        result.mods_scanned += 1
        result.mods.append(mod)
        result.reason_breakdown[mod.classification.value] = (
            result.reason_breakdown.get(mod.classification.value, 0) + 1
        )
        if mod.can_migrate:
            result.candidate_mods += 1
            result.files += mod.asset_files
            result.bytes_total += mod.asset_bytes
        elif mod.classification == RebuildClass.ALREADY_HAS_MANIFEST:
            result.already_ok_mods += 1
        else:
            result.blocked_mods += 1
            result.blocked_count += 1
    return result


def write_rebuild_audit_reports(
    audit: ManifestRebuildAuditResult,
    *,
    json_path: Path | None = None,
    md_path: Path | None = None,
) -> tuple[Path, Path]:
    json_out = Path(json_path) if json_path else AUDIT_JSON
    md_out = Path(md_path) if md_path else AUDIT_MD
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(
        json.dumps(audit.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Phase 8 — Legacy `.info` manifest rebuild audit",
        "",
        f"- mods_scanned: {audit.mods_scanned}",
        f"- candidate_mods (CAN_MIGRATE): {audit.candidate_mods}",
        f"- blocked_mods: {audit.blocked_mods}",
        f"- already_ok_mods: {audit.already_ok_mods}",
        f"- files: {audit.files}",
        f"- bytes: {audit.bytes_total}",
        f"- blocked_count: {audit.blocked_count}",
        "",
        "## Reason breakdown",
        "",
        "```",
        json.dumps(audit.reason_breakdown, indent=2, ensure_ascii=False),
        "```",
        "",
        "## BLOCKED sample (first 30)",
        "",
    ]
    blocked = [m for m in audit.mods if not m.can_migrate and m.classification not in (
        RebuildClass.ALREADY_HAS_MANIFEST,
        RebuildClass.NO_ASSET_TREE,
        RebuildClass.PATH_MISSING,
    )][:30]
    for m in blocked:
        lines.append(
            f"- mod_id={m.mod_id} reason={m.classification.value} "
            f"files={m.asset_files} bytes={m.asset_bytes}"
        )
    lines.append("")
    md_out.write_text("\n".join(lines), encoding="utf-8")
    return json_out, md_out


def _rollback_manifest(
    manifest_path: Path, *, previous_bytes: bytes | None
) -> bool:
    """Restore prior manifest bytes, or remove newly written manifest."""
    try:
        if previous_bytes is None:
            if manifest_path.is_file():
                manifest_path.unlink()
            return True
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path.with_suffix(".rollback.tmp")
        tmp.write_bytes(previous_bytes)
        os.replace(tmp, manifest_path)
        return True
    except OSError as exc:
        logger.error("manifest rollback failed %s: %s", manifest_path, exc)
        return False


def _verify_after_rebuild(
    folder: Path,
    *,
    mod_id: str,
    store: AssetStore,
    assets_before: tuple[int, int],
) -> tuple[bool, bool, bool, bool, list[str]]:
    """OPEN / MISS / Repair; prove .info/assets not rewritten."""
    from services.info_asset_runtime import repair_live_from_cas
    from services.info_asset_runtime import ensure_live_offline_openable

    issues: list[str] = []
    after = _count_assets(folder)
    untouched = after == assets_before
    if not untouched:
        issues.append(
            f".info/assets changed during rebuild: before={assets_before} after={after}"
        )

    try:
        opened = ensure_live_offline_openable(folder, store=store)
        open_ok = opened is not None
        if not open_ok:
            issues.append("OPEN failed after rebuild")
    except Exception as exc:  # noqa: BLE001
        open_ok = False
        issues.append(f"OPEN raised: {exc}")

    repair = repair_live_from_cas(
        folder, mod_id=mod_id, store=store
    )
    repair_ok = bool(repair.ok)
    if not repair_ok:
        issues.append(f"Repair failed: {repair.reason}")

    again = _count_assets(folder)
    if again != assets_before:
        issues.append(f"Repair mutated .info/assets: {again}")
        untouched = False
        repair_ok = False

    miss_ok = open_ok and repair_ok and untouched
    return open_ok and repair_ok and untouched, open_ok, repair_ok, untouched, issues


def rebuild_mod_info_manifest(
    mod_id: str | int,
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    verify_runtime: bool = True,
    db_path: Path | None = None,
    managed_path: Path | None = None,
) -> ModRebuildResult:
    """
    Rebuild LIVE manifest(s) for one Mod from ``.info/assets``.

    On verification failure after writing manifest: rollback that Mod's
    manifest file(s). Never deletes ``.info/assets`` or CAS objects.
    """
    mid = str(mod_id).strip()
    store = store or AssetStore(root=asset_store_dir())
    out = ModRebuildResult(mod_id=mid, dry_run=dry_run)

    audit = audit_mod_manifest_rebuild(
        mid, store=store, managed_path=managed_path, db_path=db_path
    )
    out.classification = audit.classification.value
    out.source_files = audit.asset_files
    out.source_bytes = audit.asset_bytes
    out.manifest_path = audit.manifest_path

    if audit.classification == RebuildClass.ALREADY_HAS_MANIFEST:
        out.ok = True
        out.skipped = True
        out.reason = "already has manifest"
        return out
    if not audit.can_migrate:
        out.ok = True
        out.skipped = True
        out.reason = f"BLOCKED:{audit.classification.value}"
        out.issues = list(audit.issues)
        return out

    folder = Path(audit.managed_path)
    assets_before = _count_assets(folder)

    # Snapshot existing manifests for rollback (per needing tree)
    needing = _trees_needing_manifest(folder)
    previous: dict[str, bytes | None] = {}
    for tree in needing:
        key = str(tree.manifest_path)
        if tree.manifest_path.is_file():
            try:
                previous[key] = tree.manifest_path.read_bytes()
            except OSError:
                previous[key] = None
        else:
            previous[key] = None

    mig = migrate_info_assets(
        folder, store=store, dry_run=dry_run, mod_id=mid
    )
    out.migrated_files = int(mig.migrated_files)
    out.created_objects = int(mig.created_objects)
    out.reused_objects = int(mig.reused_objects)
    out.issues.extend(list(mig.issues))

    if dry_run:
        out.ok = bool(mig.ok)
        out.reason = mig.reason or ("dry-run ok" if mig.ok else "dry-run failed")
        return out

    if not mig.ok:
        # migrate_info_assets does not write manifest on failure — still
        # ensure no partial manifests left from multi-tree.
        for tree in needing:
            path = tree.manifest_path
            key = str(path)
            if path.is_file() and previous.get(key) is None:
                # Newly created somehow — remove
                _rollback_manifest(path, previous_bytes=None)
                out.rolled_back = True
            elif path.is_file() and previous.get(key) is not None:
                # Should not have been rewritten on failure, but restore if changed
                try:
                    if path.read_bytes() != previous[key]:
                        _rollback_manifest(path, previous_bytes=previous[key])
                        out.rolled_back = True
                except OSError:
                    pass
        out.ok = False
        out.reason = mig.reason or "migrate failed"
        return out

    # Post-condition: every needing tree now has verifying manifest
    for tree in needing:
        if not tree.manifest_path.is_file():
            out.ok = False
            out.reason = f"manifest missing after migrate: {tree.manifest_path}"
            for t in needing:
                _rollback_manifest(
                    t.manifest_path, previous_bytes=previous.get(str(t.manifest_path))
                )
            out.rolled_back = True
            return out
        try:
            man = AssetManifest.from_path(tree.manifest_path)
            disk_files = [
                p
                for p in iter_asset_files(tree.assets_dir)
                if not _is_temp_name(p)
            ]
            # Count non-empty readable files expected in manifest
            expected = 0
            for p in disk_files:
                try:
                    if int(p.stat().st_size) > 0:
                        expected += 1
                except OSError:
                    pass
            if len(man.assets) != expected:
                out.ok = False
                out.reason = (
                    f"manifest count mismatch: manifest={len(man.assets)} "
                    f"files={expected}"
                )
                for t in needing:
                    _rollback_manifest(
                        t.manifest_path,
                        previous_bytes=previous.get(str(t.manifest_path)),
                    )
                out.rolled_back = True
                return out
            issues = verify_manifest_against_store(man, store)
            if issues:
                out.ok = False
                out.reason = "CAS verify failed after rebuild"
                out.issues.extend(issues[:10])
                for t in needing:
                    _rollback_manifest(
                        t.manifest_path,
                        previous_bytes=previous.get(str(t.manifest_path)),
                    )
                out.rolled_back = True
                return out
        except (OSError, ManifestError) as exc:
            out.ok = False
            out.reason = f"manifest invalid after write: {exc}"
            for t in needing:
                _rollback_manifest(
                    t.manifest_path,
                    previous_bytes=previous.get(str(t.manifest_path)),
                )
            out.rolled_back = True
            return out

    if verify_runtime:
        ok, open_ok, repair_ok, untouched, v_issues = _verify_after_rebuild(
            folder, mod_id=mid, store=store, assets_before=assets_before
        )
        out.open_ok = open_ok
        out.repair_ok = repair_ok
        out.miss_ok = ok
        out.info_assets_untouched = untouched
        out.issues.extend(v_issues)
        if not ok:
            out.ok = False
            out.reason = "runtime verification failed; rolling back manifests"
            for t in needing:
                _rollback_manifest(
                    t.manifest_path,
                    previous_bytes=previous.get(str(t.manifest_path)),
                )
            out.rolled_back = True
            return out
    else:
        out.info_assets_untouched = _count_assets(folder) == assets_before

    out.ok = True
    out.reason = "rebuilt"
    out.manifest_path = audit.manifest_path
    return out


def load_rebuild_checkpoint(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_CHECKPOINT
    empty = {
        "schema_version": CHECKPOINT_SCHEMA,
        "tool": CHECKPOINT_TOOL,
        "last_processed_mod_id": "",
        "migrated_mod_ids": [],
        "skipped_mod_ids": [],
        "failed_mod_ids": [],
        "files": 0,
        "bytes": 0,
        "new_objects": 0,
        "reused_objects": 0,
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


def save_rebuild_checkpoint(
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


def rebuild_all_info_manifests(
    *,
    store: AssetStore | None = None,
    dry_run: bool = True,
    resume: bool = False,
    checkpoint_path: Path | None = None,
    limit: int = 0,
    mod_ids: list[str] | None = None,
    verify_runtime: bool = True,
    db_path: Path | None = None,
    stop_on_failure: bool = False,
    should_cancel: Any | None = None,
    batch_pause_ms: int = 40,
    on_progress: Any | None = None,
) -> BatchRebuildResult:
    """
    Batch rebuild. Each Mod is independent; failures do not roll back others.

    Default ``stop_on_failure=False`` so one blocked Mod does not halt the batch.
    Optional cancel / pause hooks support background workers.
    """
    import time

    store = store or AssetStore(root=asset_store_dir())
    batch = BatchRebuildResult(dry_run=dry_run)
    cp = load_rebuild_checkpoint(checkpoint_path) if resume else {
        "schema_version": CHECKPOINT_SCHEMA,
        "tool": CHECKPOINT_TOOL,
        "last_processed_mod_id": "",
        "migrated_mod_ids": [],
        "skipped_mod_ids": [],
        "failed_mod_ids": [],
        "files": 0,
        "bytes": 0,
        "new_objects": 0,
        "reused_objects": 0,
        "updated_at": "",
    }
    done = set(str(x) for x in (cp.get("migrated_mod_ids") or []))
    skipped_set = set(str(x) for x in (cp.get("skipped_mod_ids") or []))
    failed_set = set(str(x) for x in (cp.get("failed_mod_ids") or []))

    if mod_ids:
        ids = [str(m).strip() for m in mod_ids]
    else:
        audit = audit_all_manifest_rebuild(
            store=store, limit=limit, db_path=db_path
        )
        ids = [m.mod_id for m in audit.mods if m.can_migrate]

    if limit > 0:
        ids = ids[: int(limit)]

    pause_s = max(0.0, float(batch_pause_ms) / 1000.0)

    for mid in ids:
        if callable(should_cancel) and should_cancel():
            batch.ok = False
            return batch
        if mid in done or mid in skipped_set:
            continue
        batch.mods_attempted += 1
        result = rebuild_mod_info_manifest(
            mid,
            store=store,
            dry_run=dry_run,
            verify_runtime=verify_runtime and not dry_run,
            db_path=db_path,
        )
        batch.results.append(result)
        cp["last_processed_mod_id"] = mid

        if callable(on_progress):
            try:
                on_progress(
                    {
                        "phase": "rebuild",
                        "mod_id": mid,
                        "ok": result.ok,
                        "skipped": result.skipped,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        if result.skipped:
            batch.mods_skipped += 1
            skipped_set.add(mid)
            cp["skipped_mod_ids"] = sorted(skipped_set)
        elif result.ok:
            batch.mods_migrated += 1
            done.add(mid)
            cp["migrated_mod_ids"] = sorted(done)
            batch.files += result.migrated_files or result.source_files
            batch.bytes_total += result.source_bytes
            batch.new_objects += result.created_objects
            batch.reused_objects += result.reused_objects
            if not dry_run:
                cp["files"] = int(cp.get("files") or 0) + (
                    result.migrated_files or result.source_files
                )
                cp["bytes"] = int(cp.get("bytes") or 0) + result.source_bytes
                cp["new_objects"] = int(cp.get("new_objects") or 0) + result.created_objects
                cp["reused_objects"] = int(cp.get("reused_objects") or 0) + result.reused_objects
        else:
            batch.mods_failed += 1
            failed_set.add(mid)
            cp["failed_mod_ids"] = sorted(failed_set)
            save_rebuild_checkpoint(cp, checkpoint_path)
            if stop_on_failure:
                batch.ok = False
                return batch

        save_rebuild_checkpoint(cp, checkpoint_path)
        if pause_s > 0:
            time.sleep(pause_s)

    batch.ok = batch.mods_failed == 0
    return batch