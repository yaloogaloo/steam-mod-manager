"""Fast GC for leftover LIVE ``.info/assets`` copies.

Garbage collection only: directory walk + manifest path set + unlink.
Never hashes files, never materializes OPEN, never writes Asset Store /
Backup / DB / Identity / manifest.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.mod_platform import supports_offline_page_download
from core.paths import asset_store_dir, database_path, project_root
from services.asset_manifest import MANIFEST_FILENAME
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.info_asset_migration import discover_info_asset_trees

logger = logging.getLogger(__name__)

CHECKPOINT_PATH = Path("_tmp") / "info_asset_fast_purge_checkpoint.json"
DEFAULT_BATCH_SIZE = 25
KEEP_DETAIL_CAP = 50


class PurgeClass(str, Enum):
    SAFE_DELETE = "SAFE_DELETE"
    LOW_RISK_DELETE = "LOW_RISK_DELETE"
    KEEP = "KEEP"
    NO_ASSETS = "NO_ASSETS"


@dataclass
class ModCandidate:
    mod_id: str
    managed_path: Path
    workspace_id: str = ""
    source_url: str = ""
    platform: str = ""


@dataclass
class TreePurgePlan:
    assets_dir: str
    classification: str
    files: int = 0
    bytes_total: int = 0
    delete_files: int = 0
    delete_bytes: int = 0
    keep_files: int = 0
    keep_bytes: int = 0
    keep_reason: str = ""
    unknown_files: int = 0


@dataclass
class ModPurgePlan:
    mod_id: str
    managed_path: str = ""
    classification: str = PurgeClass.NO_ASSETS.value
    files: int = 0
    bytes_total: int = 0
    delete_files: int = 0
    delete_bytes: int = 0
    keep_files: int = 0
    keep_bytes: int = 0
    keep_reason: str = ""
    unknown_files: int = 0
    trees: list[TreePurgePlan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "managed_path": self.managed_path,
            "classification": self.classification,
            "files": self.files,
            "bytes": self.bytes_total,
            "delete_files": self.delete_files,
            "delete_bytes": self.delete_bytes,
            "keep_files": self.keep_files,
            "keep_bytes": self.keep_bytes,
            "keep_reason": self.keep_reason,
            "unknown_files": self.unknown_files,
        }


@dataclass
class FastPurgeAuditResult:
    mods: int = 0
    files: int = 0
    bytes_total: int = 0
    delete_mods: int = 0
    delete_files: int = 0
    delete_bytes: int = 0
    keep_mods: int = 0
    keep_files: int = 0
    keep_bytes: int = 0
    safe_mods: int = 0
    low_risk_mods: int = 0
    no_assets_mods: int = 0
    elapsed_sec: float = 0.0
    plans: list[ModPurgePlan] = field(default_factory=list)
    keep_samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mods": self.mods,
            "files": self.files,
            "bytes": self.bytes_total,
            "delete_mods": self.delete_mods,
            "delete_files": self.delete_files,
            "delete_bytes": self.delete_bytes,
            "keep_mods": self.keep_mods,
            "keep_files": self.keep_files,
            "keep_bytes": self.keep_bytes,
            "safe_mods": self.safe_mods,
            "low_risk_mods": self.low_risk_mods,
            "no_assets_mods": self.no_assets_mods,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "keep_samples": list(self.keep_samples)[:KEEP_DETAIL_CAP],
        }


@dataclass
class FastPurgeExecuteResult:
    dry_run: bool = True
    before_mods: int = 0
    before_files: int = 0
    before_bytes: int = 0
    after_mods: int = 0
    after_files: int = 0
    after_bytes: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    cleaned_mods: int = 0
    skipped_mods: int = 0
    keep_mods: int = 0
    last_mod_id: str = ""
    elapsed_sec: float = 0.0
    keep_samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "before": {
                "mods": self.before_mods,
                "files": self.before_files,
                "bytes": self.before_bytes,
            },
            "after": {
                "mods": self.after_mods,
                "files": self.after_files,
                "bytes": self.after_bytes,
            },
            "deleted": {
                "files": self.deleted_files,
                "bytes": self.deleted_bytes,
            },
            "cleaned_mods": self.cleaned_mods,
            "skipped_mods": self.skipped_mods,
            "keep_mods": self.keep_mods,
            "last_mod_id": self.last_mod_id,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "keep_samples": list(self.keep_samples)[:KEEP_DETAIL_CAP],
        }


def default_checkpoint_path() -> Path:
    return project_root() / CHECKPOINT_PATH


def load_checkpoint(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else default_checkpoint_path()
    empty = {
        "cleaned_mod_ids": [],
        "deleted_files": 0,
        "deleted_bytes": 0,
        "last_mod_id": "",
    }
    try:
        if not target.is_file():
            return empty
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    ids = data.get("cleaned_mod_ids") or []
    if not isinstance(ids, list):
        ids = []
    return {
        "cleaned_mod_ids": [str(x) for x in ids],
        "deleted_files": int(data.get("deleted_files") or 0),
        "deleted_bytes": int(data.get("deleted_bytes") or 0),
        "last_mod_id": str(data.get("last_mod_id") or ""),
    }


def save_checkpoint(
    *,
    cleaned_mod_ids: Iterable[str],
    deleted_files: int,
    deleted_bytes: int,
    last_mod_id: str,
    path: Path | None = None,
) -> Path:
    target = Path(path) if path is not None else default_checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cleaned_mod_ids": [str(x) for x in cleaned_mod_ids],
        "deleted_files": int(deleted_files),
        "deleted_bytes": int(deleted_bytes),
        "last_mod_id": str(last_mod_id or ""),
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def list_candidates(*, db_path: Path | None = None) -> list[ModCandidate]:
    """Read-only DB listing. Does not mutate Identity / paths."""
    path = Path(db_path) if db_path is not None else database_path()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    out: list[ModCandidate] = []
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute(
            "SELECT mod_id, last_known_path, workspace_id, source_url, platform "
            "FROM mods "
            "WHERE last_known_path IS NOT NULL AND TRIM(last_known_path) != '' "
            "ORDER BY mod_id"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for mod_id, lkp, workspace_id, source_url, platform in rows:
        folder = Path(str(lkp or "").strip())
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        out.append(
            ModCandidate(
                mod_id=str(mod_id),
                managed_path=folder,
                workspace_id=str(workspace_id or "").strip(),
                source_url=str(source_url or "").strip(),
                platform=str(platform or "").strip(),
            )
        )
    return out


def _walk_asset_files(assets_dir: Path) -> Iterator[tuple[Path, int]]:
    """Yield ``(path, size)``. No hash. No sort."""
    try:
        if not assets_dir.is_dir():
            return
    except OSError:
        return
    for root, _dirs, files in os.walk(assets_dir, followlinks=False):
        for name in files:
            path = Path(root) / name
            try:
                size = int(path.stat().st_size)
            except OSError:
                size = 0
            yield path, size


def _rel_asset_path(assets_dir: Path, file_path: Path) -> str:
    rel = file_path.relative_to(assets_dir).as_posix()
    return f"assets/{rel}"


def _manifest_path_set(manifest_path: Path) -> set[str] | None:
    """Return manifest relative paths, or None if missing/empty/unreadable.

    Path-set only. Does not hash files or verify CAS objects.
    """
    try:
        if not manifest_path.is_file():
            return None
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("assets")
    if not isinstance(raw, list) or not raw:
        return None
    paths: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip().replace("\\", "/")
        if rel:
            paths.add(rel)
    return paths or None


def _can_restore(mod: ModCandidate) -> bool:
    """True when the Mod can be re-fetched or already has a platform source."""
    if str(mod.source_url or "").strip():
        return True
    if supports_offline_page_download(mod.platform):
        return True
    return False


def _has_offline_index(folder: Path) -> bool:
    for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
        for index in (
            folder / info_name / "offline" / "index.html",
            folder / info_name / "index.html",
        ):
            try:
                if index.is_file():
                    return True
            except OSError:
                continue
    return False


def _store_has_any_object(store_root: Path | None) -> bool:
    """Cheap existence probe. Does not hash or iterate all objects."""
    root = Path(store_root) if store_root is not None else asset_store_dir()
    sha_root = root / "sha256"
    try:
        if not sha_root.is_dir():
            return False
        with os.scandir(sha_root) as it:
            for prefix in it:
                if not prefix.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(prefix.path) as objects:
                    for obj in objects:
                        if obj.is_file(follow_symlinks=False):
                            return True
    except OSError:
        return False
    return False


def classify_tree(
    assets_dir: Path,
    *,
    recoverable: bool,
    store_has_objects: bool,
    has_index: bool,
) -> TreePurgePlan:
    plan = TreePurgePlan(
        assets_dir=str(assets_dir),
        classification=PurgeClass.NO_ASSETS.value,
    )
    files = list(_walk_asset_files(assets_dir))
    if not files:
        return plan

    plan.files = len(files)
    plan.bytes_total = sum(size for _p, size in files)
    manifest = _manifest_path_set(assets_dir.parent / MANIFEST_FILENAME)
    if manifest is not None:
        unknown = 0
        keep_bytes = 0
        delete_n = 0
        delete_b = 0
        for path, size in files:
            try:
                rel = _rel_asset_path(assets_dir, path)
            except ValueError:
                unknown += 1
                keep_bytes += size
                continue
            if rel in manifest:
                delete_n += 1
                delete_b += size
            else:
                unknown += 1
                keep_bytes += size
        plan.delete_files = delete_n
        plan.delete_bytes = delete_b
        plan.keep_files = unknown
        plan.keep_bytes = keep_bytes
        plan.unknown_files = unknown
        if unknown == 0 and delete_n > 0:
            plan.classification = PurgeClass.SAFE_DELETE.value
            return plan
        if delete_n > 0:
            # Covered copies go; leftover names are UNKNOWN_FILE.
            plan.classification = PurgeClass.SAFE_DELETE.value
            plan.keep_reason = "UNKNOWN_FILE"
            return plan
        plan.classification = PurgeClass.KEEP.value
        plan.keep_reason = "UNKNOWN_FILE"
        return plan

    if recoverable or (store_has_objects and has_index):
        plan.classification = PurgeClass.LOW_RISK_DELETE.value
        plan.delete_files = plan.files
        plan.delete_bytes = plan.bytes_total
        plan.keep_reason = "LOW_RISK"
        return plan

    plan.classification = PurgeClass.KEEP.value
    plan.keep_files = plan.files
    plan.keep_bytes = plan.bytes_total
    plan.keep_reason = "NO_SOURCE"
    return plan


def classify_mod(
    mod: ModCandidate,
    *,
    store_has_objects: bool | None = None,
) -> ModPurgePlan:
    folder = Path(mod.managed_path)
    out = ModPurgePlan(mod_id=str(mod.mod_id), managed_path=str(folder))
    trees = discover_info_asset_trees(folder)
    if not trees:
        out.classification = PurgeClass.NO_ASSETS.value
        return out

    recoverable = _can_restore(mod)
    has_index = _has_offline_index(folder)
    if store_has_objects is None:
        store_has_objects = False
    classes: list[str] = []
    for tree in trees:
        tplan = classify_tree(
            tree.assets_dir,
            recoverable=recoverable,
            store_has_objects=bool(store_has_objects),
            has_index=has_index,
        )
        out.trees.append(tplan)
        out.files += tplan.files
        out.bytes_total += tplan.bytes_total
        out.delete_files += tplan.delete_files
        out.delete_bytes += tplan.delete_bytes
        out.keep_files += tplan.keep_files
        out.keep_bytes += tplan.keep_bytes
        out.unknown_files += tplan.unknown_files
        if tplan.keep_reason and not out.keep_reason:
            out.keep_reason = tplan.keep_reason
        if tplan.files:
            classes.append(tplan.classification)

    if not out.files:
        out.classification = PurgeClass.NO_ASSETS.value
        return out
    if out.keep_files and not out.delete_files:
        out.classification = PurgeClass.KEEP.value
        out.keep_reason = out.keep_reason or "UNKNOWN_FILE"
        return out
    if PurgeClass.LOW_RISK_DELETE.value in classes and PurgeClass.SAFE_DELETE.value not in classes:
        out.classification = PurgeClass.LOW_RISK_DELETE.value
        out.keep_reason = "LOW_RISK"
        return out
    out.classification = PurgeClass.SAFE_DELETE.value
    return out


def _unlink_file(path: Path) -> bool:
    try:
        os.unlink(path)
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
            return True
        except OSError:
            return False
    except OSError:
        return False


def _rm_empty_dirs(assets_dir: Path) -> None:
    try:
        if not assets_dir.exists():
            return
    except OSError:
        return
    for root, dirs, files in os.walk(assets_dir, topdown=False, followlinks=False):
        if files or dirs:
            # ``dirs`` still lists names even if children were removed; try anyway.
            pass
        try:
            os.rmdir(root)
        except OSError:
            continue


def _delete_planned_files(plan: ModPurgePlan, *, dry_run: bool) -> tuple[int, int]:
    deleted_files = 0
    deleted_bytes = 0
    if dry_run:
        return plan.delete_files, plan.delete_bytes
    for tree in plan.trees:
        assets_dir = Path(tree.assets_dir)
        if tree.classification == PurgeClass.KEEP.value:
            continue
        manifest = _manifest_path_set(assets_dir.parent / MANIFEST_FILENAME)
        for path, size in _walk_asset_files(assets_dir):
            delete = False
            if tree.classification == PurgeClass.LOW_RISK_DELETE.value:
                delete = True
            elif manifest is not None:
                try:
                    rel = _rel_asset_path(assets_dir, path)
                except ValueError:
                    rel = ""
                delete = bool(rel) and rel in manifest
            if not delete:
                continue
            if _unlink_file(path):
                deleted_files += 1
                deleted_bytes += size
        _rm_empty_dirs(assets_dir)
    return deleted_files, deleted_bytes


def audit(
    *,
    mods: list[ModCandidate] | None = None,
    db_path: Path | None = None,
    store_root: Path | None = None,
) -> FastPurgeAuditResult:
    """Classify leftover ``.info/assets`` without deleting or hashing."""
    t0 = time.perf_counter()
    candidates = mods if mods is not None else list_candidates(db_path=db_path)
    store_has = _store_has_any_object(store_root)
    result = FastPurgeAuditResult()
    for mod in candidates:
        plan = classify_mod(mod, store_has_objects=store_has)
        if plan.classification == PurgeClass.NO_ASSETS.value:
            result.no_assets_mods += 1
            continue
        result.mods += 1
        result.files += plan.files
        result.bytes_total += plan.bytes_total
        result.plans.append(plan)
        if plan.delete_files:
            result.delete_mods += 1
            result.delete_files += plan.delete_files
            result.delete_bytes += plan.delete_bytes
        if plan.classification == PurgeClass.SAFE_DELETE.value:
            result.safe_mods += 1
        elif plan.classification == PurgeClass.LOW_RISK_DELETE.value:
            result.low_risk_mods += 1
        if plan.keep_files:
            result.keep_mods += 1
            result.keep_files += plan.keep_files
            result.keep_bytes += plan.keep_bytes
            if len(result.keep_samples) < KEEP_DETAIL_CAP:
                result.keep_samples.append(plan.to_dict())
    result.elapsed_sec = time.perf_counter() - t0
    return result


def execute(
    *,
    mods: list[ModCandidate] | None = None,
    db_path: Path | None = None,
    store_root: Path | None = None,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    resume: bool = False,
    checkpoint_path: Path | None = None,
) -> FastPurgeExecuteResult:
    """Unlink leftover ``.info/assets`` files. Never writes Store / Backup / DB."""
    t0 = time.perf_counter()
    checkpoint_path = (
        Path(checkpoint_path) if checkpoint_path is not None else default_checkpoint_path()
    )
    batch_n = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
    state = (
        load_checkpoint(checkpoint_path)
        if resume
        else {
            "cleaned_mod_ids": [],
            "deleted_files": 0,
            "deleted_bytes": 0,
            "last_mod_id": "",
        }
    )
    done: set[str] = set(state["cleaned_mod_ids"])
    cleaned_ids: list[str] = list(state["cleaned_mod_ids"])
    checkpoint_deleted_files = int(state["deleted_files"])
    checkpoint_deleted_bytes = int(state["deleted_bytes"])
    last_mod_id = str(state["last_mod_id"] or "")
    run_deleted_files = 0
    run_deleted_bytes = 0

    candidates = mods if mods is not None else list_candidates(db_path=db_path)
    store_has = _store_has_any_object(store_root)
    result = FastPurgeExecuteResult(dry_run=bool(dry_run))
    pending_since_checkpoint = 0

    for mod in candidates:
        mid = str(mod.mod_id)
        if resume and mid in done:
            result.skipped_mods += 1
            continue
        plan = classify_mod(mod, store_has_objects=store_has)
        if plan.classification == PurgeClass.NO_ASSETS.value or plan.files == 0:
            result.skipped_mods += 1
            last_mod_id = mid
            continue

        result.before_mods += 1
        result.before_files += plan.files
        result.before_bytes += plan.bytes_total

        removed_n, removed_b = _delete_planned_files(plan, dry_run=dry_run)
        remain_files = max(0, plan.files - removed_n)
        remain_bytes = max(0, plan.bytes_total - removed_b)
        result.after_files += remain_files
        result.after_bytes += remain_bytes
        if remain_files:
            result.after_mods += 1
            result.keep_mods += 1
            if len(result.keep_samples) < KEEP_DETAIL_CAP:
                sample = plan.to_dict()
                sample["remain_files"] = remain_files
                result.keep_samples.append(sample)
        else:
            result.cleaned_mods += 1
            if mid not in done:
                cleaned_ids.append(mid)
                done.add(mid)

        run_deleted_files += removed_n
        run_deleted_bytes += removed_b
        last_mod_id = mid
        pending_since_checkpoint += 1
        if (not dry_run) and pending_since_checkpoint >= batch_n:
            save_checkpoint(
                cleaned_mod_ids=cleaned_ids,
                deleted_files=checkpoint_deleted_files + run_deleted_files,
                deleted_bytes=checkpoint_deleted_bytes + run_deleted_bytes,
                last_mod_id=last_mod_id,
                path=checkpoint_path,
            )
            pending_since_checkpoint = 0

    result.deleted_files = run_deleted_files
    result.deleted_bytes = run_deleted_bytes
    result.last_mod_id = last_mod_id
    result.elapsed_sec = time.perf_counter() - t0
    if not dry_run:
        save_checkpoint(
            cleaned_mod_ids=cleaned_ids,
            deleted_files=checkpoint_deleted_files + run_deleted_files,
            deleted_bytes=checkpoint_deleted_bytes + run_deleted_bytes,
            last_mod_id=last_mod_id,
            path=checkpoint_path,
        )
    return result


def format_report(result: FastPurgeExecuteResult | FastPurgeAuditResult) -> str:
    def _human(n: int) -> str:
        size = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024.0
        return f"{n} B"

    if isinstance(result, FastPurgeAuditResult):
        return "\n".join(
            [
                "FAST INFO ASSET PURGE - AUDIT",
                "before:",
                f"  mods: {result.mods}",
                f"  files: {result.files}",
                f"  bytes: {result.bytes_total} ({_human(result.bytes_total)})",
                "reclaimable:",
                f"  files: {result.delete_files}",
                f"  bytes: {result.delete_bytes} ({_human(result.delete_bytes)})",
                f"SAFE_DELETE mods: {result.safe_mods}",
                f"LOW_RISK_DELETE mods: {result.low_risk_mods}",
                f"KEEP mods: {result.keep_mods}",
                f"elapsed: {result.elapsed_sec:.3f}s",
            ]
        )
    return "\n".join(
        [
            f"FAST INFO ASSET PURGE - {'DRY-RUN' if result.dry_run else 'EXECUTE'}",
            "before:",
            f"  mods: {result.before_mods}",
            f"  files: {result.before_files}",
            f"  bytes: {result.before_bytes} ({_human(result.before_bytes)})",
            "after:",
            f"  mods: {result.after_mods}",
            f"  files: {result.after_files}",
            f"  bytes: {result.after_bytes} ({_human(result.after_bytes)})",
            "deleted:",
            f"  files: {result.deleted_files}",
            f"  bytes: {result.deleted_bytes} ({_human(result.deleted_bytes)})",
            f"cleaned_mods: {result.cleaned_mods}",
            f"keep_mods: {result.keep_mods}",
            f"last_mod_id: {result.last_mod_id or '-'}",
            f"elapsed: {result.elapsed_sec:.3f}s",
        ]
    )
